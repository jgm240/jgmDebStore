"""jgmDeb: voice calls through the modem's QMI VOICE service (own work). Needs the modem running and the SIM set up (jgmsim.setup()).
dial / answer / hangup / calls() plus an indication watcher for incoming calls. In-call audio is routed with opt-jgm/call-audio.sh."""
import struct, time, subprocess, threading
import jgmsim as S

VOICE = 0x09
STATES = {1: 'Dialing', 2: 'Incoming', 3: 'Connected', 4: 'Connecting', 5: 'Ringing', 6: 'On hold', 7: 'Waiting', 8: 'Ending', 9: 'Ended', 10: 'Setup'}
_registered = False; _last_sig = None
END_REASONS = {}                       # call id -> text the modem gave when the call ended (e.g. 'cc_sip_486_busy')

def svc(): return S.svc(VOICE)
def register():
    """subscribe to call-status indications"""
    global _registered
    if _registered: return
    try: svc().call(0x03, {0x13: bytes([1])})
    except Exception: pass
    _registered = True
def _parse_calls(info, nums):
    """info: u8 count + {id, state, type, direction, mode, multiparty, als} x count.  nums: u8 count + {id, presentation, len, number} x count"""
    out = {}
    if info:
        for i in range(info[0]):
            cid, st, typ, d, mode, mp, als = info[1 + 7 * i:8 + 7 * i]
            out[cid] = {'id': cid, 'state': st, 'state_name': STATES.get(st, str(st)), 'type': typ, 'dir': 'in' if d == 2 else 'out', 'number': ''}
    if nums:
        k = 1
        for _ in range(nums[0]):
            cid, pres, ln = nums[k:k + 3]; num = nums[k + 3:k + 3 + ln].decode('ascii', 'ignore'); k += 3 + ln
            if cid in out: out[cid]['number'] = num
    return list(out.values())
def calls():
    """[{'id','state','state_name','dir','number'}] of the calls the modem knows about"""
    r = svc().call(0x2f)
    out = _parse_calls(r.get(0x10, b''), r.get(0x11, b'')) if r['result'][0] == 0 else []
    sig = repr(sorted((k, v.hex()) for k, v in r.items() if k != 'result'))
    global _last_sig
    if sig != _last_sig: _last_sig = sig; debug('calls() result %s tlvs %s' % (r['result'], sig[:200]))
    return out
def dial(number):
    r = svc().call(0x20, {0x01: number.encode('ascii'), 0x10: bytes([0])}, timeout=15)
    debug('dial %s*** -> result %s tlvs %s' % (number[:4], r['result'], {hex(k): v.hex() for k, v in r.items() if k != 'result'}))
    if r['result'][0] != 0: raise RuntimeError('dial failed (error %d)' % r['result'][1])
    return r.get(0x10, b'\x01')[0]
def hangup(cid=None):
    for c in ([cid] if cid is not None else [c['id'] for c in calls()]):
        try: svc().call(0x21, {0x01: bytes([c])}, timeout=8)
        except Exception: pass
def answer(cid):
    r = svc().call(0x22, {0x01: bytes([cid])}, timeout=10)
    if r['result'][0] != 0: raise RuntimeError('answer failed (error %d)' % r['result'][1])
def send_dtmf(cid, digits):
    svc().call(0x25, {0x01: bytes([cid, len(digits)]) + digits.encode()}, timeout=5)        # QMI VOICE burst DTMF (0x24 is 'send flash')

# ---- in-call audio
def audio(mode):
    """mode: 'handset' | 'speaker' | 'off'"""
    subprocess.Popen(['sh', '/opt/jgm/call-audio.sh', mode], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ---- watcher for incoming calls: calls cb(call dict) when a new incoming call appears, and end_cb(call) when it ends
class Watcher(threading.Thread):
    def __init__(self, on_incoming, on_end=None):
        super().__init__(daemon=True); self.on_incoming = on_incoming; self.on_end = on_end; self.seen = {}; self.stop = False
    def run(self):
        ready = False
        while not self.stop:
            try:
                if not ready:
                    if not S.modem_up(): time.sleep(5); continue
                    st = S.setup(20)
                    if not st['ok']: time.sleep(15); continue
                    register(); ready = True
                for mid, tl in svc().poll_ind(1.0):                     # indications arrive here; also poll state so nothing is missed
                    debug('indication 0x%x %s' % (mid, {hex(k): v.hex() for k, v in tl.items()}))
                    if mid == 0x2e and 0x2e in tl and len(tl[0x2e]) >= 3:
                        b = tl[0x2e]; END_REASONS[b[1]] = b[3:3 + b[2] * 2].decode('utf-16-le', 'ignore')
                cs = {c['id']: c for c in calls()}
                for cid, c in cs.items():
                    if cid not in self.seen and c['dir'] == 'in' and c['state'] in (2, 7, 10): self.on_incoming(c)
                    self.seen[cid] = c
                for cid in [i for i in self.seen if i not in cs]:
                    c = self.seen.pop(cid)
                    if self.on_end: self.on_end(c)
            except Exception: ready = False; time.sleep(5)

def ringback(on):
    """local ringback tone while the far end rings"""
    subprocess.Popen(['sh', '/opt/jgm/ringback.sh', 'start' if on else 'stop'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def debug(msg):
    try: open('/data/jgm/call-debug.log', 'a').write(time.strftime('%H:%M:%S ') + msg + '\n')
    except Exception: pass

def end_reason_text(cid):
    """friendly text for how a call ended, or ''"""
    r = (END_REASONS.get(cid) or '').lower()
    for key, text in (('busy', 'Busy'), ('noanswer', 'No answer'), ('no_answer', 'No answer'), ('reject', 'Rejected'), ('decline', 'Declined'), ('unreach', 'Not reachable'), ('barr', 'Call barred'), ('not_found', 'Number not found'), ('forbid', 'Not allowed')):
        if key in r: return text
    return r.replace('cc_', '').replace('_', ' ') if r else ''
