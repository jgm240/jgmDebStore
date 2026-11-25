"""jgmDeb: SIM card access through the modem's QMI UIM service (own work). Needs the modem running (Settings > Modem / audio).
card_status(), iccid(), imsi(), read_adn() (contacts stored on the SIM), verify_pin(). Also operator and signal via NAS."""
import struct, time, os, threading
import jgmqmi as Q

UIM, NAS, DMS = 0x0B, 0x03, 0x02
_c = {}; _irsc = False; _setup_lock = threading.Lock()
def irsc():
    """the kernel's IPC router holds back every send until user space has given it security rules (Android's irsc_util does this at boot)"""
    global _irsc
    if _irsc: return
    import socket, fcntl
    s_ = socket.socket(27, socket.SOCK_DGRAM, 0); gids = [0, 1000, 1001, 1002, 3001, 3002, 3003, 3004, 3005]
    try: fcntl.ioctl(s_.fileno(), 0x8010C305, struct.pack('<iIII', len(gids), 0xFFFFFFFF, 0xFFFFFFFF, 0) + b''.join(struct.pack('<I', g) for g in gids))
    except OSError: pass
    s_.close(); _irsc = True
def modem_up():
    try: return Q.servers().get(UIM) is not None
    except Exception: return False
def svc(n):
    irsc()
    c = _c.get(n)
    if c is None or c.sock is None:
        a = Q.servers().get(n)
        if not a: raise RuntimeError('modem service 0x%x not available (is the modem running?)' % n)
        c = _c[n] = Q.QMI(n, 6.0, a)
    return c
def reset(n=None):
    """drop cached clients (all, or one service) so the next svc() reconnects; needed after the modem restarts (its services get new ports)"""
    for k in ([n] if n is not None else list(_c)):
        c = _c.pop(k, None)
        if c:
            try: c.close()
            except Exception: pass
def refresh():
    """reconnect clients whose service moved (modem restarted) -> True if anything was dropped"""
    try: cur = Q.servers()
    except Exception: return False
    gone = [k for k, c in list(_c.items()) if cur.get(k) != (c.node, c.port)]
    for k in gone: reset(k)
    return bool(gone)

def card_status():
    """-> {'slots': [{'slot': 1.., 'state', 'error', 'apps': [{'type','aid','state','pin1_state','pin1_retries','puk1_retries'}]}], 'slot': the slot with a usable card or None, ...}"""
    r = svc(UIM).call(0x2f)
    if r['result'][0] != 0: raise RuntimeError('card status error %d' % r['result'][1])
    b = r.get(0x10, b''); nslots = b[8]; i = 9; slots = []
    for n in range(nslots):
        cs, upin, upr, upkr, err, napps = b[i:i + 6]; i += 6; apps = []
        for _a in range(napps):
            typ, st, ps, pf, pr, pur, al = b[i:i + 7]; i += 7; aid = bytes(b[i:i + al]); i += al
            up, p1s, p1r, pk1, p2s, p2r, pk2 = b[i:i + 7]; i += 7
            apps.append({'type': {1: 'SIM', 2: 'USIM', 3: 'RUIM', 4: 'CSIM', 5: 'ISIM'}.get(typ, typ), 'aid': aid, 'state': st, 'pin1_state': p1s, 'pin1_retries': p1r, 'puk1_retries': pk1})
        slots.append({'slot': n + 1, 'state': {0: 'absent', 1: 'present', 2: 'error'}.get(cs, 'unknown'), 'error': err, 'apps': apps})
    usable = next((s_ for s_ in slots if s_['state'] == 'present' and any(a['type'] in ('USIM', 'SIM') for a in s_['apps'])), None)
    return {'slots': slots, 'slot': usable['slot'] if usable else None, 'usable': usable, 'raw': b.hex()}
def registered():
    try: return svc(NAS).call(0x24).get(1, b'')[:1] == b'\x01'
    except Exception: return False
def ensure_ready(wait=25):
    """cheap check before a call: if the modem is already registered nothing is touched (no re-provisioning, no mode change); otherwise full setup()"""
    if registered(): return {'ok': True, 'registered': True}
    return setup(wait)
def setup(wait=40):
    with _setup_lock: return _setup(wait)
