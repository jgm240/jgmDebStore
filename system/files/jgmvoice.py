"""jgmDeb: voice calls through the modem's QMI VOICE service (own work). Needs the modem running and the SIM set up (jgmsim.setup()).
dial / answer / hangup / calls() / DTMF plus an indication watcher for incoming calls. In-call audio is routed with opt-jgm/call-audio.sh,
the incoming-call ringtone with ringtone.sh, the local ringback tone with ringback.sh."""
import struct, time, subprocess, threading, os
import jgmsim as S

VOICE = 0x09
STATES = {1: 'Dialing', 2: 'Incoming', 3: 'Connected', 4: 'Connecting', 5: 'Ringing', 6: 'On hold', 7: 'Waiting', 8: 'Ending', 9: 'Ended', 10: 'Setup'}
RINGING_IN = (2, 7, 10)                # states of an incoming call that has not been answered yet
_registered = False; _last_sig = None; _reg_lock = threading.Lock()
END_REASONS = {}                       # call id -> text the modem gave when the call ended (e.g. 'cc_sip_486_busy')
END_CODES = {}                         # call id -> numeric QMI call end reason
LOG = '/data/jgm/call-debug.log'

def svc(): return S.svc(VOICE)
def reconnect(client=None):
    """drop the VOICE client (only if it is still 'client', when given: another thread may already have made a new one);
    the indication registration belongs to its socket, so it has to be made again"""
    global _registered
    if client is not None and S._c.get(VOICE) is not client: return
    S.reset(VOICE); _registered = False
def register():
    """subscribe to call-status indications (QMI VOICE Indication Register, TLV 0x13 = call notification events) -> True when registered"""
    global _registered
    with _reg_lock:
        if _registered: return True
        try:
            r = svc().call(0x03, {0x13: bytes([1])})
            _registered = r['result'][0] == 0
            if not _registered: debug('indication register error %s' % (r['result'],))
        except Exception as e: debug('indication register failed: %r' % e)
        return _registered
def _call(mid, tlvs=None, timeout=None, retry=True):
    """one VOICE request. A closed/broken client (modem restarted) is reconnected and the request retried once when retry is allowed."""
    q = svc()
    try: return q.call(mid, tlvs, timeout)
    except TimeoutError:
        if not (retry and S.refresh()): raise                     # a plain timeout: only retry if the service really moved
    except OSError:
        if not retry: raise
    reconnect(q); register()
    return svc().call(mid, tlvs, timeout)

def _parse_calls(info, nums, alert=b''):
    """info: u8 count + {id, state, type, direction, mode, multiparty, als} x count.  nums: u8 count + {id, presentation, len, number} x count.
    alert: u8 count + {id, alerting type (0 local, 1 remote = the network plays the ringback itself)} x count"""
    out = {}
    if info:
        for i in range(info[0]):
            rec = info[1 + 7 * i:8 + 7 * i]
            if len(rec) < 7: break
            cid, st, typ, d, mode, mp, als = rec
            out[cid] = {'id': cid, 'state': st, 'state_name': STATES.get(st, str(st)), 'type': typ, 'dir': 'in' if d == 2 else 'out', 'number': '', 'alert': None}
    if nums:
        k = 1
        for _ in range(nums[0]):
            if k + 3 > len(nums): break
            cid, pres, ln = nums[k:k + 3]; num = nums[k + 3:k + 3 + ln].decode('ascii', 'ignore'); k += 3 + ln
            if cid in out: out[cid]['number'] = num
    if alert:
        for i in range(alert[0]):
            rec = alert[1 + 2 * i:3 + 2 * i]
            if len(rec) == 2 and rec[0] in out: out[rec[0]]['alert'] = rec[1]
    return list(out.values())
def calls():
    """[{'id','state','state_name','dir','number','alert'}] of the calls the modem knows about. Raises on a modem error (an error is NOT 'no calls')."""
    r = _call(0x2f)
    if r['result'][0] != 0: raise RuntimeError('get all call info error %d' % r['result'][1])
    out = _parse_calls(r.get(0x10, b''), r.get(0x11, b''), r.get(0x13, b''))
    sig = repr(sorted((k, v.hex()) for k, v in r.items() if k != 'result'))
    global _last_sig
    if sig != _last_sig: _last_sig = sig; debug('calls() result %s tlvs %s' % (r['result'], sig[:200]))
    return out
