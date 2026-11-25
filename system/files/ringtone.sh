#!/bin/sh
# jgmDeb: incoming-call ringtone on the loudspeaker + vibration (own work; the tone is synthesised here).
# usage: ringtone.sh start [sound|nosound] [vibrate|novibrate]  |  ringtone.sh stop
W=/opt/jgm/ringtone.wav; VIB=/sys/class/timed_output/vibrator/enable
case "$1" in
  start)
    sh $0 stop
    if [ "$2" != nosound ]; then
      [ -f $W ] || python3 - <<PY
import wave, struct, math
R = 48000
def note(f, dur, amp=0.45):
    n = int(R * dur); out = []
    for i in range(n):
        t = i / R; env = min(1.0, i / 240.0) * math.exp(-4.0 * t)        # soft attack, bell-like decay
        v = math.sin(2 * math.pi * f * t) + 0.35 * math.sin(2 * math.pi * 2 * f * t) + 0.12 * math.sin(2 * math.pi * 3 * f * t)
        out.append(int(32767 * amp * env * v / 1.47))
    return out
s = []
for _ in range(2):
    for f in (1318.5, 1568.0, 2093.0, 1568.0): s += note(f, 0.18)
    s += [0] * int(R * 0.25)
s += [0] * int(R * 1.2)
w = wave.open("$W", "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(R)
w.writeframes(b"".join(struct.pack("<h", max(-32767, min(32767, x))) for x in s)); w.close()
PY
      sh /opt/jgm/audio-speaker.sh
      (nohup sh -c "while :; do aplay -q -D hw:0,0 $W; done" >/dev/null 2>&1 &)
    fi
    if [ "$3" != novibrate ] && [ -w $VIB ]; then
      (nohup sh -c "while :; do echo 800 > $VIB; sleep 2.4; done" >/dev/null 2>&1 &)
    fi ;;
  stop)
    pkill -f "do aplay -q -D hw:0,0 $W" 2>/dev/null          # the loops first, or they start the next round
    pkill -f "do echo 800 > $VIB" 2>/dev/null
    pkill -f "aplay -q -D hw:0,0 $W" 2>/dev/null
    [ -w $VIB ] && echo 0 > $VIB
    exit 0 ;;
esac