def _setup(wait):
    """find the slot with a card, activate its (U)SIM app as the primary session, wait for network registration. -> status dict"""
    st = card_status(); u = st['usable']
    if not u: return {'ok': False, 'why': 'no SIM card found'}
    app = next(a for a in u['apps'] if a['type'] in ('USIM', 'SIM'))
    if app['pin1_state'] == 1: return {'ok': False, 'why': 'PIN required', 'pin_retries': app['pin1_retries'], 'slot': u['slot']}
    r = svc(UIM).call(0x38, {0x01: bytes([0, 1]), 0x10: bytes([u['slot'], len(app['aid'])]) + app['aid']}, timeout=20)
    end = time.time() + wait; reg = False
    try: svc(DMS).call(0x2e, {0x01: bytes([0])})                       # online
    except Exception: pass
    try: apply_mode(force=False)                                       # only if the modem's setting differs (a change makes it search the network again)
    except Exception: pass
    while time.time() < end:
        try: reg = svc(NAS).call(0x24).get(1, b'')[:1] == b'\x01'
        except Exception: reg = False
        if reg: break
        time.sleep(2)
    return {'ok': True, 'slot': u['slot'], 'registered': reg, 'provision': r['result']}
PIN_STATES = {0: 'unknown', 1: 'enabled, not verified', 2: 'enabled, verified', 3: 'disabled', 4: 'blocked', 5: 'permanently blocked'}
APP_STATES = {0: 'unknown', 1: 'detected', 2: 'PIN required', 3: 'PUK required', 4: 'perso check', 5: 'perso permanently blocked', 6: 'ready'}

def _path(p):
    """file path as the modem wants it: length in BYTES, then each id as a little-endian u16 high byte first (3F00 -> 00 3F)"""
    return struct.pack('<B', 2 * len(p)) + b''.join(struct.pack('<H', x) for x in p)
def read_transparent(fid, path, length=0, offset=0):
    r = svc(UIM).call(0x20, {0x01: bytes([0, 0]), 0x02: struct.pack('<H', fid) + _path(path), 0x03: struct.pack('<HH', offset, length)}, timeout=10)
    if r['result'][0] != 0: raise RuntimeError('read error %d' % r['result'][1])
    d = r.get(0x11, b''); n = struct.unpack('<H', d[:2])[0]; return d[2:2 + n]
def file_attrs(fid, path):
    r = svc(UIM).call(0x24, {0x01: bytes([0, 0]), 0x02: struct.pack('<H', fid) + _path(path)}, timeout=10)
    if r['result'][0] != 0: raise RuntimeError('attr error %d' % r['result'][1])
    b = r.get(0x11, b'')                                         # file attributes: size u16, id u16, type u8, record size u16, record count u16, ...
    size, fid_, ftype, rec_size, rec_count = struct.unpack('<HHBHH', b[:9]) if len(b) >= 9 else (0, 0, 0, 0, 0)
    return {'size': size, 'rec_size': rec_size, 'rec_count': rec_count, 'type': ftype, 'raw': b.hex()}
def read_record(fid, path, rec, length):
    r = svc(UIM).call(0x21, {0x01: bytes([0, 0]), 0x02: struct.pack('<H', fid) + _path(path), 0x03: struct.pack('<HH', rec, length)}, timeout=10)
    if r['result'][0] != 0: raise RuntimeError('read record error %d' % r['result'][1])
    d = r.get(0x11, b''); n = struct.unpack('<H', d[:2])[0]; return d[2:2 + n]

def _bcd(b, swap=True):
    out = ''
    for x in b:
        lo, hi = x & 15, x >> 4
        for v in ((lo, hi) if swap else (hi, lo)):
            out += '0123456789*#abc?'[v] if v != 15 else ''
    return out
def iccid(): return _bcd(read_transparent(0x2FE2, [0x3F00]))
def imsi():
    d = read_transparent(0x6F07, [0x3F00, 0x7FFF]); n = d[0]; s = ''.join('%d%d' % (x & 15, x >> 4) for x in d[1:1 + n])
    return s[1:]                                                  # first digit is the parity nibble
def decode_alpha(b):
    b = bytes(b).rstrip(b'\xff')
    if not b: return ''
    if b[0] == 0x80: return b[1:].decode('utf-16-be', 'ignore').rstrip('\0￿')
    GSM = '@\xa3$\xa5\xe8\xe9\xf9\xec\xf2\xc7\n\xd8\xf8\r\xc5\xe5Δ_ΦΓΛΩΠΨΣΘΞ\x1b\xc6\xe6\xdf\xc9 !"#\xa4%&\'()*+,-./0123456789:;<=>?\xa1ABCDEFGHIJKLMNOPQRSTUVWXYZ\xc4\xd6\xd1\xdc\xa7\xbfabcdefghijklmnopqrstuvwxyz\xe4\xf6\xf1\xfc\xe0'
    return ''.join(GSM[c] if c < len(GSM) else '?' for c in b)
