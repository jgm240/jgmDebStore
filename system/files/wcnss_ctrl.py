#!/usr/bin/env python3
"""Send wcnss_service's user messages to /dev/wcnss_ctrl: [type u16 BE][data]. 1 = serial (u32 BE), 2 = has cal data (u8), 3 = MAC (6)."""
import os, time, hashlib
fd = os.open('/dev/wcnss_ctrl', os.O_WRONLY)
serial = int(hashlib.md5((open('/proc/cpuinfo').read()).encode()).hexdigest()[:8], 16)
mac = bytes([0x02, 0x6a, 0x67, 0x6d]) + hashlib.md5(open('/proc/cpuinfo').read().encode()).digest()[:2]
for b in (bytes([0, 1]) + serial.to_bytes(4, 'big'), bytes([0, 2, 0]), bytes([0, 3]) + mac):
    try: os.write(fd, b)
    except OSError as e: print('wcnss_ctrl write', len(b), e)
    time.sleep(0.1)
time.sleep(1)
os.close(fd)
