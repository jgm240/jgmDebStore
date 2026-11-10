#!/usr/bin/env python3
"""jgmDeb adbd (own work): a small ADB daemon speaking the standard ADB wire protocol, so `adb shell`, `adb push/pull`, `adb reboot` work.
Transports: USB (FunctionFS, gadget function "adb") and TCP port 5555 (WiFi). Both are switched by flag files and re-read every few seconds:
  /data/jgm/adb-usb  exists -> USB adb on        /data/jgm/adb-tcp  exists -> adb over WiFi on (NO authentication: only enable on networks you trust)
Services: shell: (interactive pty or one command), exec:, sync: (push / pull / stat / list), reboot:[target], root:, remount:, usb:, tcpip:.
Not implemented: RSA authentication (USB adb is protected by physical access), shell_v2, jdwp, forward/reverse."""
import os, sys, struct, socket, threading, queue, subprocess, fcntl, time, stat, termios, ctypes, signal

A_SYNC, A_CNXN, A_AUTH, A_OPEN, A_OKAY, A_CLSE, A_WRTE = 0x434e5953, 0x4e584e43, 0x48545541, 0x4e45504f, 0x59414b4f, 0x45534c43, 0x45545257
VERSION = 0x01000001; DATA = '/data/jgm'
BANNER = b'device::ro.product.name=jgmdeb;ro.product.model=Nokia 8110 4G;ro.product.device=nokia_8110;features=cmd\0'
ENV = {'HOME': '/root', 'USER': 'root', 'LOGNAME': 'root', 'SHELL': '/bin/bash', 'TERM': 'xterm', 'LANG': 'C.UTF-8', 'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'}
def log(*a):
    try: open('/var/log/adbd.log', 'a').write(time.strftime('%H:%M:%S ') + ' '.join(str(x) for x in a) + '\n')
    except Exception: pass

class Closed(Exception): pass

class Stream:
    def __init__(self, conn, local, remote): self.conn = conn; self.local = local; self.remote = remote; self.inq = queue.Queue(); self.ack = threading.Event(); self.ack.set(); self.closed = False
    def send(self, data):
        """WRTE to the host in chunks, waiting for its OKAY each time"""
        for i in range(0, len(data), self.conn.maxdata):
            if self.closed: raise Closed()
            if not self.ack.wait(30): raise Closed()
            self.ack.clear(); self.conn.send(A_WRTE, self.local, self.remote, data[i:i + self.conn.maxdata])
    def recv(self):
        d = self.inq.get()
        if d is None: raise Closed()
        return d
    def readn(self, n):
        buf = getattr(self, '_buf', b'')
        while len(buf) < n: buf += self.recv()
        self._buf = buf[n:]; return buf[:n]
    def close(self):
        if not self.closed:
            self.closed = True; self.inq.put(None); self.ack.set()
            try: self.conn.send(A_CLSE, self.local, self.remote)
            except Exception: pass
            self.conn.streams.pop(self.local, None)

# ---------------------------------------------------------------------------------------------------------------- services
def ensure_pty():
    """the devpts mount inherited from the initramfs can go stale (openpty fails): remount it and recreate /dev/ptmx"""
    try: os.close(os.openpty()[0]); return
    except Exception: pass
    subprocess.call('umount /dev/pts 2>/dev/null; mount -t devpts devpts /dev/pts; rm -f /dev/ptmx; mknod -m 666 /dev/ptmx c 5 2', shell=True)