def dial(number):
    """-> call id. Never retried automatically (a retry could place the call twice)."""
    number = ''.join(ch for ch in number if ch in '0123456789+*#')
    if not number: raise RuntimeError('no number')
    r = _call(0x20, {0x01: number.encode('ascii'), 0x10: bytes([0])}, timeout=15, retry=False)
    debug('dial %s*** -> result %s tlvs %s' % (number[:4], r['result'], {hex(k): v.hex() for k, v in r.items() if k != 'result'}))
    if r['result'][0] != 0: raise RuntimeError('dial failed (error %d)' % r['result'][1])
    cid = r.get(0x10, b'\x01')[0]; forget(cid); return cid
def forget(cid):
    """call ids are reused (usually 1): drop what an earlier call with this id left behind"""
    END_REASONS.pop(cid, None); END_CODES.pop(cid, None)
def hangup(cid=None, tries=3):
    """end one call (or all) -> True if the modem accepted it"""
    ok = True
    for c in ([cid] if cid is not None else [c['id'] for c in calls()]):
        done = False
        for _ in range(tries):
            try:
                r = _call(0x21, {0x01: bytes([c])}, timeout=8)
                if r['result'][0] == 0: done = True; break
                debug('hangup %d error %s' % (c, r['result']))
                if r['result'][1] in (0x1a, 0x29): done = True; break          # no effect / invalid id: the call is already gone
            except Exception as e: debug('hangup %d failed: %r' % (c, e))
            time.sleep(0.5)
        ok = ok and done
    return ok
def answer(cid):
    forget(cid)
    r = _call(0x22, {0x01: bytes([cid])}, timeout=10)
    debug('answer %d -> %s' % (cid, r['result']))
    if r['result'][0] != 0: raise RuntimeError('answer failed (error %d)' % r['result'][1])
def send_dtmf(cid, digit, ms=200):
    """one keypad tone into the call. GSM/UMTS calls use start/stop continuous DTMF (QMI VOICE 0x29 / 0x2A, like Android's RIL);
    burst DTMF (0x28) is only a fallback (it is a CDMA feature)."""
    try:
        r = _call(0x29, {0x01: bytes([cid, ord(digit)])}, timeout=5)
        if r['result'][0] == 0:
            time.sleep(ms / 1000.0)
            r2 = _call(0x2A, {0x01: bytes([cid])}, timeout=5); debug('dtmf %s cid %d -> %s / %s' % (digit, cid, r['result'], r2['result'])); return True
        debug('dtmf start %s error %s, trying burst' % (digit, r['result']))
        r = _call(0x28, {0x01: bytes([cid, 1]) + digit.encode()}, timeout=5); debug('dtmf burst -> %s' % (r['result'],))
        return r['result'][0] == 0
    except Exception as e: debug('dtmf failed: %r' % e); return False

