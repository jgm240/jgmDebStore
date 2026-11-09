#!/bin/sh
# jgmDeb: SSH server supervisor (own work). Settings: /data/jgm/ssh-off (exists = disabled), /data/jgm/ssh-mode ("root" = unsandboxed, else sandboxed).
# Sandboxed: root login is refused and you land in the unprivileged user "jgm". Unsandboxed: root login allowed.
# The UI changes the files and kills dropbear; this loop restarts it with the new options.
while :; do
  if [ -f /data/jgm/ssh-off ]; then sleep 2; continue; fi
  if [ "$(cat /data/jgm/ssh-mode 2>/dev/null)" = root ]; then OPT=""; else OPT="-w"; fi
  /usr/sbin/dropbear -F -E -p 22 $OPT >/var/log/dropbear.log 2>&1
  sleep 1
done
