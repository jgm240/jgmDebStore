#!/usr/bin/env python3
"""jgmDeb: RMTFS server (own work). The MSM8909 modem keeps its calibration/NV data (EFS) in eMMC partitions and reads/writes it through a
QMI "remote file system" service (id 14) that Android's rmt_storage daemon provides. This does the same over the IPC router (AF_MSM_IPC):

  OPEN(1) path -> caller id; CLOSE(2); RW_IOVEC(3) read/write sectors between the partition and the shared-memory UIO region ('rmtfs');
  ALLOC_BUFF(4) -> address of that region; GET_DEV_ERROR(5).
Paths the modem uses: /boot/modem_fs1 -> modemst1 (p3), modem_fs2 -> modemst2 (p4), modem_fsg -> fsg (p24), modem_fsc -> fsc (p5), modem_tng -> tunning (p2).
Must be running before the modem is booted (open /dev/subsys_modem)."""
import os, sys, struct, socket, ctypes, ctypes.util, mmap, glob, time, syslog

AF_MSM_IPC = 27; SERVICE = 14; INSTANCE = 1
PARTS = {'modem_fs1': '/dev/mmcblk0p3', 'modem_fs2': '/dev/mmcblk0p4', 'modem_fsg': '/dev/mmcblk0p24', 'modem_fsc': '/dev/mmcblk0p5', 'modem_tng': '/dev/mmcblk0p2'}
libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)
LOG = '/data/jgm/rmtfs.log'
def log(*a):
    try:
        f = open(LOG, 'a'); f.write(time.strftime('%H:%M:%S ') + ' '.join(str(x) for x in a) + '\n'); f.flush(); os.fsync(f.fileno()); f.close()
    except Exception: pass

def uio_region():
    for u in glob.glob('/sys/class/uio/uio*'):
        if open(u + '/name').read().strip() == 'rmtfs':
            return '/dev/' + os.path.basename(u), int(open(u + '/maps/map0/addr').read(), 16), int(open(u + '/maps/map0/size').read(), 16)
    raise SystemExit('rmtfs uio region not found')

def tlvs(b):
    out = {}; i = 0
    while i + 3 <= len(b):
        t, n = struct.unpack_from('<BH', b, i); out[t] = b[i + 3:i + 3 + n]; i += 3 + n
    return out
def tlv(t, v): return struct.pack('<BH', t, len(v)) + v
def result(err=0): return tlv(2, struct.pack('<HH', 0 if err == 0 else 1, err))

def main():
    dev, base, size = uio_region()
    ufd = os.open(dev, os.O_RDWR); mem = mmap.mmap(ufd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0)
    s = socket.socket(AF_MSM_IPC, socket.SOCK_DGRAM, 0)
    addr = struct.pack('<H2xB3xIIB3x', AF_MSM_IPC, 1, SERVICE, INSTANCE, 0)               # addrtype 1 = NAME (service, instance)
    if libc.bind(s.fileno(), addr, len(addr)) != 0: raise SystemExit('bind failed errno %d' % ctypes.get_errno())
    log('serving RMTFS service %d inst %d; shared mem %s phys 0x%x size 0x%x' % (SERVICE, INSTANCE, dev, base, size))
    files = {}; nextid = 1; last_err = {}
    buf = ctypes.create_string_buffer(8192); src = ctypes.create_string_buffer(64)
    while True:
        sl = ctypes.c_uint(64)
        n = libc.recvfrom(s.fileno(), buf, 8192, 0, src, ctypes.byref(sl))
        if n < 0:
            if ctypes.get_errno() == 4: continue
            log('recvfrom errno', ctypes.get_errno()); time.sleep(0.5); continue
        d = buf.raw[:n]
        if n < 7: continue
        fl, txn, mid, ln = struct.unpack_from('<BHHH', d, 0); t = tlvs(d[7:7 + ln]); body = b''
        try:
            if mid == 1:                                                                    # OPEN
                path = t.get(1, b'').split(b'\0')[0].decode(); name = os.path.basename(path); dev_path = PARTS.get(name)
                if not dev_path: log('open unknown', path); body = result(1)
                else:
                    fd = os.open(dev_path, os.O_RDWR); cid = nextid; nextid += 1; files[cid] = fd; last_err[cid] = 0
                    log('open', path, '->', dev_path, 'id', cid); body = result() + tlv(0x10, struct.pack('<I', cid))
            elif mid == 2:                                                                  # CLOSE
                cid = struct.unpack('<I', t[1][:4])[0]; fd = files.pop(cid, None)
                if fd is not None: os.fsync(fd); os.close(fd)
                log('close', cid); body = result()
            elif mid == 3:                                                                  # RW_IOVEC
                cid = struct.unpack('<I', t[1][:4])[0]; direction = t[2][0]; cnt = t[3][0]; fd = files.get(cid); err = 0
                if fd is None: err = 1
                else:
                    for i in range(cnt):
                        sec, phys, num = struct.unpack_from('<III', t[3], 1 + 12 * i); off = phys - base if phys >= base else phys; ln_b = num * 512      # the modem may pass offsets into the shared region
                        if off < 0 or off + ln_b > size: err = 1; log('iovec out of range', hex(phys), num); break
                        os.lseek(fd, sec * 512, os.SEEK_SET)
                        if direction == 0: data = os.read(fd, ln_b); mem[off:off + len(data)] = data.ljust(ln_b, b'\0')[:ln_b]
                        else: os.write(fd, bytes(mem[off:off + ln_b]))
                    if direction == 1 and fd is not None: os.fsync(fd)
                last_err[cid] = err; log('rw', cid, 'W' if direction else 'R', cnt, 'err', err); body = result(err)
            elif mid == 4:                                                                  # ALLOC_BUFF
                log('alloc tlvs', {k: v.hex() for k, v in t.items()}); sz = struct.unpack('<I', t[2][:4])[0] if 2 in t else 0; body = result() + tlv(0x10, struct.pack('<Q', base))
            elif mid == 5:                                                                  # GET_DEV_ERROR
                cid = struct.unpack('<I', t[1][:4])[0]; body = result() + tlv(0x10, bytes([last_err.get(cid, 0)]))
            else: log('unknown msg', hex(mid)); body = result(3)
        except Exception as e:
            log('error', mid, repr(e)); body = result(1)
        resp = struct.pack('<BHHH', 2, txn, mid, len(body)) + body
        libc.sendto(s.fileno(), resp, len(resp), 0, src, sl.value)

if __name__ == '__main__':
    main()
