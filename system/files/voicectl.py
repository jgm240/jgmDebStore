#!/usr/bin/env python3
"""jgmDeb: set the DSP voice controls (own work): "Voice Rx Device Mute", "Voice Tx Mute", "Voice Rx Gain" take three integers
(value, session id, ramp ms); the session id for "all sessions" is 0xFFFFFFFF. They are write-only and declare a range of 0..-1, so amixer
cannot set them (it reads them first, and clamps every value into that broken range). This writes them directly, like the stock audio HAL.
usage: voicectl.py "Voice Rx Gain=3,-1,20" ["Voice Tx Mute=0,-1,20" ...]   -> exit 0 if all were written"""
import ctypes, sys
a = ctypes.CDLL('libasound.so.2'); a.snd_strerror.restype = ctypes.c_char_p
ctl = ctypes.c_void_p()
r = a.snd_ctl_open(ctypes.byref(ctl), b'hw:0', 0)
if r < 0: print('open: %s' % a.snd_strerror(r).decode()); sys.exit(2)
bad = 0
for arg in sys.argv[1:]:
    name, _, vals = arg.partition('=')
    v = ctypes.c_void_p(); a.snd_ctl_elem_value_malloc(ctypes.byref(v))
    a.snd_ctl_elem_value_set_interface(v, 2)                     # SND_CTL_ELEM_IFACE_MIXER
    a.snd_ctl_elem_value_set_name(v, name.encode())
    for i, x in enumerate(vals.split(',')): a.snd_ctl_elem_value_set_integer(v, i, ctypes.c_long(int(x)))
    r = a.snd_ctl_elem_write(ctl, v); a.snd_ctl_elem_value_free(v)
    if r < 0: print('%s: %s' % (name, a.snd_strerror(r).decode())); bad += 1
a.snd_ctl_close(ctl)
sys.exit(1 if bad else 0)
