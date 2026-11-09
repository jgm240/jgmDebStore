#!/bin/sh
# jgmDeb: route PCM playback (MultiMedia1) to the loudspeaker, from the stock "speaker" path (own re-implementation of the routing).
c() { amixer -q -c 0 cset name="$1" "$2" 2>/dev/null; }
c "MI2S_RX Channels" One
c "RX3 Digital Volume" 40
c "PRI_MI2S_RX Audio Mixer MultiMedia1" 1
c "RX3 MIX1 INP1" RX1
c "SPK DAC Switch" 1
c "Speaker Boost" ENABLE