def read_adn():
    """contacts stored on the SIM (ADN) -> [(name, number)]. Tries the classic telecom DF first, then the USIM one."""
    last = None
    for path in ([0x3F00, 0x7F10], [0x3F00, 0x7FFF, 0x5F3A]):
        try:
            at = file_attrs(0x6F3A, path)
            if not at['rec_count']: continue
            out = []
            for rec in range(1, at['rec_count'] + 1):
                d = read_record(0x6F3A, path, rec, at['rec_size'])
                if len(d) < 14 or all(x == 0xff for x in d): continue
                alpha, tail = d[:-14], d[-14:]; nl = tail[0]; num = _bcd(tail[2:2 + max(0, min(nl - 1, 10))])
                if tail[1] == 0x91 and num: num = '+' + num
                if alpha.strip(b'\xff') or num: out.append((decode_alpha(alpha), num))
            return out
        except Exception as e: last = e
    raise RuntimeError('could not read contacts: %s' % last)
def verify_pin(pin):
    r = svc(UIM).call(0x26, {0x01: bytes([0, 0]), 0x02: bytes([1, len(pin)]) + pin.encode()}, timeout=10)
    return r['result'][0] == 0, r['result'][1]

# ---- network info (NAS)
def operator():
    """-> {'registered': bool, 'name': str, 'mcc','mnc'} from NAS Get Serving System + Operator Name"""
    r = svc(NAS).call(0x24); out = {'registered': False, 'raw': {k: v.hex() for k, v in r.items() if k != 'result'}}
    ss = r.get(0x01, b'')
    if len(ss) >= 2: out['registered'] = ss[0] == 1
    return out
def signal():
    r = svc(NAS).call(0x20); b = r.get(0x01, b'')
    return (b[0], b[1] - 256 if b[1] > 127 else b[1]) if len(b) >= 2 else None       # (dBm-ish strength, radio interface)


# ---- network mode: which radio technologies the modem may use (NAS system selection, mode preference bits: GSM 0x04, WCDMA 0x08, LTE 0x10)
NET_MODES = [('2G only', 0x04), ('3G only', 0x08), ('2G + 3G', 0x0C), ('4G / VoLTE (auto)', 0x1C)]
MODE_FILE = '/data/jgm/netmode'
RAT_NAMES = {1: 'CDMA', 2: 'EVDO', 4: '2G (GSM)', 5: '3G (UMTS)', 8: '4G (LTE)', 9: 'TD-SCDMA'}
def get_mode():
    try: return max(0, min(len(NET_MODES) - 1, int(open(MODE_FILE).read().strip())))
    except Exception: return 2                       # default: 2G + 3G (plain circuit-switched voice; VoLTE needs an IMS data bearer that is not set up yet)
def mode_bits():
    """the modem's current mode preference bits (NAS Get System Selection Preference, TLV 0x11) or None"""
    v = svc(NAS).call(0x34).get(0x11, b'')
    return struct.unpack('<H', v[:2])[0] if len(v) >= 2 else None
def apply_mode(idx=None, force=True):
    """tell the modem which technologies it may use (setup() re-applies the saved choice on every start, but only when it differs)"""
    if idx is not None: open(MODE_FILE, 'w').write(str(idx))
    bits = NET_MODES[get_mode()][1]
    if not force:
        try:
            if mode_bits() == bits: return (0, 0)
        except Exception: pass
    return svc(NAS).call(0x33, {0x11: struct.pack('<H', bits), 0x17: bytes([1])})['result']
def status():
    """{'registered': bool, 'rat': 'name', 'dbm': int or None}"""
    b = svc(NAS).call(0x24).get(1, b''); out = {'registered': bool(b) and b[0] == 1, 'rat': RAT_NAMES.get(b[-1], '?') if len(b) >= 6 else '?', 'dbm': None}
    try:
        r = svc(NAS).call(0x4f)
        for k, fmt, idx in ((0x14, '<bbhh', 2), (0x13, 'bb', 0), (0x12, 'b', 0)):
            if k in r:
                v = struct.unpack(fmt, r[k][:struct.calcsize(fmt)]); out['dbm'] = v[idx] if k != 0x14 else v[2]; break
    except Exception: pass
    return out
