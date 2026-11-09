"""jgmDeb: Bluetooth LE in user space (own work). The kernel has no HCI transport driver, so this talks HCI directly to the WCNSS (Iris) controller
over its SMD channels: /dev/smd3 = APPS_RIVA_BT_CMD (commands + events, no H4 type byte) and /dev/smd2 = APPS_RIVA_BT_ACL (ACL data).
Needs the WCNSS firmware booted (opt-jgm/wifi-up.sh does that at boot). Supports: LE scan, one LE connection, GATT discovery/read/write over ATT.
Not supported: Bluetooth Classic (audio, file transfer), pairing/encryption."""
import os, time, select, struct, threading

CMD, ACL = '/dev/smd3', '/dev/smd2'
BASE = '-0000-1000-8000-00805f9b34fb'
def uuid16(u): return '0000%04x%s' % (u, BASE)

class BLEError(Exception): pass

class BLE:
    def __init__(self): self.cmd = self.acl = None; self.lock = threading.RLock(); self.handle = None; self.evq = []; self.aclq = []; self.chars = {}
    @staticmethod
    def available(): return os.path.exists(CMD) and os.path.exists(ACL)
    # ---- transport
    def open(self):
        if self.cmd is not None: return
        if not self.available(): raise BLEError('no Bluetooth channels (/dev/smd2, /dev/smd3)')
        self.cmd = os.open(CMD, os.O_RDWR | os.O_NONBLOCK); self.acl = os.open(ACL, os.O_RDWR | os.O_NONBLOCK)
        self.hci(0x0c03, wait=2)                                              # reset
        self.hci(0x0c01, bytes.fromhex('ffffffffffffff3f')); self.hci(0x2001, bytes.fromhex('1f00000000000000'))   # event masks (incl. LE meta events)
    def close(self):
        with self.lock:
            try:
                if self.handle is not None: self.disconnect()
            except Exception: pass
            for fd in (self.cmd, self.acl):
                if fd is not None: os.close(fd)
            self.cmd = self.acl = None
    def pump(self, t):
        """read whatever arrives within t seconds into the event / acl queues"""
        end = time.time() + t
        while True:
            r, _, _ = select.select([self.cmd, self.acl], [], [], max(0, min(0.1, end - time.time())))
            for fd in r:
                try: d = os.read(fd, 4096)
                except BlockingIOError: continue
                (self.evq if fd == self.cmd else self.aclq).append(d)
            if time.time() >= end: break
    def hci(self, op, params=b'', wait=1.0):
        """send a command and return the Command Complete / Status parameters (bytes after the opcode)"""
        with self.lock:
            os.write(self.cmd, struct.pack('<HB', op, len(params)) + params); end = time.time() + wait
            while time.time() < end:
                self.pump(0.05)
                for i, e in enumerate(self.evq):
                    if e[0] == 0x0e and len(e) >= 6 and struct.unpack_from('<H', e, 3)[0] == op: del self.evq[i]; return e[6:]
                    if e[0] == 0x0f and len(e) >= 6 and struct.unpack_from('<H', e, 4)[0] == op: del self.evq[i]; return e[2:3]
            return None
    # ---- scanning
    def scan(self, seconds=6, active=True):
        """-> {addr: {'name', 'rssi', 'type'}}"""
        with self.lock:
            self.open(); self.evq.clear(); self.hci(0x200b, struct.pack('<BHHBB', 1 if active else 0, 0x0060, 0x0030, 0, 0))
            self.hci(0x200c, bytes([1, 0])); seen = {}; end = time.time() + seconds
            while time.time() < end:
                self.pump(0.2)
                while self.evq:
                    e = self.evq.pop(0)
                    if e[0] == 0x3e and e[2] == 2:
                        n = e[3]; i = 4
                        for _ in range(n):
                            at = e[i + 1]; addr = ':'.join('%02X' % b for b in e[i + 7:i + 1:-1]); dl = e[i + 8]; data = e[i + 9:i + 9 + dl]; rssi = e[i + 9 + dl]; rssi = rssi - 256 if rssi > 127 else rssi
                            name = seen.get(addr, {}).get('name', ''); j = 0
                            while j < len(data) and data[j]:
                                if data[j + 1] in (8, 9): name = data[j + 2:j + 1 + data[j]].decode('utf8', 'replace')
                                j += 1 + data[j]
                            seen[addr] = {'name': name, 'rssi': rssi, 'type': at}; i += 10 + dl
            self.hci(0x200c, bytes([0, 0])); return seen
    # ---- connection
    def connect(self, addr, addr_type=0, timeout=10):
        with self.lock:
            self.open(); self.evq.clear(); a = bytes(int(x, 16) for x in addr.split(':'))[::-1]
            self.hci(0x200d, struct.pack('<HHBB6sBHHHHHH', 0x0060, 0x0030, 0, addr_type, a, 0, 0x0018, 0x0028, 0, 0x00c8, 0, 0), wait=1)
            end = time.time() + timeout
            while time.time() < end:
                self.pump(0.2)
                for i, e in enumerate(self.evq):
                    if e[0] == 0x3e and e[2] == 1:
                        del self.evq[i]
                        if e[3] != 0: raise BLEError('connection failed (status 0x%02x)' % e[3])
                        self.handle = struct.unpack_from('<H', e, 4)[0] & 0x0fff; self.aclq.clear(); self.chars = {}; return self.handle
            self.hci(0x200e); raise BLEError('connection timed out')            # LE Create Connection Cancel
    def disconnect(self):
        with self.lock:
            if self.handle is None: return
            self.hci(0x0406, struct.pack('<HB', self.handle, 0x13), wait=1); self.pump(0.5); self.handle = None; self.chars = {}
    # ---- ATT
    def att(self, pdu, expect, wait=4):
        with self.lock:
            if self.handle is None: raise BLEError('not connected')
            os.write(self.acl, struct.pack('<HHHH', self.handle, len(pdu) + 4, len(pdu), 4) + pdu); end = time.time() + wait
            while time.time() < end:
                self.pump(0.05)
                while self.aclq:
                    d = self.aclq.pop(0)
                    if len(d) >= 9 and struct.unpack_from('<H', d, 6)[0] == 4:          # L2CAP CID 4 = ATT
                        p = d[8:]
                        if p[0] == 0x01: raise BLEError('ATT error 0x%02x' % p[4])
                        if p[0] in expect: return p
            raise BLEError('ATT timeout')
    def discover(self):
        """characteristic discovery -> {uuid string: value handle}"""
        with self.lock:
            if self.chars: return self.chars
            start = 1; out = {}
            while start < 0xffff:
                try: p = self.att(struct.pack('<BHHH', 0x08, start, 0xffff, 0x2803), (0x09,))
                except BLEError: break
                ln = p[1]; i = 2
                while i + ln <= len(p):
                    h, props, vh = struct.unpack_from('<HBH', p, i); u = p[i + 5:i + ln]
                    out[uuid16(struct.unpack('<H', u)[0]) if len(u) == 2 else '-'.join(h_ for h_ in (u[::-1].hex()[:8], u[::-1].hex()[8:12], u[::-1].hex()[12:16], u[::-1].hex()[16:20], u[::-1].hex()[20:]))] = vh
                    start = h + 1; i += ln
            self.chars = out; return out
    def read(self, uuid):
        h = self.discover().get(uuid.lower())
        if h is None: raise BLEError('characteristic not on this device')
        return self.att(struct.pack('<BH', 0x0a, h), (0x0b,))[1:]
    def write(self, uuid, data, response=True):
        h = self.discover().get(uuid.lower())
        if h is None: raise BLEError('characteristic not on this device')
        if response: self.att(struct.pack('<BH', 0x12, h) + bytes(data), (0x13,))
        else: os.write(self.acl, struct.pack('<HHHH', self.handle, len(data) + 7, len(data) + 3, 4) + struct.pack('<BH', 0x52, h) + bytes(data))

_ble = None
def get():
    global _ble
    if _ble is None: _ble = BLE()
    return _ble
