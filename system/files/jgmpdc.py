"""jgmDeb: carrier configuration (modem MCFG profiles) through the QMI PDC service (own work, message layout from the public libqmi description).
Android's phone software picks a carrier profile (an .mbn file from the modem firmware) for the SIM and loads/activates it; without one the modem has no IMS/VoLTE
settings. This does the same: list_configs(), selected(), load(path, id), select(id), activate(), plus pick_for(mcc_mnc) to choose a profile file.
Config types: 0 = platform, 1 = software (MCFG_SW). Results come back as indications carrying the request's token."""
import struct, time, os, threading
import jgmsim as S

PDC = 0x24
SW, PLATFORM = 1, 0
_tok = [0x100]
def _svc(): return S.svc(PDC)
def _token(): _tok[0] += 1; return _tok[0]
def register():
    r = _svc().call(0x20, {0x10: bytes([1])}); return r['result']
def _wait(token, mid, timeout=15):
    """wait for the indication of message `mid` carrying `token` -> TLV dict, or None"""
    end = time.time() + timeout; c = _svc()
    while time.time() < end:
        for imid, tl in c.poll_ind(0.3):
            if imid == mid and 0x10 in tl and struct.unpack('<I', tl[0x10][:4])[0] == token: return tl
    return None
def _ok(tl): return tl is not None and (0x01 not in tl or struct.unpack('<H', tl[0x01][:2])[0] == 0)
def list_configs(ctype=SW):
    t = _token(); r = _svc().call(0x24, {0x10: struct.pack('<I', t), 0x11: struct.pack('<I', ctype)})
    if r['result'][0] != 0: raise RuntimeError('list configs refused (error %d)' % r['result'][1])
    tl = _wait(t, 0x24)
    if tl is None: raise RuntimeError('no answer to list configs')
    out = []; b = tl.get(0x11, b'')
    if b:
        i = 1
        for _ in range(b[0]):
            typ = struct.unpack('<I', b[i:i + 4])[0]; ln = b[i + 4]; out.append((typ, bytes(b[i + 5:i + 5 + ln]))); i += 5 + ln
    return out
def selected(ctype=SW):
    """-> (active id bytes or None, pending id or None)"""
    t = _token(); r = _svc().call(0x22, {0x01: struct.pack('<I', ctype), 0x10: struct.pack('<I', t)})
    if r['result'][0] != 0: raise RuntimeError('get selected refused (error %d)' % r['result'][1])
    tl = _wait(t, 0x22)
    if tl is None: raise RuntimeError('no answer to get selected')
    def idof(k): b = tl.get(k, b''); return bytes(b[1:1 + b[0]]) if b else None
    return idof(0x11), idof(0x12)
def config_info(cid, ctype=SW):
    t = _token(); r = _svc().call(0x28, {0x01: struct.pack('<I', ctype) + bytes([len(cid)]) + cid, 0x10: struct.pack('<I', t)})
    if r['result'][0] != 0: raise RuntimeError('info refused (error %d)' % r['result'][1])
    tl = _wait(t, 0x28)
    if tl is None: return None
    d = tl.get(0x12, b''); return {'size': struct.unpack('<I', tl[0x11][:4])[0] if 0x11 in tl else 0, 'description': bytes(d[1:1 + d[0]]).decode('utf8', 'replace') if d else '', 'version': struct.unpack('<I', tl[0x13][:4])[0] if 0x13 in tl else 0}
def load(data, cid, ctype=SW, chunk=900):
    """upload an .mbn into the modem's configuration store under the id `cid` (bytes)"""
    total = len(data); off = 0
    while off < total:
        part = data[off:off + chunk]; t = _token()
        body = struct.pack('<I', ctype) + bytes([len(cid)]) + cid + struct.pack('<I', total) + struct.pack('<H', len(part)) + part
        r = _svc().call(0x26, {0x01: body, 0x10: struct.pack('<I', t)}, timeout=20)
        if r['result'][0] != 0: raise RuntimeError('load refused at %d/%d (error %d)' % (off, total, r['result'][1]))
        tl = _wait(t, 0x26, 20)
        if not _ok(tl): raise RuntimeError('load failed at %d/%d' % (off, total))
        off += len(part)
    return True
def select(cid, ctype=SW):
    t = _token(); r = _svc().call(0x23, {0x01: struct.pack('<I', ctype) + bytes([len(cid)]) + cid, 0x10: struct.pack('<I', t)})
    if r['result'][0] != 0: raise RuntimeError('select refused (error %d)' % r['result'][1])
    return _ok(_wait(t, 0x23))
def activate(ctype=SW):
    t = _token(); r = _svc().call(0x27, {0x01: struct.pack('<I', ctype), 0x10: struct.pack('<I', t)})
    if r['result'][0] != 0: raise RuntimeError('activate refused (error %d)' % r['result'][1])
    return _ok(_wait(t, 0x27, 30))
def delete(cid, ctype=SW):
    t = _token(); r = _svc().call(0x25, {0x01: struct.pack('<I', ctype), 0x10: struct.pack('<I', t), 0x11: bytes([len(cid)]) + cid})
    return r['result'][0] == 0 and _ok(_wait(t, 0x25))
