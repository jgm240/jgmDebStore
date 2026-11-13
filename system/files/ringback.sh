#!/bin/sh
# jgmDeb: local ringback tone while the far end is ringing (own work). usage: ringback.sh start|stop
# 425 Hz, 1 s on / 5 s off (the European pattern), played through the same earpiece/speaker path as the call.
W=/opt/jgm/ringback.wav
case "$1" in
  start)
    [ -f $W ] || python3 - <<PY
import wave, struct, math
w = wave.open("$W", "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
on = b"".join(struct.pack("<h", int(4000 * math.sin(2 * math.pi * 425 * i / 48000) * min(1, i / 2400, (48000 - i) / 2400))) for i in range(48000))
w.writeframes(on + b"\0\0" * 48000 * 5); w.close()
PY
    sh $0 stop
    amixer -q -c 0 cset name="PRI_MI2S_RX Audio Mixer MultiMedia1" 1
    (nohup sh -c "while :; do aplay -q -D hw:0,0 $W; done" >/dev/null 2>&1 &) ;;
  stop)
    pkill -f "aplay -q -D hw:0,0 $W" 2>/dev/null
    for p in $(pgrep -f "while :; do aplay -q -D hw:0,0"); do kill $p 2>/dev/null; done
    pkill -f "aplay -q -D hw:0,0" 2>/dev/null
    amixer -q -c 0 cset name="PRI_MI2S_RX Audio Mixer MultiMedia1" 0 ;;
esac
