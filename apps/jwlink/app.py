"""JWLink for jgmDeb: talks to the JWatch over Bluetooth LE using bluez's gatttool/hcitool. `shell` is injected by the jgmDeb app loader."""
import os, re, json, threading, time
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
    """one LE connection per operation, through jgmDeb's user-space Bluetooth stack (shell.jgmble)"""
    def __init__(self, addr, atype=0): self.addr = addr; self.atype = atype
    @staticmethod
    def available(): return shell.bt_available()
    def run(self, fn):
        b = shell.jgmble.get(); b.connect(self.addr, self.atype, 10)
        try: return fn(b)
        finally: b.disconnect()
    def read(self, uuid): return self.run(lambda b: b.read(uuid))
    def write(self, uuid, data): return self.run(lambda b: b.write(uuid, data))

def scan(seconds=8):
    found = shell.jgmble.get().scan(seconds)
    return {a: (v['name'], v['type']) for a, v in found.items()}

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
        if not Bt.available(): return [('Info', lambda: ('popopen', shell.TextBox('JWLink', 'Bluetooth is not available. It needs the WiFi/WCNSS firmware started (it is at boot) and this jgmDeb version or newer.')))]
        o = [('Scan for watch', self.do_scan)]
        if self.addr(): o += [('Sync time', lambda: self.act('Syncing time', self.sync)), ('Find my watch', lambda: self.act('Ringing watch', lambda: self.bt().write(P.U['ALERT_LEVEL'], P.FIND_WATCH) or 'Watch alerted')),
                              ('Refresh info', lambda: self.act('Reading', self.refresh)), ('Do not disturb', self.dnd_menu), ('Rename watch', lambda: ('popopen', shell.Editor('Watch name', self.cfg.get('name', ''), on_done=lambda n: self.act('Renaming', lambda: self.bt().write(P.U['WATCH_NAME'], P.ascii_bytes(n, 20)) or 'Renamed')))),
                              ('Forget watch', self.forget)]
        return o
    def bt(self): return Bt(self.addr(), self.cfg.get('atype', 0))
    def act(self, label, fn): self.job(label, fn); return 'popback'
    def do_scan(self):
        def work():
            found = scan(8)
            if not found: return 'No devices found'
            items = [('%s  %s' % (n or '?', a), (lambda a=a, n=n, t=t: self.choose(a, n, t))) for a, (n, t) in sorted(found.items(), key=lambda kv: (not kv[1][0], kv[0]))]
            self.pending = ('open', shell.Popup('Pick your watch', items)); return '%d devices' % len(found)
        self.pending = None; self.job('Scanning', work); return 'popback'
    def choose(self, a, n, t=0): self.cfg.update(addr=a, name=n, atype=t); store(self.cfg); self.msg = 'Saved ' + (n or a); return 'popback'
    def forget(self): self.cfg = {}; store(self.cfg); self.info = {}; self.msg = 'Forgotten'; return 'popback'
    def sync(self):
        self.bt().run(lambda b: (b.write(P.U['CURRENT_TIME'], P.current_time()), b.write(P.U['LOCAL_TIME_INFO'], P.local_time_info()))); return 'Time synced'
    def refresh(self):
        def go(b):
            info = {}
            for k, u, f in (('battery', 'BATTERY_LEVEL', P.parse_battery), ('firmware', 'FIRMWARE_REVISION', P.parse_firmware), ('steps', 'STEP_COUNT', P.parse_steps)):
                try: info[k] = f(b.read(P.U[u]))
                except Exception: pass
            return info
        self.info = self.bt().run(go); return 'Updated'
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
