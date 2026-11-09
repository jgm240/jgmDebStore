#!/bin/sh
# jgmDeb WiFi bring-up (own work), following the stock Qualcomm sequence: tell the WCNSS platform driver that user space is ready
# (write 1 to /dev/wcnss_wlan), plumb the serial number, send the control messages, load the vendor driver.
# Vendor firmware/driver files live in /lib/firmware and /root/wifi (not redistributed).
[ -e /sys/class/net/wlan0 ] && exit 0
echo 1 > /dev/wcnss_wlan
SN=$(printf '%x' $(( 0x$(md5sum /proc/cpuinfo | cut -c1-7) )))
echo $SN > /sys/devices/soc.0/a000000.qcom,wcnss-wlan/serial_number 2>/dev/null
pgrep -f "sleep 99999999" >/dev/null || (sleep 99999999 < /dev/subsys_wcnss &)       # make sure the WCNSS firmware is booted
sleep 5
python3 /opt/jgm/wcnss_ctrl.py
sleep 2
insmod /root/wifi/pronto_wlan.ko
[ -f /data/jgm/wpa.conf ] && sh /opt/jgm/wifi-connect.sh
