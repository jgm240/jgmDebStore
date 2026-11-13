#!/bin/sh
# jgmDeb: start / talk to the voice calibration service (uses the stock Android loader from /system, see jgmcal.c). usage: jgmcal-daemon.sh start | send RX TX
F=/run/jgmcal.fifo; LOG=/run/jgmcal.log
case "$1" in
  start)
    pgrep -f "tinymix" >/dev/null && exit 0
    [ -e $F ] || mkfifo $F
    : > $LOG
    (LD_LIBRARY_PATH=/system/lib:/vendor/lib LD_PRELOAD=/opt/jgm/jgmcal.so CARD=msm8909-skub-snd-card CVD=2.3 FIFO=$F nohup /system/bin/tinymix > $LOG 2>&1 &)
    for i in $(seq 1 30); do grep -q "^ready" $LOG 2>/dev/null && exit 0; sleep 0.5; done; exit 1 ;;
  send)
    sh $0 start || exit 1
    N=$(grep -c "^sent" $LOG); echo "$2 $3" > $F
    for i in $(seq 1 20); do [ "$(grep -c '^sent' $LOG)" -gt "$N" ] && exit 0; sleep 0.25; done; exit 1 ;;
esac
