#!/usr/bin/env python3
"""jgmDeb: what Android's irsc_util does (own work): give the kernel's IPC router its security rules so it stops holding back every send.
One default rule (all services, all instances) allowing root and the Android network groups."""
import socket, fcntl, struct, sys
s = socket.socket(27, socket.SOCK_DGRAM, 0)
gids = [0, 1000, 1001, 1002, 3001, 3002, 3003, 3004, 3005]
buf = struct.pack('<iIII', len(gids), 0xFFFFFFFF, 0xFFFFFFFF, 0) + b''.join(struct.pack('<I', g) for g in gids)
try: fcntl.ioctl(s.fileno(), 0x8010C305, buf); print('ipc router security rules set')
except OSError as e: print('irsc failed:', e); sys.exit(1)
