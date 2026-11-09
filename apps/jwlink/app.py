"""JWLink for jgmDeb: talks to the JWatch over Bluetooth LE using bluez's gatttool/hcitool. `shell` is injected by the jgmDeb app loader."""
import os, re, json, shutil, subprocess, threading, time
import protocol as P

SAVE = None
def save_path(): return shell.DATA + '/jwlink.json'
def load(): 
    try: return json.load(open(save_path()))
    except Exception: return {}
def store(o):
    try: json.dump(o, open(save_path(), 'w'))
    except Exception: pass

class Bt:
    """gatttool wrapper: read/write a characteristic by UUID (handles looked up once per connection)"""
    def __init__(self, addr): self.addr = addr; self.handles = {}
    @staticmethod
    def available(): return os.path.exists('/sys/class/bluetooth/hci0') and bool(shutil.which('gatttool'))
    def run(self, args, t=15):
        return subprocess.run(['gatttool', '-b', self.addr] + args, capture_output=True, text=True, timeout=t)
    def map(self):
        if self.handles: return
        r = self.run(['--characteristics']).stdout
        for m in re.finditer(r'char value handle = (0x[0-9a-f]+), uuid = ([0-9a-f-]+)', r): self.handles[m.group(2).lower()] = m.group(1)
        if not self.handles: raise IOError('could not connect')
    def read(self, uuid):
        self.map(); h = self.handles.get(uuid)
        if not h: raise IOError('not on this watch')
        out = self.run(['--char-read', '-a', h]).stdout
        m = re.search(r'value:\s*([0-9a-f ]+)', out)
        if not m: raise IOError('read failed')
        return bytes.fromhex(m.group(1).replace(' ', ''))
    def write(self, uuid, data):
        self.map(); h = self.handles.get(uuid)
        if not h: raise IOError('not on this watch')
        r = self.run(['--char-write-req', '-a', h, '-n', data.hex()])
        if 'successfully' not in r.stdout: raise IOError('write failed')

def scan(seconds=8):
    r = subprocess.run(['timeout', '-s', 'INT', str(seconds), 'hcitool', 'lescan'], capture_output=True, text=True)
    seen = {}
    for l in r.stdout.splitlines():
        m = re.match(r'([0-9A-F:]{17}) (.*)', l.strip())
        if m and (m.group(2) != '(unknown)' or m.group(1) not in seen): seen[m.group(1)] = m.group(2)
    return seen

class JWLink(shell.App):
    title = 'JWLink'; left = 'Menu'; tick = 1
    def __init__(self):
        self.cfg = load(); self.msg = ''; self.info = {}; self.sel = 0; self.busy = False
        if not Bt.available(): self.msg = 'Bluetooth not available'
    def addr(self): return self.cfg.get('addr')
    def job(self, label, fn):
        def run():
            self.busy = True; self.msg = label + '...'
            try: self.msg = fn() or 'Done'
            except Exception as e: self.msg = 'Failed: ' + str(e)[:40]
            self.busy = False
        if not self.busy: threading.Thread(target=run, daemon=True).start()
    def options(self):
        if not Bt.available(): return [('Info', lambda: ('popopen', shell.TextBox('JWLink', 'This phone build has no working Bluetooth yet (needs an hci0 adapter plus the bluez package). JWLink is installed and ready for when it does.')))]
        o = [('Scan for watch', self.do_scan)]
        if self.addr(): o += [('Sync time', lambda: self.act('Syncing time', self.sync)), ('Find my watch', lambda: self.act('Ringing watch', lambda: self.bt().write(P.U['ALERT_LEVEL'], P.FIND_WATCH) or 'Watch alerted')),
                              ('Refresh info', lambda: self.act('Reading', self.refresh)), ('Do not disturb', self.dnd_menu), ('Rename watch', lambda: ('popopen', shell.Editor('Watch name', self.cfg.get('name', ''), on_done=lambda n: self.act('Renaming', lambda: self.bt().write(P.U['WATCH_NAME'], P.ascii_bytes(n, 20)) or 'Renamed')))),
                              ('Forget watch', self.forget)]
        return o
    def bt(self): return Bt(self.addr())
    def act(self, label, fn): self.job(label, fn); return 'popback'
    def do_scan(self):
        def work():
            found = scan(8)
            if not found: return 'No devices found'
            items = [('%s  %s' % (n or '?', a), (lambda a=a, n=n: self.choose(a, n))) for a, n in found.items()]
            self.pending = ('open', shell.Popup('Pick your watch', items)); return '%d devices' % len(found)
        self.pending = None; self.job('Scanning', work); return 'popback'
    def choose(self, a, n): self.cfg.update(addr=a, name=n); store(self.cfg); self.msg = 'Saved ' + (n or a); return 'popback'
    def forget(self): self.cfg = {}; store(self.cfg); self.info = {}; self.msg = 'Forgotten'; return 'popback'
    def sync(self):
        b = self.bt(); b.write(P.U['CURRENT_TIME'], P.current_time()); b.write(P.U['LOCAL_TIME_INFO'], P.local_time_info()); return 'Time synced'
    def refresh(self):
        b = self.bt(); info = {}
        for k, u, f in (('battery', 'BATTERY_LEVEL', P.parse_battery), ('firmware', 'FIRMWARE_REVISION', P.parse_firmware), ('steps', 'STEP_COUNT', P.parse_steps)):
            try: info[k] = f(b.read(P.U[u]))
            except Exception: pass
        self.info = info; return 'Updated'
    def dnd_menu(self): return ('popopen', shell.Popup('Do not disturb', [(n, (lambda m=i, n=n: self.act('Setting', lambda: self.bt().write(P.U['DND'], P.dnd_value(m)) or 'DND ' + n))) for i, n in enumerate(P.DND_NAMES)]))
    def draw(self, d, img):
        shell.ctext(d, shell.W / 2, 44, 'JWLink', shell.font(24, True), shell.ACC)
        shell.ctext(d, shell.W / 2, 78, self.cfg.get('name') or ('no watch paired' if not self.addr() else self.addr()), shell.font(15), shell.FG)
        y = 112
        for k, label in (('battery', 'Battery'), ('steps', 'Steps'), ('firmware', 'Firmware')):
            if k in self.info: d.text((20, y), '%s  %s' % (label, self.info[k]) + ('%' if k == 'battery' else ''), font=shell.font(14), fill=shell.FG); y += 22
        for i, l in enumerate(shell.wrap(d, self.msg, shell.font(12), shell.W - 20)[:4]): shell.ctext(d, shell.W / 2, 220 + i * 15, l, shell.font(12), shell.WARN if self.msg.startswith(('Failed', 'Bluetooth')) else shell.DIM)
    def key(self, k):
        p = getattr(self, 'pending', None)
        if p: self.pending = None; return p
        if k in ('RSK', 'END'): return 'back'
        if k == 'OK': return ('open', shell.Popup('JWLink', self.options()))

def make(): return JWLink()