def svc_shell(st, cmd):
    import pty
    if not cmd: ensure_pty()
    if cmd:
        p = subprocess.Popen(['/bin/bash', '-c', cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV, cwd='/root'); rfd = p.stdout.fileno(); wfd = p.stdin.fileno()
    else:
        m, s = pty.openpty(); fcntl.ioctl(m, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
        p = subprocess.Popen(['/bin/bash', '-l'], stdin=s, stdout=s, stderr=s, env=ENV, cwd='/root', start_new_session=True, preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 1)); os.close(s); rfd = wfd = m
    def pump_out():
        try:
            while True:
                d = os.read(rfd, 4096)
                if not d: break
                st.send(d)
        except (OSError, Closed): pass
        finally:
            try: p.wait(timeout=2)
            except Exception: p.kill()
            st.close()
    threading.Thread(target=pump_out, daemon=True).start()
    try:
        while True:
            d = st.recv()
            try: os.write(wfd, d)
            except OSError: break
    except Closed: pass
    finally:
        try: p.kill()
        except Exception: pass

def svc_sync(st, _):
    def reply(id_, a=0, extra=b''): st.send(id_ + struct.pack('<I', a) + extra)
    while True:
        try: hdr = st.readn(8)
        except Closed: return
        cid, ln = hdr[:4], struct.unpack('<I', hdr[4:])[0]
        if cid == b'QUIT': return
        arg = st.readn(ln).decode('utf8', 'replace') if ln else ''
        if cid in (b'STAT', b'LST2', b'STA2'):
            try: s = os.stat(arg); reply(b'STAT', s.st_mode, struct.pack('<II', s.st_size & 0xffffffff, int(s.st_mtime)))
            except OSError: reply(b'STAT', 0, struct.pack('<II', 0, 0))
        elif cid == b'LIST':
            try:
                for n in sorted(os.listdir(arg)):
                    s = os.lstat(os.path.join(arg, n)); nb = n.encode(); st.send(b'DENT' + struct.pack('<IIII', s.st_mode, s.st_size & 0xffffffff, int(s.st_mtime), len(nb)) + nb)
            except OSError: pass
            st.send(b'DONE' + b'\0' * 16)
        elif cid == b'RECV':
            try:
                with open(arg, 'rb') as f:
                    while True:
                        b = f.read(65536)
                        if not b: break
                        st.send(b'DATA' + struct.pack('<I', len(b)) + b)
                st.send(b'DONE' + struct.pack('<I', 0))
            except OSError as e: msg = str(e).encode(); st.send(b'FAIL' + struct.pack('<I', len(msg)) + msg)
        elif cid == b'SEND':
            path, _, mode = arg.rpartition(','); err = None; f = None
            try: os.makedirs(os.path.dirname(path) or '/', exist_ok=True); f = open(path, 'wb')
            except OSError as e: err = str(e)
            mtime = 0
            while True:
                h = st.readn(8); i, n = h[:4], struct.unpack('<I', h[4:])[0]
                if i == b'DATA':
                    d = st.readn(n)
                    if f and not err:
                        try: f.write(d)
                        except OSError as e: err = str(e)
                elif i == b'DONE': mtime = n; break
                else: err = err or 'protocol error'; break
            if f:
                f.close()
                try: os.chmod(path, int(mode, 8) & 0o7777 if mode.isdigit() else 0o644); os.utime(path, (mtime, mtime))
                except Exception: pass
            if err: m = err.encode(); st.send(b'FAIL' + struct.pack('<I', len(m)) + m)
            else: st.send(b'OKAY' + struct.pack('<I', 0))
        else: m = b'unknown sync command'; st.send(b'FAIL' + struct.pack('<I', len(m)) + m); return

def svc_reboot(st, arg):
    st.send(b'rebooting\n'); time.sleep(0.5); st.close(); subprocess.call('sync', shell=True)
    libc = ctypes.CDLL(None, use_errno=True); libc.syscall(88, 0xfee1dead, 672274793, 0xA1B2C3D4, ctypes.c_char_p(arg.encode()))
    time.sleep(5); subprocess.call('reboot -f', shell=True)

def svc_text(text):
    def f(st, _): st.send(text.encode()); st.close()
    return f

def service(dest):
    d = dest.rstrip('\0')
    if d.startswith('shell:') or d.startswith('exec:'): return lambda st, _: svc_shell(st, d.split(':', 1)[1])
    if d.startswith('sync:'): return svc_sync
    if d.startswith('reboot:'): return lambda st, _: svc_reboot(st, d.split(':', 1)[1])
    if d.startswith('root:'): return svc_text('adbd is already running as root\n')
    if d.startswith('remount:'): return svc_text('remount succeeded\n')
    if d.startswith('usb:'): return svc_text('restarting in USB mode\n')
    if d.startswith('tcpip:'): return svc_text('adb over WiFi is controlled in Settings > ADB\n')
    return None

# ---------------------------------------------------------------------------------------------------------------- connection
class Conn:
    def __init__(self, rd, wr, maxdata): self.rd = rd; self.wr = wr; self.maxdata = maxdata; self.streams = {}; self.lock = threading.Lock(); self.nextid = 1
    def send(self, cmd, a0, a1, data=b''):
        with self.lock:
            self.wr(struct.pack('<IIIIII', cmd, a0, a1, len(data), sum(data) & 0xffffffff, cmd ^ 0xffffffff))
            if data: self.wr(data)
    def loop(self):
        try:
            while True:
                h = self.rd(24); cmd, a0, a1, ln, _crc, magic = struct.unpack('<IIIIII', h)
                if magic != cmd ^ 0xffffffff: raise IOError('bad magic')
                data = self.rd(ln) if ln else b''
                if cmd == A_CNXN: self.send(A_CNXN, VERSION, self.maxdata, BANNER); log('connected', data[:60])
                elif cmd == A_OPEN:
                    svc = service(data.decode('utf8', 'replace'))
                    if not svc: self.send(A_CLSE, 0, a0); continue
                    sid = self.nextid; self.nextid += 1; st = Stream(self, sid, a0); self.streams[sid] = st
                    self.send(A_OKAY, sid, a0)
                    def run(st=st, svc=svc, dest=data.decode('utf8', 'replace').rstrip('\0')):
                        try: svc(st, dest)
                        except Closed: pass
                        except Exception as e: log('service error', dest, repr(e))
                        finally: st.close()
                    threading.Thread(target=run, daemon=True).start()
                elif cmd == A_WRTE:
                    st = self.streams.get(a1)
                    if st: st.inq.put(data); self.send(A_OKAY, a1, a0)
                elif cmd == A_OKAY:
                    st = self.streams.get(a1)
                    if st: st.ack.set()
                elif cmd == A_CLSE:
                    st = self.streams.get(a1)
                    if st: st.closed = True; st.inq.put(None); st.ack.set(); self.streams.pop(a1, None)
                elif cmd == A_AUTH: self.send(A_CNXN, VERSION, self.maxdata, BANNER)
        except (IOError, OSError, Closed, struct.error) as e: log('connection ended', repr(e))
        finally:
            for st in list(self.streams.values()): st.closed = True; st.inq.put(None); st.ack.set()

# ---------------------------------------------------------------------------------------------------------------- transports
def tcp_server(flag):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); host = '0.0.0.0' if open(flag).read().strip() == 'lan' else '127.0.0.1'      # LAN only when the flag file says so (no authentication!)
    s.bind((host, 5555)); s.listen(2); s.settimeout(2); log('adb tcp listening on', host, 5555)
    while os.path.exists(flag):
        try: c, addr = s.accept()
        except socket.timeout: continue
        log('tcp client', addr); c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        def rd(n, c=c):
            b = b''
            while len(b) < n:
                x = c.recv(n - len(b))
                if not x: raise IOError('eof')
                b += x
            return b
        threading.Thread(target=Conn(rd, c.sendall, 65536).loop, daemon=True).start()
    s.close(); log('adb tcp stopped')

