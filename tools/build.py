#!/usr/bin/env python3
"""Build the store: packages/*.tar.gz and index.json (own work).
 apps/<id>/            -> packages/<id>-<version>.tar.gz   (manifest.json, app.py, ...)
 system/files/*        -> packages/jgmdeb-system-<version>.tar.gz  (installed to /opt/jgm on the phone; version in system/VERSION, notes in system/NOTES.txt)
Only own code belongs here: no vendor firmware, drivers or blobs."""
import os, sys, json, hashlib, tarfile, io, time, glob
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def sha(b): return hashlib.sha256(b).hexdigest()
def pack(files):                      # files: {arcname: bytes}; deterministic tar.gz
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz', format=tarfile.PAX_FORMAT, compresslevel=9) as t:
        for n in sorted(files):
            ti = tarfile.TarInfo(n); ti.size = len(files[n]); ti.mtime = 0; ti.mode = 0o644; t.addfile(ti, io.BytesIO(files[n]))
    # gzip header has a timestamp: normalise it so the checksum is stable
    b = bytearray(buf.getvalue()); b[4:8] = b'\0\0\0\0'; return bytes(b)
os.makedirs(R + '/packages', exist_ok=True)
for f in glob.glob(R + '/packages/*'): os.remove(f)
index = {'schema': 1, 'updated': time.strftime('%Y-%m-%d', time.gmtime()), 'apps': []}
for d in sorted(glob.glob(R + '/apps/*/manifest.json')):
    ad = os.path.dirname(d); man = json.load(open(d)); files = {}
    for root, _, fs in os.walk(ad):
        for f in fs:
            if f.endswith('.pyc') or '__pycache__' in root: continue
            files[os.path.relpath(os.path.join(root, f), ad)] = open(os.path.join(root, f), 'rb').read()
    data = pack(files); rel = 'packages/%s-%s.tar.gz' % (man['id'], man['version']); open(R + '/' + rel, 'wb').write(data)
    index['apps'].append({k: man[k] for k in ('id', 'name', 'version', 'author', 'description') if k in man} | {'long': man.get('long', ''), 'file': rel, 'sha256': sha(data), 'size': len(data)})
sv = open(R + '/system/VERSION').read().strip(); files = {}; target = {}
for f in sorted(glob.glob(R + '/system/files/*')):
    n = os.path.basename(f); files['files/' + n] = open(f, 'rb').read(); target['files/' + n] = '/opt/jgm/' + n
files['manifest.json'] = json.dumps({'version': sv, 'files': target}, indent=1).encode()
data = pack(files); rel = 'packages/jgmdeb-system-%s.tar.gz' % sv; open(R + '/' + rel, 'wb').write(data)
index['system'] = {'version': sv, 'file': rel, 'sha256': sha(data), 'size': len(data), 'notes': open(R + '/system/NOTES.txt').read().strip()}
json.dump(index, open(R + '/index.json', 'w'), indent=1); print('built', len(index['apps']), 'apps; system', sv)
