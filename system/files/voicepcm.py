#!/usr/bin/env python3
"""jgmDeb: keep the CS-Voice PCM device open while a call has audio (own work).

The DSP's voice session starts when the voice front end (ALSA hw:0,2) is prepared/started for playback AND capture, and ends when both are
closed. It is "hostless": no samples ever pass through it, the audio goes modem <-> DSP <-> codec directly. Android just pcm_start()s the two
streams and leaves them alone. aplay/arecord instead wait for data that never comes and quit after ALSA's ~10 s I/O timeout, which closes
the device and tears the voice session down - so a call answered after ~10 s of ringing had no audio. This opens both directions with
no stop threshold (an empty buffer is never an xrun), starts them, and then only sleeps until it is killed.
usage: voicepcm.py [hw:0,2]"""
import ctypes, signal, sys, time

dev = (sys.argv[1] if len(sys.argv) > 1 else 'hw:0,2').encode()
a = ctypes.CDLL('libasound.so.2')
a.snd_strerror.restype = ctypes.c_char_p
U = ctypes.c_ulong                                  # snd_pcm_uframes_t

def chk(r, what):
    if r < 0: raise OSError('%s: %s' % (what, a.snd_strerror(r).decode()))
    return r

def open_stream(stream):
    p = ctypes.c_void_p()
    chk(a.snd_pcm_open(ctypes.byref(p), dev, stream, 0), 'open %s' % ('playback', 'capture')[stream])
    chk(a.snd_pcm_set_params(p, 2, 3, 1, 8000, 0, 40000), 'set params')        # S16_LE, RW interleaved, mono, 8 kHz, 40 ms
    sw = ctypes.c_void_p(); chk(a.snd_pcm_sw_params_malloc(ctypes.byref(sw)), 'sw malloc')
    chk(a.snd_pcm_sw_params_current(p, sw), 'sw current')
    b = U(); chk(a.snd_pcm_sw_params_get_boundary(sw, ctypes.byref(b)), 'boundary')
    chk(a.snd_pcm_sw_params_set_stop_threshold(p, sw, b), 'stop threshold')      # never stop on "xrun"
    if stream == 0: chk(a.snd_pcm_sw_params_set_silence_size(p, sw, b), 'silence')
    chk(a.snd_pcm_sw_params(p, sw), 'sw params'); a.snd_pcm_sw_params_free(sw)
    # (snd_pcm_set_params already prepared the stream; preparing again makes the driver try to start the voice session twice)
    r = a.snd_pcm_start(p)
    if r < 0: print('start %s: %s (prepared is enough for the voice session)' % (('playback', 'capture')[stream], a.snd_strerror(r).decode()), flush=True)
    return p

pcms = []
def bye(*_):
    for p in pcms:
        try: a.snd_pcm_close(p)
        except Exception: pass
    sys.exit(0)
signal.signal(signal.SIGTERM, bye); signal.signal(signal.SIGINT, bye)
try:
    pcms.append(open_stream(0)); pcms.append(open_stream(1))
except OSError as e:
    print('voicepcm: %s' % e, flush=True); bye()
print('voicepcm: running', flush=True)
while True: signal.pause()