G = '/sys/class/android_usb/android0'
def gw(p, v):
    try: open(G + '/' + p, 'w').write(v)
    except Exception as e: log('write', p, v, e)
def usb_descriptors():
    def intf(): return struct.pack('<BBBBBBBBB', 9, 4, 0, 0, 2, 0xff, 0x42, 1, 1)
    def ep(addr, mps): return struct.pack('<BBBBHB', 7, 5, addr, 2, mps, 0)
    fs = intf() + ep(0x01, 64) + ep(0x82, 64); hs = intf() + ep(0x01, 512) + ep(0x82, 512)
    body = struct.pack('<II', 2, 2) + fs + hs                                   # fs_count, hs_count
    descs = struct.pack('<II', 1, 12 + len(body) - 8 + 0) + body                # placeholder, fixed below
    descs = struct.pack('<III', 1, 12 + 8 + len(fs) + len(hs) - 0, 2)[:8] + struct.pack('<II', 2, 2) + fs + hs
    total = 8 + 8 + len(fs) + len(hs)
    descs = struct.pack('<II', 1, total) + struct.pack('<II', 3, 3) + fs + hs   # 3 descriptors per speed (interface + 2 endpoints)
    strings = struct.pack('<IIII', 2, 16 + 2 + len(b'ADB Interface\0'), 1, 1) + struct.pack('<H', 0x0409) + b'ADB Interface\0'
    return descs, strings
