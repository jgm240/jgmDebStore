#!/bin/sh
# jgmDeb: in-call audio routing (own re-implementation of the stock "voice-call" + handset/speaker paths).
# usage: call-audio.sh handset|speaker|off      route the call and start / stop the DSP voice session
#        call-audio.sh volume 0..5              in-call volume (DSP voice RX gain)
# Runs one at a time (lock); a newer request makes a running one give up early. Each call's kernel audio messages go to /data/jgm/call-kernel.log.
MODE=/run/callaudio.mode; LOCK=/run/callaudio.lock; LOG=/data/jgm/call-debug.log; KLOG=/data/jgm/call-kernel.log; K0=/run/callaudio.k0
DD=/sys/kernel/debug/dynamic_debug/control
log() { echo "$(date +%T) audio: $*" >> $LOG; }
c() { amixer -q -c 0 cset name="$1" "$2" 2>/dev/null || log "mixer: cannot set '$1' = $2"; }
alive() { pgrep -f "voicepcm.py" >/dev/null || pgrep -f "aplay -D hw:0,2" >/dev/null || pgrep -f "arecord -D hw:0,2" >/dev/null; }
stop_pcm() {
  pkill -f "voicepcm.py" 2>/dev/null; pkill -f "aplay -D hw:0,2" 2>/dev/null; pkill -f "arecord -D hw:0,2" 2>/dev/null
  for i in 1 2 3 4 5 6 7 8 9 10; do alive || break; sleep 0.2; done          # the device must really be closed before it is opened again
}
levels() {                      # the DSP voice paths start muted / at the lowest volume: unmute and set levels like the stock config (mute flag, session -1 = all, ramp ms)
  amixer -q -c 0 cset name="Voice Rx Device Mute" 0,-1,20 2>/dev/null || log "rx unmute failed"
  amixer -q -c 0 cset name="Voice Tx Mute" 0,-1,20 2>/dev/null || log "tx unmute failed"
  amixer -q -c 0 cset name="Voice Rx Gain" "$(cat /run/callaudio.vol 2>/dev/null || echo 3),-1,20" 2>/dev/null || log "rx gain failed"
}
routes_off() {
  c "PRI_MI2S_RX_Voice Mixer CSVoice" 0; c "Voice_Tx Mixer TERT_MI2S_TX_Voice" 0
  c "RX1 MIX1 INP1" ZERO; c "RX3 MIX1 INP1" ZERO; c "RDAC2 MUX" ZERO; c "EAR_S" ZERO; c "EAR PA Gain" POS_1P5_DB; c "SPK DAC Switch" 0; c "Speaker Boost" DISABLE
  c "DEC1 MUX" ZERO; c "IIR1 INP1 MUX" ZERO
}
kdebug() {                      # more kernel detail about the voice session while a call has audio (if the kernel allows it)
  [ -w $DD ] || return 0
  for f in q6voice.c msm-pcm-voice-v2.c; do echo "file $f $1" > $DD 2>/dev/null; done
  return 0
}
kernel_log() {                  # keep this call's audio-related kernel messages
  [ -f $K0 ] || return 0
  N=$(cat $K0); rm -f $K0
  [ -f $KLOG ] && [ "$(wc -c < $KLOG)" -gt 200000 ] && mv $KLOG $KLOG.1
  { echo "=== $(date '+%F %T')"; dmesg | tail -n +$((N + 1)) | grep -iE "voice|voc|cvp|cvs|mvm|apr|afe|adm|q6|pcm|asm|acdb|cal|mi2s|wcd|msm8x16" | grep -vE "IRQ|migrate"; } >> $KLOG
  return 0
}

case "$1" in
  volume)
    echo "$2" > /run/callaudio.vol
    amixer -q -c 0 cset name="Voice Rx Gain" "$2,-1,20" 2>/dev/null || log "volume $2 failed"
    exit 0 ;;
  handset|speaker|off) ;;
  *) echo "usage: $0 handset|speaker|off|volume N"; exit 1 ;;
esac

echo "$1" > $MODE                          # first, so an instance that is still running sees it and stops
if command -v flock >/dev/null; then exec 9>$LOCK; flock -w 60 9; fi
[ "$(cat $MODE 2>/dev/null)" = "$1" ] || exit 0          # superseded while waiting for the lock

if [ "$1" = off ]; then
  stop_pcm; routes_off; kdebug -p; kernel_log; log "off"; exit 0
fi

stop_pcm; routes_off
[ -f $K0 ] || dmesg | wc -l > $K0
kdebug +p
c "MI2S_RX Channels" One; c "MI2S_TX Channels" One
# the stock voice calibration for this device pair (the service keeps it loaded; handset = ACDB ids 7/4, speaker phone = 14/11)
if [ "$1" = handset ]; then R=7; T=4; else R=14; T=11; fi
timeout 8 sh /opt/jgm/jgmcal-daemon.sh send $R $T || log "calibration $R/$T not confirmed"
c "PRI_MI2S_RX_Voice Mixer CSVoice" 1; c "Voice_Tx Mixer TERT_MI2S_TX_Voice" 1
levels
c "ADC1 Volume" 6; c "DEC1 MUX" ADC1; c "IIR1 INP1 MUX" DEC1                     # microphone
if [ "$1" = handset ]; then c "RX1 MIX1 INP1" RX1; c "RDAC2 MUX" RX1; c "RX1 Digital Volume" 84; c "EAR PA Gain" POS_6_DB; c "EAR_S" Switch
else c "RX3 Digital Volume" 60; c "RX3 MIX1 INP1" RX1; c "SPK DAC Switch" 1; c "Speaker Boost" ENABLE; fi

# start the DSP voice session: open the CS-Voice front end (hw:0,2) for playback and capture and keep it open (voicepcm.py).
# Right after the modem boots its voice services are not up yet: the kernel logs "voice_apr_register: Unable to register" and the call has no
# audio -> close and retry.
for try in 1 2 3 4 5 6 7 8; do
  [ "$(cat $MODE 2>/dev/null)" = "$1" ] || { stop_pcm; exit 0; }
  N=$(dmesg | grep -c "voice_apr_register: Unable")
  (nohup python3 /opt/jgm/voicepcm.py hw:0,2 >> /run/voicepcm.log 2>&1 &)
  sleep 1.5
  if ! pgrep -f voicepcm.py >/dev/null; then               # fallback: the old way (works, but quits after ~10 s without data)
    log "voicepcm failed: $(tail -n 1 /run/voicepcm.log 2>/dev/null); using aplay/arecord"
    (nohup aplay -D hw:0,2 -t raw -f S16_LE -r 8000 -c 1 /dev/zero >/dev/null 2>&1 &)
    (nohup arecord -D hw:0,2 -t raw -f S16_LE -r 8000 -c 1 /dev/null >/dev/null 2>&1 &)
    sleep 1.5
  fi
  if [ "$(dmesg | grep -c 'voice_apr_register: Unable')" -le "$N" ]; then
    levels; log "$1 on (try $try)"; exit 0
  fi
  log "voice not ready, retry $try"; stop_pcm; sleep 3
done
log "$1: voice session did not start"
