"""jgmDeb: minimal QMI client over the Qualcomm IPC router (AF_MSM_IPC = 27), own work. No vendor code.

Services: 1 WDS, 2 DMS, 3 NAS, 5 WMS, 9 VOICE, 0xA CAT, 0xB UIM, 0xC PBM.
Wire format on the router: [ctl_flags u8][txn u16 LE][msg_id u16 LE][len u16 LE][TLVs: type u8, len u16 LE, value].
Usage: c = QMI(3); res = c.call(0x20, {}) -> {'result': (0, 0), tlv_type: bytes, ...}
"""
import socket, struct, fcntl, ctypes, ctypes.util, os, time, threading

AF_MSM_IPC = 27
IPC_ROUTER_IOCTL_LOOKUP_SERVER = (3 << 30) | (20 << 16) | (0xC3 << 8) | 2          # _IOWR(0xC3, 2, struct server_lookup_args[20 bytes])
libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)

def lookup(service, instance=0, mask=0, maxn=8):
    """-> [(node, port, service, instance)] of servers registered for a service (mask 0 = any instance)"""
    s = socket.socket(AF_MSM_IPC, socket.SOCK_DGRAM, 0)
    buf = bytearray(20 + 16 * maxn)
    struct.pack_into('<IIiiI', buf, 0, service, instance, maxn, 0, mask)
    try: fcntl.ioctl(s.fileno(), IPC_ROUTER_IOCTL_LOOKUP_SERVER, buf)
    except OSError: return []
    finally: s.close()
    n = struct.unpack_from('<i', buf, 12)[0]
    out = []
    for i in range(min(n, maxn)):
        out.append(struct.unpack_from('<IIII', buf, 20 + 16 * i))
    return out

def _addr_id(node, port):                    # struct sockaddr_msm_ipc: family u16, addrtype u8 (2 = ID), pad, node u32, port u32
    return struct.pack('<H2xB3xIIB3x', AF_MSM_IPC, 2, node, port, 0)       # 20 bytes

class QMI:
    def __init__(self, service, timeout=5.0):
        self.service = service; self.txn = 0; self.timeout = timeout; self.lock = threading.Lock(); self.sock = None; self.ind = []
        self.connect()
    def connect(self):
        srv = lookup(service=self.service)
        if not srv: raise OSError('QMI service 0x%x not found' % self.service)
        self.node, self.port = srv[0][0], srv[0][1]
        self.sock = socket.socket(AF_MSM_IPC, socket.SOCK_DGRAM, 0); self.sock.settimeout(self.timeout)
    def _send(self, data):
        a = _addr_id(self.node, self.port)
        r = libc.sendto(self.sock.fileno(), data, len(data), 0, a, len(a))
        if r < 0: raise OSError(ctypes.get_errno(), 'sendto failed')
    def call(self, msg_id, tlvs=None, timeout=None):
        """tlvs: {type: bytes}. Returns {tlv type: bytes}, plus 'result': (qmi_result, qmi_error). Indications seen meanwhile go to .ind"""
        with self.lock:
            self.txn = (self.txn % 0xFFFE) + 1; body = b''.join(struct.pack('<BH', t, len(v)) + v for t, v in (tlvs or {}).items())
            self._send(struct.pack('<BHHH', 0, self.txn, msg_id, len(body)) + body)
            end = time.time() + (timeout or self.timeout)
            while time.time() < end:
                self.sock.settimeout(max(0.05, end - time.time()))
                try: d = self.sock.recv(4096)
                except socket.timeout: break
                if len(d) < 7: continue
                fl, txn, mid, ln = struct.unpack_from('<BHHH', d, 0)
                tl = parse_tlvs(d[7:7 + ln])
                if fl == 4: self.ind.append((mid, tl)); continue
                if txn == self.txn:
                    tl['result'] = struct.unpack('<HH', tl[2][:4]) if 2 in tl else (0, 0)
                    return tl
            raise TimeoutError('QMI 0x%x/0x%x timed out' % (self.service, msg_id))
    def poll_ind(self, timeout=0.1):
        """collect indications: -> [(msg_id, {tlvs})]"""
        end = time.time() + timeout
        with self.lock:
            while time.time() < end:
                self.sock.settimeout(max(0.01, end - time.time()))
                try: d = self.sock.recv(4096)
                except socket.timeout: break
                if len(d) >= 7:
                    fl, txn, mid, ln = struct.unpack_from('<BHHH', d, 0)
                    if fl == 4: self.ind.append((mid, parse_tlvs(d[7:7 + ln])))
            out, self.ind = self.ind, []
        return out

def parse_tlvs(b):
    out = {}; i = 0
    while i + 3 <= len(b):
        t, ln = struct.unpack_from('<BH', b, i); out[t] = bytes(b[i + 3:i + 3 + ln]); i += 3 + ln
    return out
