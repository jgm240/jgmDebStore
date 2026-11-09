#!/bin/sh
# jgmDeb WiFi hotspot (own work): hostapd + dnsmasq-less DHCP via busybox udhcpd. usage: hotspot.sh start|stop
PASS=$(cat /data/jgm/hotspot-pass 2>/dev/null || echo jgmdeb1234)
case "$1" in
stop) pkill hostapd; pkill -f "udhcpd /tmp/udhcpd.conf"; ip addr flush dev wlan0 2>/dev/null; sh /opt/jgm/wifi-connect.sh >/dev/null 2>&1 & exit 0 ;;
esac
which hostapd >/dev/null || { echo "hostapd not installed"; exit 1; }
pkill wpa_supplicant; pkill -f "udhcpc -i wlan0"; sleep 1
cat > /tmp/hostapd.conf <<EOC
interface=wlan0
driver=nl80211
ssid=jgmDeb
hw_mode=g
channel=6
wpa=2
wpa_passphrase=$PASS
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOC
ip link set wlan0 up; ip addr add 192.168.43.1/24 dev wlan0
hostapd -B /tmp/hostapd.conf 2>&1 | tail -3
printf 'start 192.168.43.20\nend 192.168.43.60\ninterface wlan0\nopt router 192.168.43.1\nopt dns 1.1.1.1\n' > /tmp/udhcpd.conf
busybox udhcpd /tmp/udhcpd.conf 2>&1 | tail -1
pgrep hostapd >/dev/null && echo "hotspot up" || echo "hostapd failed (driver may not support AP mode)"
