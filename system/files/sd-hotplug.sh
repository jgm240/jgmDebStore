#!/bin/sh
# jgmDeb: SD card automount (own work). Called by mdev (see /etc/mdev.conf) with ACTION and MDEV, or by hand: sd-hotplug.sh scan
M=/media/sd
mnt() {
  mkdir -p $M; mountpoint -q $M && return
  mount -t vfat -o rw,noatime,umask=000,utf8,flush "$1" $M 2>/dev/null || mount -t ext4 -o rw,noatime "$1" $M 2>/dev/null || { rmdir $M 2>/dev/null; return 1; }
}
case "$1" in
  scan) for d in /dev/mmcblk1p1 /dev/mmcblk1; do [ -b $d ] && mnt $d && break; done; exit 0 ;;
  eject) sync; umount $M 2>/dev/null || umount -l $M; exit 0 ;;
esac
case "$ACTION" in
  add) case "$MDEV" in
         mmcblk1p*) mnt /dev/$MDEV ;;
         mmcblk1) sleep 2; [ -e /sys/block/mmcblk1/mmcblk1p1 ] || mnt /dev/$MDEV ;;
       esac ;;
  remove) case "$MDEV" in mmcblk1*) umount -l $M 2>/dev/null; rmdir $M 2>/dev/null ;; esac ;;
esac
