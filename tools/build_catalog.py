#!/usr/bin/env python3
"""Build the jgmDeb software catalog (own work): Debian's AppStream (dep11) metadata for armhf joined with package sizes -> compact catalog.json.
 usage: build_catalog.py Components-armhf.yml.gz Packages.xz out.json"""
import sys, gzip, lzma, json, yaml, re
comp, pkgs, out = sys.argv[1:4]
sizes = {}; deps = {}
cur = {}
for line in lzma.open(pkgs, 'rt', errors='replace'):
    if line == '\n':
        if 'Package' in cur: sizes[cur['Package']] = int(cur.get('Installed-Size', 0)); deps[cur['Package']] = cur.get('Depends', '')
        cur = {}
    elif line[0] not in ' \t' and ':' in line:
        k, v = line.split(':', 1); cur[k] = v.strip()
apps = {}
with gzip.open(comp, 'rt') as f:
    for doc in yaml.safe_load_all(f):
        if not doc or doc.get('Type') != 'desktop-application': continue
        pkg = doc.get('Package'); name = (doc.get('Name') or {}).get('C') or ''
        if not pkg or not name: continue
        cats = [c for c in doc.get('Categories', []) if c not in ('GTK', 'Qt', 'KDE', 'GNOME', 'X-Debian', 'XFCE', 'GNUstep', 'LXDE', 'MATE', 'Cinnamon')]
        summ = (doc.get('Summary') or {}).get('C') or ''
        icon = ''
        ic = doc.get('Icon') or {}
        if 'stock' in ic: icon = ic['stock']
        elif 'cached' in ic and ic['cached']: icon = ic['cached'][0].get('name', '') if isinstance(ic['cached'], list) else ''
        e = {'p': pkg, 'n': name, 's': summ, 'c': cats[:4], 'i': icon, 'z': sizes.get(pkg, 0)}
        # some packages ship several launchers: keep the one whose name matches the package best
        score = 2 if re.sub(r'[^a-z0-9]', '', name.lower()) == re.sub(r'[^a-z0-9]', '', pkg.lower()) else (1 if pkg.lower() in (doc.get('ID') or '').lower() else 0)
        if pkg not in apps or score > apps[pkg]['_s']: e['_s'] = score; apps[pkg] = e
cat = sorted(apps.values(), key=lambda e: e['n'].lower())
for e in cat: e.pop('_s', None)
json.dump({'schema': 1, 'source': 'Debian bookworm AppStream (dep11) armhf', 'count': len(cat), 'apps': cat}, open(out, 'w'), separators=(',', ':'), ensure_ascii=False)
print(len(cat), 'apps')
