#!/usr/bin/env python3
"""jgmDeb: keep big package files on the SD card (own work).

The root partition is small. This moves files >= 200 KB of installed packages (under /usr, /lib) to /media/sd/jgmdeb/store/<same path> and leaves a symlink behind.
  sd-offload.py --new          packages installed/upgraded since the last run (called by the apt post-invoke hook); also cleans up removed ones
  sd-offload.py pkg [pkg...]   offload these packages now
  sd-offload.py --restore pkg|--all   move files back to the root partition
  sd-offload.py --status       what is offloaded
Skipped: Essential / required / important packages (the system must boot without the card), files with names FAT cannot store, everything when no card is mounted.
Without the card the offloaded programs fail to start (the rest of the system is unaffected); put the card back and they work again.
"""
import os, sys, re, shutil, subprocess, time
SD = '/media/sd'; STORE = SD + '/jgmdeb/store'; LIST = '/data/jgm/offload.list'; MARK = '/data/jgm/offload.mark'; LOG = '/data/jgm/offload.log'
MIN = 200 * 1024; ROOTS = ('/usr/', '/lib/')
BAD = re.compile(r'[<>:"|?*\\\x00-\x1f]|[ .]$')
def log(m):
    try:
        with open(LOG, 'a') as f: f.write(time.strftime('%m-%d %H:%M:%S ') + m + '\n')
    except Exception: pass
def run(c): return subprocess.run(c, capture_output=True, text=True).stdout
CORE = re.compile(r'^(libc6|libc-bin|libgcc-s1|libstdc\+\+6|libssl\d*|libzstd1|libsystemd0|libcrypt1|zlib1g|libtinfo6|libncurses.*|libreadline\d*|libpython3.*|python3(\.\d+)?(-minimal)?|python3-(numpy|pil|xlib|six)|perl.*|libperl.*|openssh.*|dropbear.*|sudo|busybox.*|udev|systemd.*|sysvinit.*|init.*|bash|dash|coreutils|util-linux|mount|libmount1|libblkid1|libglib2.0-0|libffi\d*|libpcre2.*|libacl1|libattr1|libselinux1|apt|libapt.*|dpkg|libgnutls30|libnettle8|libhogweed6|libgmp10|libp11-kit0|libidn2-0|libunistring2|libtasn1-6|libdb5.3|libudev1|libkmod2|kmod|ca-certificates|wpasupplicant|hostapd|iw|wireless-tools|libnl.*|libdbus-1-3|dbus.*)$')
def protected(pkg):
    if CORE.match(pkg): return True
    out = run(['dpkg-query', '-W', '-f', '${Essential} ${Priority}', pkg]).split()
    return 'yes' in out[:1] or (len(out) > 1 and out[-1] in ('required', 'important'))
def entries():
    try: return [l.rstrip('\n').split('\t') for l in open(LIST) if '\t' in l]
    except Exception: return []
def offload(pkg):
    n = tot = 0
    for p in run(['dpkg', '-L', pkg]).splitlines():
        if not p.startswith(ROOTS) or os.path.islink(p) or not os.path.isfile(p): continue
        try: sz = os.path.getsize(p)
        except OSError: continue
        if sz < MIN or BAD.search(p): continue
        dst = STORE + p
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.copyfile(p, dst + '.part'); os.chmod(dst + '.part', 0o755); os.replace(dst + '.part', dst)
            if os.path.getsize(dst) != sz: raise OSError('size mismatch')
            tmp = p + '.jgmtmp'; os.symlink(dst, tmp); os.replace(tmp, p)
        except Exception as e:
            log('FAILED %s: %s' % (p, e)); 
            try: os.unlink(dst + '.part')
            except OSError: pass
            continue
        with open(LIST, 'a') as f: f.write('%s\t%s\n' % (pkg, p))
        n += 1; tot += sz
    if n: log('%s: %d files, %.1f MB to SD' % (pkg, n, tot / 1e6))
    return n, tot
def restore(pkgs):
    keep = []
    for pkg, p in entries():
        if pkgs != ['--all'] and pkg not in pkgs: keep.append((pkg, p)); continue
        dst = STORE + p
        try:
            if os.path.islink(p) and os.readlink(p) == dst:
                tmp = p + '.jgmtmp'; shutil.copyfile(dst, tmp); os.chmod(tmp, 0o755); os.replace(tmp, p); os.unlink(dst)
        except Exception as e: log('restore FAILED %s: %s' % (p, e)); keep.append((pkg, p))
    with open(LIST, 'w') as f:
        for pkg, p in keep: f.write('%s\t%s\n' % (pkg, p))
def gc():
    keep = []
    for pkg, p in entries():
        if os.path.islink(p) and os.readlink(p) == STORE + p: keep.append((pkg, p))
        else:
            try: os.unlink(STORE + p); log('removed orphan ' + p)
            except OSError: pass
    with open(LIST, 'w') as f:
        for pkg, p in keep: f.write('%s\t%s\n' % (pkg, p))
def new_packages():
    if not os.path.exists(MARK): return []
    since = float(open(MARK).read())
    pk = set()
    try:
        for l in open('/var/log/dpkg.log'):
            p = l.split()
            if len(p) >= 5 and p[2] == 'status' and p[3] == 'installed':
                if time.mktime(time.strptime(p[0] + ' ' + p[1], '%Y-%m-%d %H:%M:%S')) > since: pk.add(p[4].split(':')[0])
    except Exception: pass
    return sorted(pk)
def main():
    a = sys.argv[1:]; os.makedirs('/data/jgm', exist_ok=True)
    if a[:1] == ['--restore']: restore(a[1:]); return
    if a[:1] == ['--status']:
        e = entries(); print('%d files from %d packages on the SD card' % (len(e), len({x[0] for x in e})))
        print('card:', 'mounted' if os.path.ismount(SD) else 'NOT mounted'); return
    if not os.path.ismount(SD): log('no SD card mounted: nothing offloaded'); return
    start = time.time(); pk = new_packages() if a[:1] == ['--new'] else a
    gc()
    for p in pk:
        if protected(p): continue
        offload(p)
    if a[:1] == ['--new']: open(MARK, 'w').write(str(start - 2))
main()
