#!/usr/bin/env python3
"""jgmDeb: set the system clock from NTP (SNTP over UDP); the phone has no battery-backed time."""
import socket, struct, time, sys
for host in ('pool.ntp.org', '162.159.200.1', 'time.cloudflare.com'):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4)
        s.sendto(b'\x1b' + 47 * b'\0', (host, 123)); d = s.recv(48)
        t = struct.unpack('!I', d[40:44])[0] - 2208988800
        if t > 1.7e9: time.clock_settime(time.CLOCK_REALTIME, t); print('time set', time.ctime()); sys.exit(0)
    except Exception as e: print(host, e)
sys.exit(1)
