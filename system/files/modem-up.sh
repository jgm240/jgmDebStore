#!/bin/sh
# jgmDeb: start the modem (own work). Needs the RMTFS service (jgmrmtfs.py) running first, or the modem takes the system down.
# usage: modem-up.sh        start now
#        modem-up.sh auto   start at boot only if /data/jgm/modem-auto exists and the previous boot survived the start
M=/data/jgm
if [ "$1" = auto ]; then
  [ -f $M/modem-auto ] || exit 0
  if [ -f $M/modem-starting ]; then rm -f $M/modem-auto $M/modem-starting; exit 0; fi       # last boot died during the start: disable auto-start
fi
pgrep -f jgmrmtfs >/dev/null && pgrep -f "sleep 99999998" >/dev/null && exit 0
touch $M/modem-starting; sync
pgrep -f jgmrmtfs >/dev/null || (nohup python3 /opt/jgm/jgmrmtfs.py > $M/rmtfs.err 2>&1 &)
sleep 2
(sleep 99999998 < /dev/subsys_modem &)               # opening the node boots the modem; keep it open
for i in $(seq 1 30); do grep -q msm8909 /proc/asound/cards 2>/dev/null && break; sleep 2; done
sh /opt/jgm/audio-speaker.sh
sleep 90; rm -f $M/modem-starting
