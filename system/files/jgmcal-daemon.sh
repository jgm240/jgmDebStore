#!/bin/sh
# jgmDeb: start / talk to the voice calibration service (uses the stock Android loader from /system, see jgmcal.c). usage: jgmcal-daemon.sh start | send RX TX
# exit codes: 0 ok, 1 service not running / not answering, 2 the loader reported an error for this device pair
F=/run/jgmcal.fifo; LOG=/run/jgmcal.log
up() { pgrep -f "^/system/bin/tinymix" >/dev/null && grep -q "^ready" $LOG 2>/dev/null; }
case "$1" in
  start)
    up && exit 0
    if ! pgrep -f "^/system/bin/tinymix" >/dev/null; then
      [ -p $F ] || { rm -f $F; mkfifo $F; }
      : > $LOG
      (LD_LIBRARY_PATH=/system/lib:/vendor/lib LD_PRELOAD=/opt/jgm/jgmcal.so CARD=msm8909-skub-snd-card CVD=2.3 FIFO=$F nohup /system/bin/tinymix > $LOG 2>&1 &)
    fi
    for i in $(seq 1 30); do up && exit 0; sleep 0.5; done; exit 1 ;;
  send)
    sh $0 start || exit 1
    N=$(grep -c "^sent" $LOG)
    timeout 3 sh -c "echo '$2 $3' > $F" || exit 1                 # never block on a FIFO nobody reads
    for i in $(seq 1 20); do
      if [ "$(grep -c '^sent' $LOG)" -gt "$N" ]; then grep "^sent" $LOG | tail -n 1 | grep -q -- "-> 0$" && exit 0; exit 2; fi
      sleep 0.25
    done; exit 1 ;;
esac
