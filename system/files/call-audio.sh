#!/bin/sh
# jgmDeb: in-call audio routing (own re-implementation of the stock "voice-call" + handset/speaker paths). usage: call-audio.sh handset|speaker|off
c() { amixer -q -c 0 cset name="$1" "$2" 2>/dev/null; }
stop() { pkill -f "aplay -D hw:0,2" 2>/dev/null; pkill -f "arecord -D hw:0,2" 2>/dev/null; }
off() {
  stop
  c "PRI_MI2S_RX_Voice Mixer CSVoice" 0; c "Voice_Tx Mixer TERT_MI2S_TX_Voice" 0
  c "RX1 MIX1 INP1" ZERO; c "RX3 MIX1 INP1" ZERO; c "RDAC2 MUX" ZERO; c "EAR_S Switch" 0; c "SPK DAC Switch" 0; c "Speaker Boost" DISABLE
  c "DEC1 MUX" ZERO; c "IIR1 INP1 MUX" ZERO
}
case "$1" in
  off) off ;;
  handset|speaker)
    off
    c "MI2S_RX Channels" One; c "MI2S_TX Channels" One
    c "PRI_MI2S_RX_Voice Mixer CSVoice" 1; c "Voice_Tx Mixer TERT_MI2S_TX_Voice" 1
    c "ADC1 Volume" 6; c "DEC1 MUX" ADC1; c "IIR1 INP1 MUX" DEC1                     # microphone
    if [ "$1" = handset ]; then c "RX1 MIX1 INP1" RX1; c "RDAC2 MUX" RX1; c "EAR_S" Switch
    else c "RX3 Digital Volume" 60; c "RX3 MIX1 INP1" RX1; c "SPK DAC Switch" 1; c "Speaker Boost" ENABLE; fi
    # the voice session on the DSP starts when the CS-Voice PCM device (hw:0,2) is opened for playback and capture
    (nohup aplay -D hw:0,2 -t raw -f S16_LE -r 8000 -c 1 /dev/zero >/dev/null 2>&1 &)
    (nohup arecord -D hw:0,2 -t raw -f S16_LE -r 8000 -c 1 /dev/null >/dev/null 2>&1 &) ;;
esac
