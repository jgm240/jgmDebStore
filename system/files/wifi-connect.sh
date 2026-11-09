#!/bin/sh
# jgmDeb: (re)start wpa_supplicant + DHCP on wlan0 using /data/jgm/wpa.conf (own work). usage: wifi-connect.sh [stop]
pkill wpa_supplicant 2>/dev/null; pkill -f "udhcpc -i wlan0" 2>/dev/null
[ "$1" = stop ] && { ip addr flush dev wlan0 2>/dev/null; ip link set wlan0 down; exit 0; }
[ -f /data/jgm/wpa.conf ] || exit 1
mkdir -p /usr/share/udhcpc /run/wpa
[ -f /usr/share/udhcpc/default.script ] || cat > /usr/share/udhcpc/default.script <<'EOS'
#!/bin/sh
case "$1" in
  bound|renew)
    ip addr flush dev $interface; ip addr add $ip/${mask:-24} dev $interface
    [ -n "$router" ] && ip route replace default via ${router%% *} dev $interface
    : > /etc/resolv.conf; for d in $dns; do echo "nameserver $d" >> /etc/resolv.conf; done ;;
  deconfig) ip addr flush dev $interface ;;
esac
EOS
chmod +x /usr/share/udhcpc/default.script
ip link set wlan0 up
wpa_supplicant -B -i wlan0 -D wext -c /data/jgm/wpa.conf -C /run/wpa
busybox udhcpc -i wlan0 -b -q -t 8 -s /usr/share/udhcpc/default.script >/dev/null 2>&1 &
# set the clock (the phone has no RTC battery time): needed for TLS / apt
(for i in 1 2 3 4 5 6 7 8; do ip -4 addr show wlan0 | grep -q inet && break; sleep 2; done; python3 /opt/jgm/ntpset.py >/dev/null 2>&1) &