def usb_server(flag):
    mnt = '/dev/usb-ffs/adb'; os.makedirs(mnt, exist_ok=True)
    while os.path.ismount(mnt):
        if subprocess.call(['umount', mnt]): break                            # always start from a fresh FunctionFS instance (also clears stacked mounts)
    subprocess.call(['mount', '-t', 'functionfs', 'adb', mnt])
    # the Android gadget wants the function enabled first; adbd then writes the descriptors, which binds the function
    gw('enable', '0'); gw('f_ffs/aliases', 'adb'); gw('functions', 'adb,acm'); gw('idProduct', '4ee2'); gw('enable', '1'); log('gadget: adb,acm')
    time.sleep(1); ep0 = os.open(mnt + '/ep0', os.O_RDWR); d, s = usb_descriptors(); os.write(ep0, d); os.write(ep0, s); log('ffs descriptors written')
    time.sleep(1); out = os.open(mnt + '/ep1', os.O_RDWR); inn = os.open(mnt + '/ep2', os.O_RDWR)
    def rd(n):
        b = b''; zeros = 0
        while len(b) < n:
            x = os.read(out, n - len(b))
            if not x:                                    # zero-length packet: the host ends a transfer that is a multiple of the packet size this way
                zeros += 1
                if zeros > 200: raise IOError('eof')
                continue
            zeros = 0; b += x
        return b
    log('usb adb ready'); c = Conn(rd, lambda b: os.write(inn, b), 16384)
    t = threading.Thread(target=c.loop, daemon=True); t.start()
    while os.path.exists(flag) and t.is_alive(): time.sleep(1)
    for fd in (out, inn, ep0):
        try: os.close(fd)
        except Exception: pass
    if not os.path.exists(flag):
        gw('enable', '0'); gw('functions', 'acm'); gw('idProduct', 'd002'); gw('enable', '1'); log('usb adb stopped, gadget back to acm')
    subprocess.call(['umount', mnt])

def usb_guard(flag):
    """run usb_server, and on failure wait before the supervisor retries (so a failing setup cannot loop and stack mounts)"""
    try: usb_server(flag)
    except Exception as e: log('usb server failed', repr(e)); time.sleep(20)

def main():
    running = {}
    while True:
        for name, flag, fn in (('tcp', DATA + '/adb-tcp', tcp_server), ('usb', DATA + '/adb-usb', usb_guard)):
            th = running.get(name)
            if os.path.exists(flag) and (th is None or not th.is_alive()):
                th = threading.Thread(target=lambda fn=fn, flag=flag: fn(flag), daemon=True); th.start(); running[name] = th
        time.sleep(3)
if __name__ == '__main__': main()