# ---- sounds and in-call audio (all asynchronous; each script serialises itself)
def audio(mode):
    """mode: 'handset' | 'speaker' | 'off'"""
    subprocess.Popen(['sh', '/opt/jgm/call-audio.sh', mode], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def volume(level):
    """in-call earpiece/speaker volume 0..5 (the DSP's voice RX gain); takes effect immediately"""
    subprocess.Popen(['sh', '/opt/jgm/call-audio.sh', 'volume', str(max(0, min(5, int(level))))], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def ringback(on):
    """local ringback tone while the far end rings (only when the network does not send its own, see 'alert')"""
    subprocess.Popen(['sh', '/opt/jgm/ringback.sh', 'start' if on else 'stop'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def ringtone(on, vibrate=True, sound=True):
    """incoming-call ringtone on the loudspeaker (+ vibration)"""
    args = ['sh', '/opt/jgm/ringtone.sh', 'start' if on else 'stop']
    if on: args += ['sound' if sound else 'nosound', 'vibrate' if vibrate else 'novibrate']
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ---- watcher for incoming calls: calls on_incoming(call dict) when a new incoming call appears, and on_end(call) when one ends
class Watcher(threading.Thread):
    def __init__(self, on_incoming, on_end=None):
        super().__init__(daemon=True); self.on_incoming = on_incoming; self.on_end = on_end; self.seen = {}; self.stop = False; self.last_poll = 0
    def _ended(self, cid):
        c = self.seen.pop(cid)
        if self.on_end:
            try: self.on_end(c)
            except Exception as e: debug('on_end failed: %r' % e)
    def run(self):
        set_up = False; addr = None
        while not self.stop:
            try:
                srv = S.Q.servers()
                if srv.get(S.UIM) is None or srv.get(VOICE) is None:            # modem off (or restarting): every call it had is over
                    if addr is not None: debug('watcher: modem services gone'); S.reset(); reconnect(); addr = None; set_up = False
                    for cid in list(self.seen): self._ended(cid)
                    time.sleep(5); continue
                if srv.get(VOICE) != addr:                                   # first start, or the modem restarted: its services have new ports
                    if addr is not None: debug('watcher: modem restarted, reconnecting')
                    S.refresh(); reconnect(); addr = srv.get(VOICE); set_up = False
                if not set_up:
                    st = S.setup(20)
                    if not st['ok']: debug('watcher: setup: %s' % st.get('why')); time.sleep(15); continue
                    set_up = True
                if not register(): time.sleep(5); continue
                inds = svc().poll_ind(3.0)                              # returns when an indication arrives (cheap while idle)
                for mid, tl in inds:
                    debug('indication 0x%x %s' % (mid, {hex(k): v.hex() for k, v in tl.items()}))
                    if mid != 0x2e: continue
                    b = tl.get(0x2e, b'')                                # end reason text: count, {id, len, UTF-16 text}
                    if len(b) >= 3: END_REASONS[b[1]] = b[3:3 + b[2] * 2].decode('utf-16-le', 'ignore')
                    b = tl.get(0x14, b'')                                # end reason code: count, {id, u16}
                    if len(b) >= 4: END_CODES[b[1]] = struct.unpack('<H', b[2:4])[0]
                now = time.time()
                if not inds and not self.seen and now - self.last_poll < 10: continue      # idle: only ask the modem for the call list every 10 s
                self.last_poll = now
                cs = {c['id']: c for c in calls()}
                for cid, c in cs.items():
                    prev = self.seen.get(cid)
                    new_in = c['dir'] == 'in' and c['state'] in RINGING_IN and (prev is None or prev['dir'] != 'in' or prev['state'] not in RINGING_IN)
                    if new_in and prev is not None: self._ended(cid)                 # the id was reused by a new call before we saw the old one go
                    self.seen[cid] = c
                    if new_in: forget(cid); self.on_incoming(c)
                for cid in [i for i in self.seen if i not in cs]: self._ended(cid)
            except Exception as e:
                debug('watcher error: %r' % e); reconnect(S._c.get(VOICE)); time.sleep(3)

def end_reason(cid, wait=1.0):
    """friendly text for how a call ended, or ''. Waits a moment: the reason arrives in an indication the watcher handles."""
    end = time.time() + wait
    while not END_REASONS.get(cid) and time.time() < end: time.sleep(0.1)
    return end_reason_text(cid)
def end_reason_text(cid):
    r = (END_REASONS.get(cid) or '').lower()
    for key, text in (('busy', 'Busy'), ('noanswer', 'No answer'), ('no_answer', 'No answer'), ('noreply', 'No answer'), ('reject', 'Rejected'), ('decline', 'Declined'),
                      ('unreach', 'Not reachable'), ('barr', 'Call barred'), ('not_found', 'Number not found'), ('unallocated', 'Number does not exist'),
                      ('unassigned', 'Number does not exist'), ('forbid', 'Not allowed'), ('user triggered', ''), ('normal', ''), ('client_end', '')):
        if key in r: return text
    return r.replace('cc_', '').replace('_', ' ') if r else ''

def debug(msg):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 256 * 1024: os.replace(LOG, LOG + '.1')
        with open(LOG, 'a') as f: f.write(time.strftime('%H:%M:%S ') + msg + '\n')
    except Exception: pass
