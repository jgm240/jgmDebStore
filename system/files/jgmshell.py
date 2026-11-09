#!/usr/bin/env python3
"""jgmDeb phone shell v2 (own work): keypad launcher + apps, drawn straight to /dev/fb0 (240x320 RGB565 SPI panel).

No toolkit: Pillow draws an RGB image, numpy converts it to RGB565, one FBIOPAN_DISPLAY pushes it. The panel takes ~100 ms per
full frame, so we redraw only when something changed (never faster than ~8 fps).

Keys (evdev matrix_keypad): 1..9 0 * #   LSK (KEY_MENU)  RSK (KEY_BACK)  D-pad  OK  CALL (KEY_SEND)  END (power key)
Soft keys everywhere: LEFT soft = options for the current screen (when it shows a label), RIGHT soft = back / clear.
"""
import threading, os, sys, time, struct, mmap, fcntl, glob, json, select, datetime, subprocess, traceback, random, pty, signal
import numpy as np
from PIL import Image, ImageDraw, ImageFont
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from jgmicons import icon
    import jgmicons as icon_mod
except Exception:
    icon = None; icon_mod = None

W, H = 240, 320
FDIR = '/usr/share/fonts/truetype/dejavu/'
DATA = os.environ.get('JGM_DATA', '/data/jgm')
try: os.makedirs(DATA, exist_ok=True)
except OSError: pass

# ------------------------------------------------------------------------------------------------ config / theme
ACCENTS = [('Green', (45, 226, 166)), ('Blue', (80, 160, 255)), ('Orange', (250, 160, 60)), ('Pink', (240, 100, 170)), ('Yellow', (240, 210, 80))]
DEFAULT_CFG = {'accent': 0, 'wallpaper': 'none', 'clock24': True, 'fontscale': 1, 'timeout': 30,
               'home': ['voicemail', 'phone', 'contacts', 'messages', 'settings', 'clock', 'calc', 'browser', 'notes'], 'dark': True,
               'setup_done': False, 'pin': '', 'autolock': True, 'slider_enabled': True, 'slider_lock': True, 'slide_unlock': True, 'slider_flip': False, 'brightness': 255, 'icon_theme': 0, 'radius': 2,
               'alarm': {'on': False, 'h': 7, 'm': 0, 'fired': ''}}
def load_cfg():
    c = dict(DEFAULT_CFG)
    try: c.update(json.load(open(DATA + '/config.json')))
    except Exception: pass
    return c
CFG = load_cfg()
STATE = {'slider_closed': False}
LCD = '/sys/class/leds/lcd-backlight/brightness'; KBL = '/sys/class/leds/button-backlight/brightness'
def wr(path, v):
    try: open(path, 'w').write(str(v))
    except Exception: pass
def save_cfg():
    try: json.dump(CFG, open(DATA + '/config.json', 'w'))
    except Exception: pass
def theme():
    global BG, PANEL, FG, DIM, ACC, WARN, RED
    if CFG['dark']: BG, PANEL, FG, DIM = (10, 18, 20), (22, 34, 38), (228, 238, 238), (116, 138, 140)
    else: BG, PANEL, FG, DIM = (238, 242, 242), (210, 220, 222), (16, 24, 26), (100, 112, 114)
    ACC = ACCENTS[CFG['accent'] % len(ACCENTS)][1]; WARN, RED = (250, 176, 60), (236, 92, 80)
theme()
_fc = {}
def font(size, bold=False, mono=False):
    size = max(8, int(round(size * (1 + 0.15 * (CFG['fontscale'] - 1))))) if not mono else size
    k = (size, bold, mono)
    if k not in _fc:
        _fc[k] = ImageFont.truetype(FDIR + ('DejaVuSansMono.ttf' if mono else ('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf')), size)
    return _fc[k]

# ------------------------------------------------------------------------------------------------ display + keys
class Screen:
    def __init__(self):
        self.fd = os.open('/dev/fb0', os.O_RDWR)
        self.var = bytearray(160); fcntl.ioctl(self.fd, 0x4600, self.var)
        fix = bytearray(80); fcntl.ioctl(self.fd, 0x4602, fix)
        smem_len = struct.unpack_from('I', fix, 20)[0]; self.line = struct.unpack_from('I', fix, 44)[0]
        self.mm = mmap.mmap(self.fd, smem_len); self.buf = np.zeros((H, self.line // 2), dtype=np.uint16); self.last = 0.0
    def show(self, img):
        a = np.asarray(img.convert('RGB'), dtype=np.uint16)
        self.buf[:, :W] = ((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)
        self.mm.seek(0); self.mm.write(self.buf.tobytes())
        struct.pack_into('II', self.var, 16, 0, 0); fcntl.ioctl(self.fd, 0x4606, self.var); self.last = time.time()
KEYMAP = {2: '1', 3: '2', 4: '3', 5: '4', 6: '5', 7: '6', 8: '7', 9: '8', 10: '9', 11: '0', 522: '*', 523: '#',
          139: 'LSK', 158: 'RSK', 103: 'UP', 108: 'DOWN', 105: 'LEFT', 106: 'RIGHT', 352: 'OK', 28: 'OK', 231: 'CALL', 116: 'END'}
class Keys:
    def __init__(self):
        self.fds = []; self.names = {}
        for p in sorted(glob.glob('/dev/input/event*')):
            try:
                fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK); self.fds.append(fd)
                self.names[fd] = rd('/sys/class/input/input%s/name' % p.replace('/dev/input/event', ''))
            except OSError: pass
        self.seen_down = {}; self.end_down = 0; self.end_fired = False; self.last_end = 0
    def slider_closed(self):
        """current slider state (hall_sensor0 key 249 = 1 means closed; CFG slider_flip inverts)"""
        for fd, n in self.names.items():
            if n == 'hall_sensor0':
                try:
                    b = bytearray(96); fcntl.ioctl(fd, 0x80604518, b); v = bool((b[31] >> 1) & 1)
                    return v != bool(CFG['slider_flip'])
                except Exception: pass
        return False
    def poll(self, timeout):
        out = []
        r, _, _ = select.select(self.fds, [], [], timeout)
        for fd in r:
            try:
                while True:
                    d = os.read(fd, 16 * 32)
                    for i in range(0, len(d) - 15, 16):
                        _, _, t, c, v = struct.unpack('llHHi', d[i:i + 16])
                        if t == 1 and c == 249 and self.names.get(fd) == 'hall_sensor0':
                            out.append('SLIDE_CLOSE' if (v == 1) != bool(CFG['slider_flip']) else 'SLIDE_OPEN'); continue
                        if t == 1 and c == 116:                    # power/hang-up key: reported by two devices, so de-bounce by time
                            if v == 1 and not self.end_down: self.end_down = time.time(); self.end_fired = False
                            elif v == 0: self.end_down = 0
                            if v == 1 and time.time() - self.last_end > 0.6: self.last_end = time.time(); out.append('END')
                            continue
                        if t == 1 and c in KEYMAP and v in (1, 2):
                            k = KEYMAP[c]
                            if k == 'END' and v == 1 and time.time() - self.seen_down.get('END', 0) < 0.05: continue   # power key shows on two devices
                            self.seen_down[k] = time.time(); out.append(k)
                    if len(d) < 16 * 32: break
            except (BlockingIOError, OSError): pass
        if self.end_down and not self.end_fired and time.time() - self.end_down > 0.9:
            self.end_fired = True; self.last_end = time.time(); out = [k for k in out if k != 'END'] + ['POWER_LONG']
        return out

# ------------------------------------------------------------------------------------------------ helpers
def rd(path, default=''):
    try: return open(path).read().strip()
    except Exception: return default
import jgmpower
BATT = None
def battery():
    """(percent str, status, microvolts str) from the voltage-based estimator (the kernel gauge always says 0)"""
    global BATT
    if BATT is None: BATT = jgmpower.BatteryManager(DATA)
    s = BATT.snapshot()
    return str(s['pct']), s['status'] + (' (limit %d%%)' % s['limit'] if s['limit'] < 100 and s['online'] else ''), str(int(s['v'] * 1e6))
def reboot_to(cmd):
    """sync and reboot with a restart reason ('recovery', 'bootloader', 'edl' or '' for normal)"""
    import ctypes
    subprocess.call('sync', shell=True)
    ctypes.CDLL(None, use_errno=True).syscall(88, 0xfee1dead, 672274793, 0xA1B2C3D4, ctypes.c_char_p(cmd.encode()))   # LINUX_REBOOT_CMD_RESTART2
    time.sleep(5); subprocess.call('reboot -f', shell=True)
def power_menu():
    ask = lambda t, fn: (lambda: ('open', Popup(t, [('Yes', fn), ('No', lambda: 'popback')])))
    return Popup('Power', [('Power off', ask('Power off?', lambda: ('run', 'sync; poweroff'))), ('Reboot', ask('Reboot?', lambda: reboot_to('') or 'back')),
                           ('Recovery', ask('Reboot to recovery?', lambda: reboot_to('recovery') or 'back')), ('EDL (9008)', ask('Enter EDL mode?', lambda: reboot_to('edl') or 'back')),
                           ('Bootloader', ask('Reboot to bootloader?', lambda: reboot_to('bootloader') or 'back'))])
def sh(cmd, t=20):
    try: return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t).stdout.strip()
    except Exception as e: return str(e)
def tw(d, s, f): return d.textlength(s, font=f)
def ctext(d, cx, y, s, f, fill): d.text((cx - tw(d, s, f) / 2, y), s, font=f, fill=fill)
def fit(d, s, f, maxw):
    while s and tw(d, s, f) > maxw: s = s[:-1]
    return s
def wrap(d, s, f, maxw):
    lines = []
    for para in s.split('\n'):
        cur = ''
        for word in para.split(' '):
            t = (cur + ' ' + word).strip()
            if tw(d, t, f) <= maxw or not cur: cur = t
            else: lines.append(cur); cur = word
        lines.append(cur)
    return lines
ICON_THEMES = ['normal', 'dark', 'metal', 'wood', 'paper']
UI_RADIUS = [0, 3, 6, 10, 14]
def rr(): return UI_RADIUS[max(0, min(4, CFG.get('radius', 2)))]
def rrect(d, box, fill=None, outline=None, width=1): d.rounded_rectangle(box, rr(), fill=fill, outline=outline, width=width)
def now_str(): return datetime.datetime.now().strftime('%H:%M' if CFG['clock24'] else '%I:%M %p').lstrip('0')
def chrome(d, title, left, right):
    d.rectangle((0, 0, W, 22), fill=PANEL)
    d.text((6, 3), now_str(), font=font(13, True), fill=FG)
    if STATE['slider_closed'] and CFG.get('slider_enabled', True): d.text((tw(d, now_str(), font(13, True)) + 14, 6), 'SCL', font=font(10, True), fill=WARN)
    cap, st, _ = battery(); bt = cap + '%' + (' +' if st.lower().startswith('charg') else '')
    d.text((W - 6 - tw(d, bt, font(12)), 4), bt, font=font(12), fill=ACC if cap not in ('0', '1', '2', '3') else WARN)
    if title: ctext(d, W / 2, 4, title, font(12, True), DIM)
    d.rectangle((0, H - 26, W, H), fill=PANEL)
    d.text((6, H - 22), left, font=font(13, True), fill=FG)
    d.text((W - 6 - tw(d, right, font(13, True)), H - 22), right, font=font(13, True), fill=FG)

# ------------------------------------------------------------------------------------------------ app base
class App:
    title = ''
    left = ''; right = 'Back'
    tick = None
    def draw(self, d, img): pass
    def key(self, k): return 'back' if k in ('RSK', 'END') else None
    def options(self): return []                          # list of (label, fn) shown by the left soft key
    def left_label(self): return 'Options' if self.options() else self.left

class Popup(App):
    """Options list opened by the left soft key. fn() returns a normal key result ('back', ('open', app), None)."""
    def __init__(self, title, items): self.title = title; self.items = items; self.sel = 0; self.left = ''; self.right = 'Cancel'
    def draw(self, d, img):
        d.rectangle((0, 22, W, H - 26), fill=BG)
        for i, (n, _) in enumerate(self.items[max(0, self.sel - 9):][:10]):
            idx = max(0, self.sel - 9) + i; y = 30 + i * 26
            if idx == self.sel: rrect(d, (3, y - 2, W - 3, y + 22), fill=PANEL); d.text((4, y + 1), '>', font=font(15, True), fill=ACC)
            d.text((18, y + 1), fit(d, n, font(15), W - 26), font=font(15), fill=FG)
    def key(self, k):
        if k == 'UP': self.sel = max(0, self.sel - 1)
        elif k == 'DOWN': self.sel = min(len(self.items) - 1, self.sel + 1)
        elif k.isdigit() and len(k) == 1 and 0 < int(k) <= len(self.items): self.sel = int(k) - 1; return ('popit', self.items[self.sel][1])
        elif k in ('OK', 'CALL'): return ('popit', self.items[self.sel][1])
        elif k in ('RSK', 'END', 'LSK'): return 'back'

class ListMenu(App):
    """A settings-style list. items: (label, value_fn or None, action_fn). action returns a key result."""
    def __init__(self, title, items): self.title = title; self.items = items; self.sel = 0; self.left = ''; self.right = 'Back'
    def draw(self, d, img):
        top = max(0, self.sel - 9)
        for i, it in enumerate(self.items[top:top + 10]):
            y = 30 + i * 28; sel = top + i == self.sel
            if sel: rrect(d, (3, y - 2, W - 3, y + 24), fill=PANEL)
            d.text((10, y + 3), fit(d, it[0], font(14), 130), font=font(14), fill=FG)
            if it[1]:
                v = str(it[1]())
                d.text((W - 8 - tw(d, v, font(12)), y + 5), fit(d, v, font(12), 96), font=font(12), fill=ACC)
    def key(self, k):
        if k == 'UP': self.sel = max(0, self.sel - 1)
        elif k == 'DOWN': self.sel = min(len(self.items) - 1, self.sel + 1)
        elif k in ('OK', 'RIGHT', 'CALL') and self.items: return self.items[self.sel][2]()
        elif k in ('RSK', 'END', 'LEFT'): return 'back'

class TextBox(App):
    def __init__(self, title, text, right='Back'): self.title = title; self.text = text; self.off = 0; self.right = right
    def draw(self, d, img):
        for i, l in enumerate(wrap(d, self.text, font(12, mono=True), W - 12)[self.off:self.off + 20]): d.text((6, 28 + i * 14), l, font=font(12, mono=True), fill=FG)
    def key(self, k):
        if k == 'DOWN': self.off += 1
        elif k == 'UP': self.off = max(0, self.off - 1)
        elif k in ('RSK', 'END', 'OK'): return 'back'

# ------------------------------------------------------------------------------------------------ text entry (multitap)
T9 = {'1': '.,?!1', '2': 'abc2', '3': 'def3', '4': 'ghi4', '5': 'jkl5', '6': 'mno6', '7': 'pqrs7', '8': 'tuv8', '9': 'wxyz9', '0': ' 0', '*': '*+-=/', '#': '#@_'}
class MultiTap:
    def __init__(self): self.last = None; self.t = 0.0; self.i = 0; self.upper = False
    def press(self, text, k):
        """returns the new text for a keypad key k (digits, * #) using multitap"""
        if k not in T9: return text
        ch = T9[k]; now = time.time()
        if self.last == k and now - self.t < 0.9 and text: text = text[:-1]; self.i = (self.i + 1) % len(ch)
        else: self.i = 0
        c = ch[self.i]; self.last = k; self.t = now
        return text + (c.upper() if self.upper else c)
    def reset(self): self.last = None

class Editor(App):
    """single/multi-line text input. Soft: LSK = Options (done/new line/abc-ABC/clear), RSK = delete (or cancel when empty)."""
    def __init__(self, title, text='', multiline=False, numeric=False, on_done=None):
        self.title = title; self.text = text; self.multi = multiline; self.numeric = numeric; self.on_done = on_done; self.mt = MultiTap(); self.right = 'Delete'
    tick = 0.7
    def options(self):
        o = [('Done', self.done), ('Clear', lambda: self.set('')), ('Cancel', lambda: 'back')]
        if self.multi: o.insert(1, ('New line', lambda: self.set(self.text + '\n')))
        if not self.numeric: o.insert(1, ('Mode: ' + ('ABC' if self.mt.upper else 'abc'), self.toggle))
        return o
    def set(self, t): self.text = t; self.mt.reset(); return 'popback'
    def toggle(self): self.mt.upper = not self.mt.upper; return 'popback'
    def finish(self):
        if self.on_done: self.on_done(self.text)
    def done(self):
        self.finish(); return 'popback2'
    def draw(self, d, img):
        f = font(16); lines, cur = [], ''
        for ch in self.text + '|':
            if ch == '\n' or tw(d, cur + ch, f) > W - 16: lines.append(cur); cur = '' if ch == '\n' else ch
            else: cur += ch
        lines.append(cur)
        for i, l in enumerate(lines[-11:]): d.text((8, 30 + i * 22), l, font=f, fill=FG)
        d.text((8, H - 46), ('123' if self.numeric else ('ABC' if self.mt.upper else 'abc')), font=font(11, True), fill=DIM)
    def key(self, k):
        if len(k) == 1 and k in '0123456789':
            self.text = (self.text + k) if self.numeric else self.mt.press(self.text, k)
        elif k in ('*', '#') and not self.numeric: self.text = self.mt.press(self.text, k)
        elif k == '*' and self.numeric: self.text += '*'
        elif k == '#' and self.numeric: self.text += '#'
        elif k == 'OK': self.mt.reset(); self.text += '\n' if self.multi else ''
        elif k == 'DOWN' and self.multi: self.text += '\n'; self.mt.reset()
        elif k == 'RSK':
            if self.text: self.text = self.text[:-1]; self.mt.reset()
            else: return 'back'
        elif k == 'END': return 'back'
        elif k == 'CALL': self.finish(); return 'back'

# ------------------------------------------------------------------------------------------------ data stores
def jload(name, default):
    try: return json.load(open(DATA + '/' + name))
    except Exception: return default
def jsave(name, obj):
    try: json.dump(obj, open(DATA + '/' + name, 'w'))
    except Exception: pass

# ------------------------------------------------------------------------------------------------ apps
class Phone(App):
    title = 'Phone'; right = 'Clear'
    def __init__(self, num=''): self.num = num; self.msg = ''
    def options(self): return [('Contacts', lambda: ('popopen', Contacts(pick=lambda n: setattr(self, 'num', n)))), ('Save number', self.save), ('Send message', lambda: ('open', Compose(self.num))), ('Call log', lambda: ('open', CallLog()))]
    def save(self): return ('open', Editor('Name', '', on_done=lambda n: self._add(n)))
    def _add(self, n):
        cs = jload('contacts.json', []); cs.append({'name': n or self.num, 'number': self.num}); jsave('contacts.json', cs)
    def draw(self, d, img):
        d.text((10, 36), self.num[-14:] or 'Enter number', font=font(26 if len(self.num) < 12 else 20, True), fill=FG if self.num else DIM)
        if self.msg:
            for i, l in enumerate(wrap(d, self.msg, font(12), W - 20)): d.text((10, 86 + i * 15), l, font=font(12), fill=WARN)
        for i, c in enumerate('123456789*0#'):
            x, y = 24 + (i % 3) * 66, 130 + (i // 3) * 38
            d.rounded_rectangle((x, y, x + 56, y + 30), 8, fill=PANEL); ctext(d, x + 28, y + 5, c, font(18, True), FG)
    def key(self, k):
        if len(k) == 1 and k in '0123456789*#': self.num += k; self.msg = ''
        elif k == 'RSK':
            if self.num: self.num = self.num[:-1]
            else: return 'back'
        elif k == 'END': return 'back'
        elif k in ('CALL', 'OK'): return ('open', Calling(self.num)) if self.num else None
class Calling(App):
    title = 'Call'; right = 'End'; tick = 1
    def __init__(self, num):
        self.num = num; self.t0 = time.time(); self.state = 'Dialing...'; self.err = ''
        jsave('calls.json', (jload('calls.json', []) + [{'n': num, 't': time.time(), 'dir': 'out'}])[-50:])
    def draw(self, d, img):
        ctext(d, W / 2, 70, self.num, font(24, True), FG)
        ctext(d, W / 2, 120, self.state, font(16), ACC)
        for i, l in enumerate(wrap(d, self.err or 'Voice calls need the modem and audio bring-up.', font(12), W - 20)): ctext(d, W / 2, 170 + i * 15, l, font(12), WARN)
    def key(self, k):
        if k == 'CALL' and self.state != 'Connected': self.state = 'Connected'; self.t0 = time.time()
        return 'back' if k in ('RSK', 'END') else None
class CallLog(App):
    title = 'Call log'
    def draw(self, d, img):
        cl = jload('calls.json', [])[::-1]
        for i, c in enumerate(cl[:11]): d.text((8, 30 + i * 22), fit(d, ('>' if c.get('dir') == 'out' else '<') + ' ' + c['n'], font(14), W - 16), font=font(14), fill=FG)
        if not cl: d.text((10, 40), 'No calls yet', font=font(14), fill=DIM)
class Contacts(App):
    title = 'Contacts'; left = 'Options'; right = 'Back'
    def __init__(self, pick=None): self.sel = 0; self.pick = pick; self.cs = []; self.load()
    def load(self): self.cs = sorted(jload('contacts.json', []), key=lambda c: c['name'].lower()); self.sel = min(self.sel, max(0, len(self.cs) - 1))
    def options(self):
        o = [('New contact', lambda: ('open', NewContact(self.load)))]
        if self.cs: o += [('Send message', lambda: ('open', Compose(self.cs[self.sel]['number']))), ('Delete', self.delete)]
        o.append(('Import from SIM', lambda: ('open', SimImport(self.load))))
        return o
    def delete(self):
        all_ = jload('contacts.json', []); c = self.cs[self.sel]
        all_ = [x for x in all_ if not (x['name'] == c['name'] and x['number'] == c['number'])]; jsave('contacts.json', all_); self.load(); return 'popback'
    def draw(self, d, img):
        if not self.cs: d.text((10, 40), 'No contacts', font=font(15), fill=DIM); d.text((10, 64), 'Options > New contact', font=font(12), fill=DIM); return
        top = max(0, self.sel - 8)
        for i, c in enumerate(self.cs[top:top + 9]):
            y = 28 + i * 30; sel = top + i == self.sel
            if sel: d.rectangle((0, y - 2, W, y + 27), fill=PANEL)
            d.text((10, y), fit(d, c['name'], font(14, True), W - 20), font=font(14, True), fill=FG); d.text((10, y + 15), c['number'], font=font(11), fill=DIM)
    def key(self, k):
        if k == 'UP': self.sel = max(0, self.sel - 1)
        elif k == 'DOWN': self.sel = min(len(self.cs) - 1, self.sel + 1)
        elif k in ('OK', 'CALL') and self.cs:
            n = self.cs[self.sel]['number']
            if self.pick: self.pick(n); return 'back'
            return ('open', Calling(n)) if k == 'CALL' else ('open', Popup(self.cs[self.sel]['name'], [('Call', lambda: ('popopen', Calling(n))), ('Message', lambda: ('popopen', Compose(n)))]))
        elif k in ('RSK', 'END'): return 'back'
class NewContact(App):
    title = 'New contact'
    def __init__(self, cb): self.cb = cb; self.name = ''; self.num = ''
    def draw(self, d, img):
        d.text((10, 40), 'Name', font=font(12), fill=DIM); d.text((10, 56), self.name or '(OK to edit)', font=font(16, True), fill=FG)
        d.text((10, 100), 'Number', font=font(12), fill=DIM); d.text((10, 116), self.num or '(DOWN to edit)', font=font(16, True), fill=FG)
        d.text((10, 170), 'LSK = save', font=font(12), fill=ACC)
    left = 'Save'
    def key(self, k):
        if k == 'OK': return ('open', Editor('Name', self.name, on_done=lambda t: setattr(self, 'name', t)))
        elif k == 'DOWN': return ('open', Editor('Number', self.num, numeric=True, on_done=lambda t: setattr(self, 'num', t)))
        elif k == 'LSK':
            if self.num:
                cs = jload('contacts.json', []); cs.append({'name': self.name or self.num, 'number': self.num}); jsave('contacts.json', cs); self.cb()
                return 'back'
        elif k in ('RSK', 'END'): return 'back'
class SimImport(App):
    title = 'SIM contacts'
    def __init__(self, cb): self.cb = cb
    def draw(self, d, img):
        for i, l in enumerate(wrap(d, 'Reading the SIM needs the modem. Not available yet.', font(14), W - 20)): d.text((10, 40 + i * 20), l, font=font(14), fill=WARN)
class Compose(App):
    title = 'New message'; left = 'Send'
    def __init__(self, to=''): self.to = to; self.body = ''
    def draw(self, d, img):
        d.text((10, 36), 'To', font=font(11), fill=DIM); d.text((10, 50), self.to or '(UP to edit)', font=font(15, True), fill=FG)
        d.text((10, 86), 'Message', font=font(11), fill=DIM)
        for i, l in enumerate(wrap(d, self.body or '(OK to edit)', font(14), W - 20)[:9]): d.text((10, 100 + i * 18), l, font=font(14), fill=FG if self.body else DIM)
    def key(self, k):
        if k == 'UP': return ('open', Editor('To', self.to, numeric=True, on_done=lambda t: setattr(self, 'to', t)))
        elif k == 'OK': return ('open', Editor('Message', self.body, multiline=True, on_done=lambda t: setattr(self, 'body', t)))
        elif k == 'LSK':
            if self.to and self.body:
                m = jload('sms.json', []); m.append({'n': self.to, 't': time.time(), 'dir': 'out', 'b': self.body, 'ok': False}); jsave('sms.json', m)
                return ('replace', TextBox('Message', 'Saved to Outbox.\nSending SMS needs the modem (not available yet).'))
        elif k in ('RSK', 'END'): return 'back'
class Messages(App):
    title = 'Messages'; left = 'New'
    def __init__(self): self.sel = 0; self.ms = []; self.load()
    def load(self): self.ms = jload('sms.json', [])[::-1]; self.sel = min(self.sel, max(0, len(self.ms) - 1))
    def draw(self, d, img):
        if not self.ms: d.text((10, 40), 'No messages', font=font(15), fill=DIM); return
        top = max(0, self.sel - 6)
        for i, m in enumerate(self.ms[top:top + 7]):
            y = 28 + i * 38; sel = top + i == self.sel
            if sel: d.rectangle((0, y - 2, W, y + 35), fill=PANEL)
            d.text((10, y), ('To ' if m['dir'] == 'out' else 'From ') + m['n'], font=font(13, True), fill=FG)
            d.text((10, y + 16), fit(d, m['b'].replace('\n', ' '), font(12), W - 20), font=font(12), fill=DIM)
    def key(self, k):
        if k == 'LSK': return ('open', Compose())
        elif k == 'UP': self.sel = max(0, self.sel - 1)
        elif k == 'DOWN': self.sel = min(len(self.ms) - 1, self.sel + 1)
        elif k == 'OK' and self.ms: return ('open', TextBox(self.ms[self.sel]['n'], self.ms[self.sel]['b']))
        elif k in ('RSK', 'END'): return 'back'
class Clock(App):
    title = 'Clock'; tick = 1; left = 'Options'
    def __init__(self): self.sw = None; self.sw_t = 0.0; self.running = False; self.mode = 'clock'
    def options(self): return [('Stopwatch', lambda: self.setm('sw')), ('Clock', lambda: self.setm('clock')), ('Reset stopwatch', self.reset)]
    def setm(self, m): self.mode = m; return 'popback'
    def reset(self): self.sw_t = 0.0; self.running = False; return 'popback'
    def draw(self, d, img):
        if self.mode == 'sw':
            t = self.sw_t + (time.time() - self.sw if self.running else 0)
            ctext(d, W / 2, 100, '%02d:%02d.%d' % (t // 60, t % 60, (t * 10) % 10), font(38, True), ACC); ctext(d, W / 2, 170, 'OK = start / stop', font(13), DIM); return
        n = datetime.datetime.now()
        ctext(d, W / 2, 90, n.strftime('%H:%M') if CFG['clock24'] else n.strftime('%I:%M'), font(54, True), ACC)
        ctext(d, W / 2, 160, n.strftime(':%S'), font(24, True), FG); ctext(d, W / 2, 200, n.strftime('%A'), font(16), FG)
        ctext(d, W / 2, 224, n.strftime('%d %B %Y'), font(14), DIM)
        up = float(rd('/proc/uptime', '0 0').split()[0]); ctext(d, W / 2, 262, 'uptime %dh %02dm' % (up // 3600, up % 3600 // 60), font(12), DIM)
    def key(self, k):
        if k == 'OK' and self.mode == 'sw':
            if self.running: self.sw_t += time.time() - self.sw
            else: self.sw = time.time()
            self.running = not self.running
        else: return super().key(k)
class Calc(App):
    title = 'Calculator'; left = '+ - x /'; right = 'Clear'
    def __init__(self): self.expr = ''; self.res = ''
    def draw(self, d, img):
        d.rectangle((8, 30, W - 8, 100), fill=PANEL, outline=DIM)
        d.text((14, 36), fit(d, self.expr[-18:], font(20, True), W - 30), font=font(20, True), fill=FG); d.text((14, 70), self.res, font=font(22, True), fill=ACC)
        d.text((10, 120), '0-9 digits   * = x   # = .\nUP +  DOWN -  LEFT /  RIGHT x\nOK or CALL = equals   LSK = menu', font=font(12), fill=DIM)
    def options(self): return [('+', lambda: self.add('+')), ('-', lambda: self.add('-')), ('x', lambda: self.add('*')), ('/', lambda: self.add('/')), ('( )', lambda: self.add('('))]
    def add(self, s): self.expr += s; return 'popback'
    def key(self, k):
        m = {'*': '*', '#': '.', 'UP': '+', 'DOWN': '-', 'LEFT': '/', 'RIGHT': '*'}
        if len(k) == 1 and k in '0123456789': self.expr += k
        elif k in m: self.expr += m[k]
        elif k in ('OK', 'CALL'):
            try: self.res = '= ' + str(eval(self.expr, {'__builtins__': {}}, {})) if self.expr else ''
            except Exception: self.res = 'error'
        elif k == 'RSK':
            if self.expr: self.expr = self.expr[:-1]
            else: return 'back'
        elif k == 'END': return 'back'
class Notes(App):
    title = 'Notes'; left = 'Options'
    def __init__(self): self.sel = 0; self.items = []; self.load()
    def load(self): self.items = jload('notes.json', []); self.sel = min(self.sel, max(0, len(self.items) - 1))
    def options(self):
        o = [('New note', lambda: ('open', Editor('New note', '', True, on_done=self.add)))]
        if self.items: o += [('Edit', lambda: ('open', Editor('Edit note', self.items[self.sel], True, on_done=self.edit))), ('Delete', self.delete)]
        return o
    def add(self, t):
        if t.strip(): self.items.append(t); jsave('notes.json', self.items)
    def edit(self, t): self.items[self.sel] = t; jsave('notes.json', self.items)
    def delete(self): self.items.pop(self.sel); jsave('notes.json', self.items); self.load(); return 'popback'
    def draw(self, d, img):
        if not self.items: d.text((10, 40), 'No notes', font=font(15), fill=DIM); d.text((10, 64), 'Options > New note', font=font(12), fill=DIM); return
        top = max(0, self.sel - 8)
        for i, t in enumerate(self.items[top:top + 9]):
            y = 28 + i * 28; sel = top + i == self.sel
            if sel: rrect(d, (3, y - 2, W - 3, y + 24), fill=PANEL)
            d.text((10, y + 3), fit(d, t.replace('\n', ' '), font(14), W - 20), font=font(14), fill=FG)
    def key(self, k):
        if k == 'UP': self.sel = max(0, self.sel - 1)
        elif k == 'DOWN': self.sel = min(len(self.items) - 1, self.sel + 1)
        elif k == 'OK' and self.items: return ('open', TextBox('Note', self.items[self.sel]))
        elif k in ('RSK', 'END'): return 'back'
class Calendar(App):
    title = 'Calendar'; left = 'Today'
    def __init__(self): t = datetime.date.today(); self.y = t.year; self.m = t.month; self.d = t.day
    def draw(self, d, img):
        import calendar
        ctext(d, W / 2, 28, '%s %d' % (calendar.month_name[self.m], self.y), font(16, True), ACC)
        for i, n in enumerate('MTWTFSS'): ctext(d, 17 + i * 34, 56, n, font(12, True), DIM)
        for r, week in enumerate(calendar.monthcalendar(self.y, self.m)):
            for c, day in enumerate(week):
                if not day: continue
                x, y = 17 + c * 34, 78 + r * 34
                if day == self.d: d.rounded_rectangle((x - 14, y - 3, x + 14, y + 22), 6, fill=ACC); ctext(d, x, y, str(day), font(14, True), (10, 18, 20))
                else: ctext(d, x, y, str(day), font(14), FG)
    def key(self, k):
        import calendar
        if k == 'LEFT': self.d = max(1, self.d - 1)
        elif k == 'RIGHT': self.d = min(calendar.monthrange(self.y, self.m)[1], self.d + 1)
        elif k == 'UP': self.d = max(1, self.d - 7)
        elif k == 'DOWN': self.d = min(calendar.monthrange(self.y, self.m)[1], self.d + 7)
        elif k == '1': self.m = 12 if self.m == 1 else self.m - 1; self.y -= self.m == 12
        elif k == '3': self.m = 1 if self.m == 12 else self.m + 1; self.y += self.m == 1
        elif k == 'LSK': t = datetime.date.today(); self.y, self.m, self.d = t.year, t.month, t.day
        elif k in ('RSK', 'END'): return 'back'
class Files(App):
    title = 'Files'; left = 'Options'
    def __init__(self, path='/'): self.path = path; self.sel = 0; self.refresh()
    def refresh(self):
        try: self.items = sorted(os.listdir(self.path), key=lambda s: (not os.path.isdir(os.path.join(self.path, s)), s.lower()))
        except Exception: self.items = []
        if self.path != '/': self.items = ['..'] + self.items
        self.sel = min(self.sel, max(0, len(self.items) - 1))
    def options(self):
        o = [('Go to /data/jgm', lambda: self.goto(DATA)), ('Go to /', lambda: self.goto('/'))]
        if sd_mounted(): o.insert(0, ('Go to SD card', lambda: self.goto('/media/sd')))
        if self.items and self.items[self.sel] != '..': o.append(('View file', self.view))
        return o
    def goto(self, p): self.path = p; self.sel = 0; self.refresh(); return 'popback'
    def view(self):
        p = os.path.join(self.path, self.items[self.sel])
        try: t = open(p, errors='replace').read(3000) if os.path.isfile(p) else '(directory)'
        except Exception as e: t = str(e)
        return ('popopen', TextBox(self.items[self.sel], t))
    def draw(self, d, img):
        d.text((8, 28), fit(d, self.path, font(12), W - 16), font=font(12), fill=DIM)
        top = max(0, self.sel - 8)
        for i, n in enumerate(self.items[top:top + 11]):
            y = 46 + i * 22; p = os.path.join(self.path, n)
            if top + i == self.sel: d.rectangle((0, y - 1, W, y + 20), fill=PANEL)
            d.text((8, y), fit(d, ('/' if os.path.isdir(p) else '') + n, font(14), W - 16), font=font(14), fill=ACC if os.path.isdir(p) else FG)
    def key(self, k):
        if k == 'UP': self.sel = max(0, self.sel - 1)
        elif k == 'DOWN': self.sel = min(len(self.items) - 1, self.sel + 1)
        elif k in ('OK', 'RIGHT') and self.items:
            p = os.path.normpath(os.path.join(self.path, self.items[self.sel]))
            if os.path.isdir(p): self.path = p; self.sel = 0; self.refresh()
        elif k in ('LEFT', 'RSK'):
            if self.path == '/': return 'back' if k == 'RSK' else None
            self.path = os.path.dirname(self.path) or '/'; self.sel = 0; self.refresh()
        elif k == 'END': return 'back'

# --------------------------------------------------------------------------------------------- terminal
KEYSEQ = {'UP': b'\x1b[A', 'DOWN': b'\x1b[B', 'RIGHT': b'\x1b[C', 'LEFT': b'\x1b[D', 'OK': b'\r'}
def ensure_pty():
    """the devpts mount inherited from the initramfs is stale on this kernel: remount it and recreate /dev/ptmx when openpty fails"""
    try: os.close(os.openpty()[0]); return
    except Exception: pass
    subprocess.call('umount /dev/pts 2>/dev/null; mount -t devpts devpts /dev/pts; rm -f /dev/ptmx; mknod -m 666 /dev/ptmx c 5 2', shell=True)

class Terminal(App):
    title = 'Terminal'; left = 'Keys'; right = 'Bksp'; COLS, ROWS = 40, 21
    def __init__(self):
        try: import pyte
        except Exception: pyte = None
        self.pyte = pyte; self.mt = MultiTap(); self.alive = True; self.fd = None
        if pyte:
            ensure_pty()
            self.screen = pyte.Screen(self.COLS, self.ROWS); self.stream = pyte.ByteStream(self.screen)
            pid, fd = pty.fork()
            if pid == 0:
                env = {'HOME': '/root', 'USER': 'root', 'LOGNAME': 'root', 'SHELL': '/bin/bash', 'TERM': 'xterm', 'LANG': 'C.UTF-8', 'COLUMNS': str(self.COLS), 'LINES': str(self.ROWS),
                       'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'}   # plain root login shell: no sandbox, full system access
                os.chdir('/root'); os.setgroups([0, 3003, 3004]); os.execve('/bin/bash', ['-bash'], env)
            self.pid, self.fd = pid, fd
            fcntl.ioctl(fd, 0x5414, struct.pack('HHHH', self.ROWS, self.COLS, 0, 0))   # TIOCSWINSZ
    tick = 0.25
    def pump(self):
        if self.fd is None: return False
        ch = False
        while select.select([self.fd], [], [], 0)[0]:
            try: data = os.read(self.fd, 4096)
            except OSError: self.alive = False; break
            if not data: self.alive = False; break
            self.stream.feed(data); ch = True
        return ch
    def options(self): return [('Ctrl-C', lambda: self.send(b'\x03')), ('Ctrl-D', lambda: self.send(b'\x04')), ('Tab', lambda: self.send(b'\t')), ('Esc', lambda: self.send(b'\x1b')), ('Mode: ' + ('ABC' if self.mt.upper else 'abc'), self.toggle), ('Exit terminal', lambda: 'popback2')]
    def send(self, b):
        if self.fd is not None: os.write(self.fd, b)
        return 'popback'
    def toggle(self): self.mt.upper = not self.mt.upper; return 'popback'
    def draw(self, d, img):
        if not self.pyte: d.text((8, 40), 'python3-pyte is not installed', font=font(13), fill=WARN); return
        self.pump(); f = font(10, mono=True)
        for y in range(self.ROWS):
            line = ''.join(self.screen.buffer[y][x].data for x in range(self.COLS)).rstrip()
            if line: d.text((0, 24 + y * 13), line, font=f, fill=FG)
        cx, cy = self.screen.cursor.x, self.screen.cursor.y
        d.rectangle((cx * 6, 24 + cy * 13, cx * 6 + 5, 24 + cy * 13 + 11), outline=ACC)
        if not self.alive: d.text((8, H - 44), '[shell exited]', font=font(11), fill=WARN)
    def key(self, k):
        if not self.pyte: return 'back' if k in ('RSK', 'END') else None
        if k in KEYSEQ: os.write(self.fd, KEYSEQ[k])
        elif len(k) == 1 and (k in T9):
            # multitap: replace the previous char with a backspace-then-new-char sequence
            now = time.time(); ch = T9[k]
            if self.mt.last == k and now - self.mt.t < 0.9: os.write(self.fd, b'\x7f'); self.mt.i = (self.mt.i + 1) % len(ch)
            else: self.mt.i = 0
            c = ch[self.mt.i]; self.mt.last = k; self.mt.t = now; os.write(self.fd, (c.upper() if self.mt.upper else c).encode())
        elif k == 'RSK': os.write(self.fd, b'\x7f'); self.mt.reset()
        elif k == 'CALL': os.write(self.fd, b'\r')
        elif k == 'END':
            try: os.kill(self.pid, signal.SIGHUP)
            except Exception: pass
            return 'back'
        return None

# --------------------------------------------------------------------------------------------- games / tools
class Snake(App):
    title = 'Snake'; tick = 0.25; left = ''; right = 'Back'
    def __init__(self): self.reset()
    def reset(self):
        self.body = [(8, 8), (7, 8), (6, 8)]; self.dir = (1, 0); self.food = self.newfood(); self.dead = False; self.score = 0; self.next = self.dir
    def newfood(self):
        while True:
            f = (random.randrange(0, 15), random.randrange(0, 17))
            if f not in self.body: return f
    def step(self):
        if self.dead: return
        self.dir = self.next; h = (self.body[0][0] + self.dir[0], self.body[0][1] + self.dir[1])
        if h in self.body or not (0 <= h[0] < 15 and 0 <= h[1] < 17): self.dead = True; return
        self.body.insert(0, h)
        if h == self.food: self.score += 1; self.food = self.newfood()
        else: self.body.pop()
    def draw(self, d, img):
        self.step(); c = 15
        d.rectangle((0, 24, 15 * c, 24 + 17 * c), outline=DIM)
        for x, y in self.body: d.rectangle((x * c + 1, 25 + y * c, x * c + c - 1, 24 + y * c + c - 1), fill=ACC)
        d.ellipse((self.food[0] * c + 2, 26 + self.food[1] * c, self.food[0] * c + c - 2, 24 + self.food[1] * c + c - 2), fill=WARN)
        d.text((6, 24 + 17 * c + 6), 'Score %d' % self.score + ('   GAME OVER - OK' if self.dead else ''), font=font(12, True), fill=FG)
    def key(self, k):
        dd = {'UP': (0, -1), 'DOWN': (0, 1), 'LEFT': (-1, 0), 'RIGHT': (1, 0), '2': (0, -1), '8': (0, 1), '4': (-1, 0), '6': (1, 0)}
        if k in dd and (dd[k][0] != -self.dir[0] or dd[k][1] != -self.dir[1]): self.next = dd[k]
        elif k == 'OK' and self.dead: self.reset()
        elif k in ('RSK', 'END'): return 'back'
class Torch(App):
    title = 'Flashlight'
    LEDS = ['/sys/class/leds/torch-light0/brightness', '/sys/class/leds/flashlight/brightness']
    def __init__(self): self.on = False; self.screen = False; self.set(True)
    def set(self, on):
        self.on = on
        for p in self.LEDS:
            try: open(p, 'w').write('255' if on else '0')
            except Exception: pass
    def draw(self, d, img):
        if self.screen and self.on: d.rectangle((0, 24, W, H - 28), fill=(255, 255, 255))
        ctext(d, W / 2, 130, 'ON' if self.on else 'OFF', font(34, True), ACC if self.on else DIM)
        ctext(d, W / 2, 190, 'OK = on / off', font(13), DIM); ctext(d, W / 2, 210, '* = screen light', font(13), DIM)
    def key(self, k):
        if k == 'OK': self.set(not self.on)
        elif k == '*': self.screen = not self.screen
        elif k in ('RSK', 'END'): self.set(False); return 'back'
class Stub(App):
    def __init__(self, title, lines): self.title = title; self.lines = lines
    def draw(self, d, img):
        for i, l in enumerate(wrap(d, '\n'.join(self.lines), font(14), W - 20)): d.text((10, 40 + i * 19), l, font=font(14), fill=FG if i == 0 else DIM)

# --------------------------------------------------------------------------------------------- settings
class DateTimeSet(App):
    title = 'Date & time'; left = 'Save'
    F = ['Year', 'Month', 'Day', 'Hour', 'Minute']
    def __init__(self): n = datetime.datetime.now(); self.v = [n.year, n.month, n.day, n.hour, n.minute]; self.f = 0
    def draw(self, d, img):
        for i, n in enumerate(self.F):
            y = 40 + i * 36
            if i == self.f: d.rectangle((0, y - 4, W, y + 28), fill=PANEL)
            d.text((12, y + 2), n, font=font(15), fill=FG); d.text((W - 80, y), '%04d' % self.v[i] if i == 0 else '%02d' % self.v[i], font=font(20, True), fill=ACC)
    def key(self, k):
        lim = [(2020, 2099), (1, 12), (1, 31), (0, 23), (0, 59)]
        if k == 'UP': self.f = max(0, self.f - 1)
        elif k == 'DOWN': self.f = min(4, self.f + 1)
        elif k in ('RIGHT', '6'): lo, hi = lim[self.f]; self.v[self.f] = lo if self.v[self.f] >= hi else self.v[self.f] + 1
        elif k in ('LEFT', '4'): lo, hi = lim[self.f]; self.v[self.f] = hi if self.v[self.f] <= lo else self.v[self.f] - 1
        elif k == 'LSK':
            v = self.v; sh('date -s "%04d-%02d-%02d %02d:%02d:00"' % tuple(v)); return 'back'
        elif k in ('RSK', 'END'): return 'back'
def bl_path():
    for p in glob.glob('/sys/class/leds/*/brightness') + glob.glob('/sys/class/backlight/*/brightness'):
        if 'lcd' in p or 'backlight' in p: return p
def sysinfo():
    mem = {l.split(':')[0]: int(l.split()[1]) // 1024 for l in open('/proc/meminfo') if l.split(':')[0] in ('MemTotal', 'MemFree', 'Cached', 'Buffers')}
    cap, st, mv = battery(); up = float(rd('/proc/uptime', '0 0').split()[0])
    return ('jgmDeb (Debian %s)\nkernel %s\nRAM %d MB, free %d MB\nbattery %s%% %s %.2f V\nroot %s\n/data %s\nuptime %dh %02dm\nIP %s' % (
        rd('/etc/debian_version'), sh('uname -r')[:26], mem['MemTotal'], mem['MemFree'] + mem['Cached'] + mem.get('Buffers', 0), cap, st, int(mv or 0) / 1e6,
        sh("df -h / | tail -1 | tr -s ' ' | cut -d' ' -f2,3,5"), sh("df -h /data | tail -1 | tr -s ' ' | cut -d' ' -f2,3,5"), up // 3600, up % 3600 // 60, sh("ip -4 -o addr show scope global | head -1 | tr -s ' ' | cut -d' ' -f4") or 'none'))
def make_settings():
    def cyc(key, opts):
        def f(): CFG[key] = opts[(opts.index(CFG[key]) + 1) % len(opts)] if CFG[key] in opts else opts[0]; save_cfg(); theme(); return None
        return f
    def accent(): CFG['accent'] = (CFG['accent'] + 1) % len(ACCENTS); save_cfg(); theme()
    def theme_t(): CFG['dark'] = not CFG['dark']; save_cfg(); theme()
    def clk(): CFG['clock24'] = not CFG['clock24']; save_cfg()
    def fscale(): CFG['fontscale'] = {1: 2, 2: 3, 3: 1}.get(CFG['fontscale'], 1); save_cfg(); _fc.clear()
    def bright():
        p = bl_path()
        if not p: return None
        cur = int(rd(p, '0') or 0); mx = int(rd(os.path.dirname(p) + '/max_brightness', '255') or 255)
        nxt = max(10, int(((cur * 4 // mx) + 1) % 5 * mx / 4) or mx // 4); open(p, 'w').write(str(nxt))
    def tout(): CFG['timeout'] = {15: 30, 30: 60, 60: 0, 0: 15}.get(CFG['timeout'], 30); save_cfg()
    display = ListMenu('Display', [('Brightness', lambda: (rd(bl_path() or '') or 'n/a'), lambda: bright()), ('Theme', lambda: 'Dark' if CFG['dark'] else 'Light', theme_t),
                                   ('Accent colour', lambda: ACCENTS[CFG['accent']][0], accent), ('Text size', lambda: ['', 'Normal', 'Large', 'Huge'][CFG['fontscale']], fscale),
                                   ('Icon style', lambda: ICON_THEMES[CFG.get('icon_theme', 0) % 5].title(), lambda: (CFG.__setitem__('icon_theme', (CFG.get('icon_theme', 0) + 1) % 5), save_cfg())[1]),
                                   ('Corner radius', lambda: ['Square', 'Small', 'Medium', 'Large', 'Round'][CFG.get('radius', 2)], lambda: (CFG.__setitem__('radius', (CFG.get('radius', 2) + 1) % 5), save_cfg())[1]),
                                   ('Clock format', lambda: '24 h' if CFG['clock24'] else '12 h', clk), ('Screen timeout', lambda: ('%ds' % CFG['timeout']) if CFG['timeout'] else 'never', tout)])
    return display
def home_layout():
    items = []
    def mk(i):
        def pick():
            return ('open', Popup('Slot %d' % (i + 1), [(a[1], (lambda aid=a[0]: ('popset', (i, aid)))) for a in ALL_APPS_META]))
        return pick
    for i in range(9): items.append(('Key %d' % (i + 1), (lambda i=i: APP_BY_ID.get(CFG['home'][i], ('', '?'))[1]), mk(i)))
    return ListMenu('Home screen keys', items)

def wlan_ip():
    return sh("ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1")
def ssh_menu():
    off = lambda: os.path.exists(DATA + '/ssh-off'); root = lambda: rd(DATA + '/ssh-mode') == 'root'
    def restart(): sh('pkill dropbear')
    def tog_on():
        if off(): os.remove(DATA + '/ssh-off')
        else: open(DATA + '/ssh-off', 'w').close()
        restart()
    def tog_mode():
        open(DATA + '/ssh-mode', 'w').write('sandbox' if root() else 'root'); restart()
    def setpw(pw):
        if len(pw) < 4: return
        subprocess.run(['chpasswd'], input='root:%s\njgm:%s\n' % (pw, pw), text=True)
    items = [('SSH server', lambda: 'Off' if off() else 'On', tog_on),
             ('Mode', lambda: 'Unsandboxed' if root() else 'Sandboxed', tog_mode),
             ('Login user', lambda: 'root' if root() else 'jgm', lambda: None),
             ('IP address', lambda: wlan_ip() or 'no WiFi', lambda: None), ('Port', lambda: '22', lambda: None),
             ('Change password', lambda: '(root + jgm)', lambda: ('open', Editor('New SSH password', '', on_done=setpw))),
             ('How to connect', None, lambda: ('open', TextBox('SSH', 'From a computer on the same WiFi:\n\n  ssh %s@%s\n\nSandboxed: only the unprivileged user "jgm" can log in; root is refused.\nUnsandboxed: root can log in (full control of the phone).\n\nThe password applies to both users.' % ('root' if root() else 'jgm', wlan_ip() or '<phone IP>'))))]
    return ListMenu('SSH', items)
def sd_mounted(): return os.path.ismount('/media/sd')
def sd_menu():
    def sdinfo():
        if not sd_mounted(): return 'No card mounted.\n\n' + ('Card detected but not mounted.' if os.path.exists('/dev/mmcblk1') else 'Insert a microSD card.')
        return sh('df -h /media/sd | tr -s " "') + '\n\n' + sh('ls /media/sd | head -12')
    return ListMenu('SD card', [('Status', lambda: 'Mounted' if sd_mounted() else ('Detected' if os.path.exists('/dev/mmcblk1') else 'No card'), lambda: ('open', TextBox('SD card', sdinfo()))),
                                ('Browse files', None, lambda: ('open', Files('/media/sd')) if sd_mounted() else None),
                                ('Mount', None, lambda: (sh('/opt/jgm/sd-hotplug.sh scan'), None)[1]),
                                ('Eject safely', None, lambda: (sh('/opt/jgm/sd-hotplug.sh eject'), None)[1])])


def modem_menu():
    auto = lambda: os.path.exists(DATA + '/modem-auto')
    def tog():
        if auto(): os.remove(DATA + '/modem-auto')
        else: open(DATA + '/modem-auto', 'w').close()
    def start(): subprocess.Popen('sh /opt/jgm/modem-up.sh', shell=True, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ListMenu('Modem / audio', [('Status', lambda: 'Running' if audio_ok() else 'Off', lambda: None), ('Start modem', None, lambda: (start(), None)[1]),
                                      ('Start at boot', lambda: 'On' if auto() else 'Off', tog),
                                      ('Info', None, lambda: ('open', TextBox('Modem', 'The modem DSP provides audio.\nIt needs a file service (rmtfs) that this phone software runs for it.\nCalls, SMS and SIM are not wired up yet.\n\nIf the phone ever fails to boot with Start at boot on, it turns itself off for the next boot.')))])

class Settings(ListMenu):
    def __init__(self):
        items = [('Display & theme', None, lambda: ('open', make_settings())), ('Home screen keys', None, lambda: ('open', home_layout())),
                 ('Date & time', None, lambda: ('open', DateTimeSet())), ('Lock & security', None, lambda: ('open', lock_menu())),
                 ('Slider', lambda: ('' if not CFG['slider_enabled'] else ('Closed' if STATE['slider_closed'] else 'Open')), lambda: ('open', slider_menu())),
                 ('WiFi', None, lambda: ('open', WifiApp())),
                 ('Modem / audio', lambda: 'Running' if audio_ok() else 'Off', lambda: ('open', modem_menu())),
                 ('Software update', lambda: 'v' + os_version(), lambda: ('open', SystemUpdate())),
                 ('Sound', None, lambda: ('open', Stub('Sound', ['Audio is not working yet.', 'It needs the modem/audio DSP bring-up.']))),
                 ('Battery', None, lambda: ('open', TextBox('Battery', 'level %s%%\nstatus %s\nvoltage %.2f V\ntemp %s' % (battery()[0], battery()[1], int(battery()[2] or 0) / 1e6, rd('/sys/class/power_supply/battery/temp', '?'))))),
                 ('SSH', lambda: 'Off' if os.path.exists(DATA + '/ssh-off') else 'On', lambda: ('open', ssh_menu())),
                 ('SD card', lambda: 'Mounted' if sd_mounted() else ('Detected' if os.path.exists('/dev/mmcblk1') else 'No card'), lambda: ('open', sd_menu())),
                 ('Storage & memory', None, lambda: ('open', TextBox('Storage', sh('df -h / /data /media/sd 2>/dev/null | tr -s " "') + '\n\n' + sh('free -m')))),
                 ('About this phone', None, lambda: ('open', TextBox('About', sysinfo()))), ('Run setup', None, lambda: ('open', Setup())),
                 ('Power menu', None, lambda: ('open', power_menu())),
                 ('Restart', None, lambda: ('open', Popup('Restart now?', [('Yes, restart', lambda: ('run', 'reboot')), ('No', lambda: 'popback')]))),
                 ('Power off', None, lambda: ('open', Popup('Power off?', [('Yes, power off', lambda: ('run', 'poweroff')), ('No', lambda: 'popback')])))]
        super().__init__('Settings', items)
class WifiApp(App):
    """WiFi: scan list (iw), connect with wpa_supplicant (/data/jgm/wpa.conf is also used at boot), DHCP via busybox udhcpc."""
    title = 'WiFi'; left = 'Options'; tick = 2
    def __init__(self): self.sel = 0; self.nets = []; self.busy = ''; self.err = ''; self.scan()
    def up(self): return os.path.exists('/sys/class/net/wlan0')
    def scan(self):
        if not self.up(): self.busy = 'No wlan0'; return
        self.busy = 'Scanning...'
        def work():
            out = sh('ip link set wlan0 up 2>&1; iw dev wlan0 scan 2>/dev/null', 30); nets = {}; cur = None
            for l in out.split('\n'):
                l = l.strip()
                if l.startswith('BSS '): cur = {'ssid': '', 'sig': -100, 'sec': False}
                elif cur is None: continue
                elif l.startswith('signal:'):
                    try: cur['sig'] = float(l.split()[1])
                    except Exception: pass
                elif l.startswith('SSID:'):
                    cur['ssid'] = l[5:].strip()
                    if cur['ssid'] and (cur['ssid'] not in nets or nets[cur['ssid']]['sig'] < cur['sig']): nets[cur['ssid']] = cur
                elif l.startswith(('RSN:', 'WPA:')): cur['sec'] = True
            self.nets = sorted(nets.values(), key=lambda n: -n['sig']); self.busy = ''
        threading.Thread(target=work, daemon=True).start()
    def cur(self):
        s = sh('wpa_cli -i wlan0 -p /run/wpa status 2>/dev/null')
        kv = dict(l.split('=', 1) for l in s.split('\n') if '=' in l)
        ssid = kv.get('ssid', '') if kv.get('wpa_state') == 'COMPLETED' else ''
        ip = sh("ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}'")
        return ssid, ip
    def connect(self, ssid, pw):
        self.busy = 'Connecting...'
        def work():
            q = lambda t: t.replace('\\', '\\\\').replace('"', '\\"')
            conf = 'ctrl_interface=/run/wpa\nupdate_config=0\ncountry=00\nnetwork={\n  ssid="%s"\n' % q(ssid) + ('  psk="%s"\n' % q(pw) if pw else '  key_mgmt=NONE\n') + '  scan_ssid=1\n}\n'
            try: open(DATA + '/wpa.conf', 'w').write(conf); os.chmod(DATA + '/wpa.conf', 0o600)
            except Exception: pass
            sh('sh /opt/jgm/wifi-connect.sh', 15)
            self.err = ''
            for i in range(25):
                time.sleep(1)
                if self.cur()[1]: break
                st = sh('wpa_cli -i wlan0 -p /run/wpa status 2>/dev/null | grep wpa_state').split('=')[-1]
                self.busy = 'Connecting... ' + (st.title() if st else '')
            else:
                st = sh('wpa_cli -i wlan0 -p /run/wpa status 2>/dev/null | grep wpa_state').split('=')[-1]
                self.err = 'Failed: no DHCP' if st == 'COMPLETED' else 'Failed: wrong password or out of range'
            self.busy = self.err
        threading.Thread(target=work, daemon=True).start()
    def options(self):
        return [('Scan again', lambda: (self.scan(), 'popback')[1]), ('Disconnect', lambda: (sh('sh /opt/jgm/wifi-connect.sh stop'), 'popback')[1]),
                ('Forget saved network', lambda: (os.path.exists(DATA + '/wpa.conf') and os.remove(DATA + '/wpa.conf'), sh('sh /opt/jgm/wifi-connect.sh stop'), 'popback')[2]),
                ('Reconnect saved', lambda: (sh('sh /opt/jgm/wifi-connect.sh &'), 'popback')[1])]
    def draw(self, d, img):
        ssid, ip = self.cur() if self.up() else ('', '')
        d.text((8, 28), fit(d, ('Connected: ' + ssid) if ssid else 'Not connected', font(13, True), W - 16), font=font(13, True), fill=ACC if ssid else DIM)
        d.text((8, 46), (ip or self.busy or 'no IP')[:40], font=font(11), fill=DIM)
        top = max(0, self.sel - 7)
        for i, n in enumerate(self.nets[top:top + 8]):
            y = 66 + i * 28; sel = top + i == self.sel
            if sel: rrect(d, (3, y - 2, W - 3, y + 24), fill=PANEL)
            bars = 4 if n['sig'] > -55 else 3 if n['sig'] > -67 else 2 if n['sig'] > -78 else 1
            d.text((6, y + 3), fit(d, n['ssid'], font(14), W - 64), font=font(14), fill=FG)
            for b in range(4): d.rectangle((W - 36 + b * 6, y + 18 - b * 4 - 2, W - 32 + b * 6, y + 20), fill=ACC if b < bars else DIM)
            if n['sec']: d.text((W - 54, y + 4), '*', font=font(14, True), fill=WARN)
    def key(self, k):
        if k == 'UP': self.sel = max(0, self.sel - 1)
        elif k == 'DOWN': self.sel = min(max(0, len(self.nets) - 1), self.sel + 1)
        elif k in ('OK', 'CALL') and self.nets:
            n = self.nets[self.sel]
            if n['sec']: return ('open', Editor('Password: ' + n['ssid'][:14], '', on_done=lambda pw: self.connect(n['ssid'], pw)))
            self.connect(n['ssid'], '')
        elif k in ('RSK', 'END'): return 'back'


# --------------------------------------------------------------------------------------------- lock screen, setup, extra apps
class LockScreen(App):
    title = ''; left = 'Unlock'; right = ''; tick = 1
    def __init__(self): self.hint = 0.0
    def draw(self, d, img):
        n = datetime.datetime.now()
        ctext(d, W / 2, 70, n.strftime('%H:%M') if CFG['clock24'] else n.strftime('%I:%M'), font(56, True), ACC)
        ctext(d, W / 2, 140, n.strftime('%A'), font(16), FG); ctext(d, W / 2, 164, n.strftime('%d %B'), font(14), DIM)
        cap, st, _ = battery(); ctext(d, W / 2, 210, 'Battery %s%%%s' % (cap, ' (charging)' if st.lower().startswith('charg') else ''), font(12), DIM)
        if STATE['slider_closed'] and CFG.get('slider_enabled', True): ctext(d, W / 2, 232, 'Slider closed', font(12), WARN)
        miss = len([c for c in jload('calls.json', []) if c.get('dir') == 'in' and not c.get('seen')])
        if miss: ctext(d, W / 2, 252, '%d missed call%s' % (miss, '' if miss == 1 else 's'), font(12), WARN)
        for i, n in enumerate(unseen_notifs()[-3:][::-1]): ctext(d, W / 2, 262 + i * 16 - (16 if not miss else 0), fit(d, n['title'], font(11), W - 20), font(11), ACC)
        if time.time() - self.hint < 3: ctext(d, W / 2, 276 - 4, 'Now press  *', font(16, True), FG)
    def key(self, k):
        if k == 'LSK': self.hint = time.time()
        elif k == '*' and time.time() - self.hint < 3: return 'unlock'
class PinEntry(App):
    title = 'Enter PIN'; left = ''; right = 'Cancel'
    def __init__(self): self.pin = ''; self.msg = ''
    def draw(self, d, img):
        ctext(d, W / 2, 90, '• ' * len(self.pin) + '_ ' * (4 - len(self.pin)), font(34, True), FG)
        ctext(d, W / 2, 160, self.msg, font(13), RED)
    def key(self, k):
        if len(k) == 1 and k in '0123456789':
            self.pin += k
            if len(self.pin) == len(CFG['pin']):
                if self.pin == CFG['pin']: return 'unlock'
                self.pin = ''; self.msg = 'Wrong PIN'
        elif k == 'RSK': 
            if self.pin: self.pin = self.pin[:-1]
            else: return 'cancel'
class Ringing(App):
    title = 'Alarm'; left = ''; right = 'Stop'; tick = 0.5
    def __init__(self): self.on = False
    def draw(self, d, img):
        self.on = not self.on
        for p in ('/sys/class/leds/red/brightness',): wr(p, 255 if self.on else 0)
        ctext(d, W / 2, 90, now_str(), font(48, True), ACC); ctext(d, W / 2, 170, 'ALARM', font(26, True), WARN if self.on else FG)
    def key(self, k):
        wr('/sys/class/leds/red/brightness', 0); return 'back'
class Monitor(App):
    title = 'Monitor'; tick = 1
    def __init__(self): self.prev = None; self.cpu = 0
    def sample(self):
        try: f = [int(x) for x in open('/proc/stat').readline().split()[1:8]]
        except Exception: return
        tot = sum(f); idle = f[3] + f[4]
        if self.prev: dt = tot - self.prev[0]; self.cpu = 100 * (1 - (idle - self.prev[1]) / dt) if dt else 0
        self.prev = (tot, idle)
    def bar(self, d, y, label, pct, text):
        d.text((8, y), label, font=font(12, True), fill=DIM); d.text((W - 8 - tw(d, text, font(12)), y), text, font=font(12), fill=FG)
        d.rectangle((8, y + 17, W - 8, y + 25), outline=DIM); d.rectangle((9, y + 18, max(9, 8 + (W - 18) * max(0, min(100, pct)) / 100), y + 24), fill=ACC)
    def draw(self, d, img):
        self.sample()
        try: mem = {l.split(':')[0]: int(l.split()[1]) // 1024 for l in open('/proc/meminfo') if l.split(':')[0] in ('MemTotal', 'MemFree', 'Cached', 'Buffers')}
        except Exception: mem = {'MemTotal': 1, 'MemFree': 0, 'Cached': 0}
        used = mem['MemTotal'] - mem['MemFree'] - mem['Cached'] - mem.get('Buffers', 0); cap, st, mv = battery()
        self.bar(d, 32, 'CPU', self.cpu, '%d%%  %s MHz' % (self.cpu, int(rd('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', '0') or 0) // 1000))
        self.bar(d, 72, 'RAM', 100 * used / mem['MemTotal'], '%d / %d MB' % (used, mem['MemTotal']))
        self.bar(d, 112, 'Battery', int(cap) if cap.isdigit() else 0, '%s%%  %.2f V' % (cap, int(mv or 0) / 1e6))
        up = float(rd('/proc/uptime', '0 0').split()[0])
        la = (rd('/proc/loadavg', '0 0 0 0/0').split(' ') + ['', '', '', ''])
        for i, l in enumerate(['load  ' + la[0], 'uptime  %dh %02dm' % (up // 3600, up % 3600 // 60), 'temp  %s' % rd('/sys/class/power_supply/battery/temp', '?'), 'procs  ' + la[3]]):
            d.text((10, 160 + i * 20), l, font=font(13), fill=FG)
class BatteryApp(App):
    title = 'Battery'; tick = 2; left = 'Options'
    def options(self):
        def lim(n): 
            def f(): BATT.set_limit(n); return 'back'
            return f
        def gov(n):
            def f(): CFG['governor'] = n; save_cfg(); BATT.governor(n); return 'back'
            return f
        return [('Charge to 100%', lim(100)), ('Charge limit 80%', lim(80)), ('Charge limit 60%', lim(60)), ('Power saver CPU', gov('powersave')), ('Normal CPU', gov('ondemand'))]
    def draw(self, d, img):
        cap, st, mv = battery(); b = '/sys/class/power_supply/battery/'
        ctext(d, W / 2, 50, cap + '%', font(50, True), ACC if cap.isdigit() and int(cap) > 10 else WARN)
        d.rounded_rectangle((40, 130, W - 40, 160), 6, outline=FG, width=2); d.rectangle((44, 134, 44 + (W - 88) * (int(cap) if cap.isdigit() else 0) / 100, 156), fill=ACC)
        for i, l in enumerate(['status   ' + st, 'voltage  %.3f V' % (int(mv or 0) / 1e6), 'current  %s mA' % (int(rd(b + 'current_now', '0') or 0) // 1000), 'health   ' + rd(b + 'health', '?'), 'temp     %s' % rd(b + 'temp', '?'), 'USB      ' + ('yes' if rd('/sys/class/power_supply/usb/online', '0') == '1' else 'no')]):
            d.text((20, 176 + i * 19), l, font=font(12, mono=True), fill=FG)
class AlarmApp(App):
    title = 'Alarm'; left = 'Save'; right = 'Back'
    def __init__(self): a = CFG['alarm']; self.v = [a['h'], a['m'], 1 if a['on'] else 0]; self.f = 0
    def draw(self, d, img):
        ctext(d, W / 2, 70, '%02d : %02d' % (self.v[0], self.v[1]), font(50, True), ACC if self.f < 2 else FG)
        d.rectangle((W / 2 - 62 + self.f * 62 if self.f < 2 else 0, 128, W / 2 - 4 + self.f * 62 if self.f < 2 else 0, 132), fill=ACC)
        ctext(d, W / 2, 160, 'Alarm  ' + ('ON' if self.v[2] else 'OFF'), font(22, True), ACC if self.v[2] else DIM)
        ctext(d, W / 2, 215, 'LEFT/RIGHT field   UP/DOWN change', font(11), DIM); ctext(d, W / 2, 232, 'OK on/off', font(11), DIM)
    def key(self, k):
        if k == 'LEFT': self.f = max(0, self.f - 1)
        elif k == 'RIGHT': self.f = min(1, self.f + 1)
        elif k in ('UP', 'DOWN') and self.f < 2:
            lim = 24 if self.f == 0 else 60; self.v[self.f] = (self.v[self.f] + (1 if k == 'UP' else -1)) % lim
        elif k == 'OK': self.v[2] = 1 - self.v[2]
        elif k == 'LSK': CFG['alarm'] = {'on': bool(self.v[2]), 'h': self.v[0], 'm': self.v[1], 'fired': ''}; save_cfg(); return 'back'
        elif k in ('RSK', 'END'): return 'back'
class Setup(App):
    title = 'Setup'; left = 'Next'; right = 'Back'
    STEPS = ['welcome', 'datetime', 'theme', 'slider', 'pin', 'done']
    def __init__(self): self.step = 0
    def draw(self, d, img):
        st = self.STEPS[self.step]; ctext(d, W / 2, 28, 'Step %d of %d' % (self.step + 1, len(self.STEPS)), font(11), DIM)
        def para(y, text, f=14, col=FG): 
            for i, l in enumerate(wrap(d, text, font(f), W - 24)): d.text((12, y + i * (f + 6)), l, font=font(f), fill=col)
        if st == 'welcome': ctext(d, W / 2, 56, 'jgmDeb', font(34, True), ACC); para(110, 'Welcome. This short setup takes a minute.\n\nNext: time, look, slider and PIN.')
        elif st == 'datetime': ctext(d, W / 2, 60, now_str(), font(40, True), ACC); para(120, datetime.datetime.now().strftime('%A %d %B %Y')); para(160, 'Press OK to set the date and time.', 13, DIM)
        elif st == 'theme':
            para(52, 'Theme:  ' + ('Dark' if CFG['dark'] else 'Light') + '   (UP/DOWN)', 14); para(90, 'Colour:  ' + ACCENTS[CFG['accent']][0] + '   (LEFT/RIGHT)', 14, ACC)
            d.rounded_rectangle((60, 140, W - 60, 220), 10, fill=PANEL, outline=ACC, width=3); ctext(d, W / 2, 168, 'Preview', font(18, True), ACC)
        elif st == 'slider':
            para(52, 'The slider cover:', 15); para(84, 'Use slider:  ' + ('Yes' if CFG['slider_enabled'] else 'No') + '   (OK)', 14, ACC)
            para(120, 'Now: ' + ('closed' if STATE['slider_closed'] else 'open') + '. Slide it to check - the state should change. If it is backwards, press 5 to swap.', 12, DIM)
            para(210, 'Lock when closed: ' + ('Yes' if CFG['slider_lock'] else 'No') + '  (1)', 13)
        elif st == 'pin': para(52, 'Lock screen PIN:  ' + ('set' if CFG['pin'] else 'none'), 15, ACC); para(96, 'OK = set a 4-digit PIN\n0 = no PIN', 14); para(170, 'You can change this later in Settings > Lock & security.', 12, DIM)
        elif st == 'done': ctext(d, W / 2, 80, 'All set', font(30, True), ACC); para(140, 'Press Next to start using your phone.\n\nSetup is under Settings if you need it again.', 14)
    def key(self, k):
        st = self.STEPS[self.step]
        if k in ('LSK', 'CALL') or (k == 'OK' and st in ('welcome', 'done')):
            if st == 'done': CFG['setup_done'] = True; save_cfg(); return 'back'
            self.step += 1
        elif k == 'RSK':
            if self.step > 0: self.step -= 1
            else: CFG['setup_done'] = True; save_cfg(); return 'back'
        elif st == 'datetime' and k == 'OK': return ('open', DateTimeSet())
        elif st == 'theme':
            if k in ('UP', 'DOWN'): CFG['dark'] = not CFG['dark']; theme(); save_cfg()
            elif k in ('LEFT', 'RIGHT'): CFG['accent'] = (CFG['accent'] + (1 if k == 'RIGHT' else -1)) % len(ACCENTS); theme(); save_cfg()
        elif st == 'slider':
            if k == 'OK': CFG['slider_enabled'] = not CFG['slider_enabled']; save_cfg()
            elif k == '1': CFG['slider_lock'] = not CFG['slider_lock']; save_cfg()
            elif k == '5': CFG['slider_flip'] = not CFG['slider_flip']; save_cfg()
        elif st == 'pin':
            if k == 'OK': return ('open', Editor('New PIN (4 digits)', '', numeric=True, on_done=lambda t: self.setpin(t)))
            elif k == '0': CFG['pin'] = ''; save_cfg()
    def setpin(self, t):
        if len(t) == 4 and t.isdigit(): CFG['pin'] = t; save_cfg()

def slider_menu():
    def tog(key):
        def f(): CFG[key] = not CFG[key]; save_cfg()
        return f
    m = ListMenu('Slider', [('Use slider', lambda: 'On' if CFG['slider_enabled'] else 'Off', tog('slider_enabled')),
                            ('State', lambda: ('Closed' if STATE['slider_closed'] else 'Open') if CFG['slider_enabled'] else 'Disabled', lambda: None),
                            ('Lock when closed', lambda: 'Yes' if CFG['slider_lock'] else 'No', tog('slider_lock')),
                            ('Unlock on open', lambda: 'Yes' if CFG['slide_unlock'] else 'No', tog('slide_unlock')),
                            ('Swap open/closed', lambda: 'Swapped' if CFG['slider_flip'] else 'Normal', tog('slider_flip')),
                            ('Open=answer, close=hang up', lambda: 'On' if CFG.get('slider_calls', True) else 'Off', lambda: (CFG.__setitem__('slider_calls', not CFG.get('slider_calls', True)), save_cfg())[1])])
    m.tick = 1; return m
def lock_menu():
    def tog(key):
        def f(): CFG[key] = not CFG[key]; save_cfg()
        return f
    def tout(): CFG['timeout'] = {15: 30, 30: 60, 60: 0, 0: 15}.get(CFG['timeout'], 30); save_cfg()
    def setpin(): return ('open', Editor('New PIN (4 digits)', '', numeric=True, on_done=lambda t: (CFG.__setitem__('pin', t), save_cfg()) if len(t) == 4 and t.isdigit() else None))
    def clrpin(): CFG['pin'] = ''; save_cfg()
    return ListMenu('Lock & security', [('Auto-lock', lambda: 'On' if CFG['autolock'] else 'Off', tog('autolock')),
                                        ('Screen timeout', lambda: ('%ds' % CFG['timeout']) if CFG['timeout'] else 'never', tout),
                                        ('Set PIN', lambda: 'set' if CFG['pin'] else 'none', setpin), ('Remove PIN', None, clrpin)])

# --------------------------------------------------------------------------------------------- notifications, news/podcasts, weather, radio/music, YouTube downloader
import urllib.request, urllib.parse, hashlib, re, html as _html, xml.etree.ElementTree as ET
MEDIA = DATA + '/media'; PODCASTS = DATA + '/podcasts'
def http_get(url, timeout=15, binary=False):
    req = urllib.request.Request(url, headers={'User-Agent': 'jgmDeb/1.0 (Nokia 8110 4G)'})
    with urllib.request.urlopen(req, timeout=timeout) as r: b = r.read(3_000_000)
    return b if binary else b.decode('utf-8', 'replace')
def strip_html(t): return re.sub(r'\s+', ' ', _html.unescape(re.sub(r'<[^>]+>', ' ', t or ''))).strip()
def storage_dir(sub):
    base = '/media/sd/jgm' if sd_mounted() else DATA
    p = base + '/' + sub; os.makedirs(p, exist_ok=True); return p

# ---- notifications: stored in notifs.json; shown on the lock screen, blink the red LED while the screen is off and they are unseen
def notify(title, text='', kind='info'):
    n = jload('notifs.json', []); n.append({'t': time.time(), 'title': title, 'text': text, 'kind': kind, 'seen': False}); jsave('notifs.json', n[-40:])
def unseen_notifs(): return [n for n in jload('notifs.json', []) if not n.get('seen')]
def mark_notifs_seen():
    n = jload('notifs.json', [])
    for x in n: x['seen'] = True
    jsave('notifs.json', n)

class PickList(App):
    """generic scrolling list: rows = [(text, subtext)] (or a function returning them), pick(i) -> key result, opts() -> options list, status shown at the bottom"""
    def __init__(self, title, rows, pick, opts=None, status=None):
        self.title = title; self.rows = rows; self.pick = pick; self.opts = opts; self.sel = 0; self.left = 'Options' if opts else ''; self.status = status; self.tick = 1
    def options(self): return self.opts() if self.opts else []
    def draw(self, d, img):
        rows = self.rows() if callable(self.rows) else self.rows
        self.sel = min(self.sel, max(0, len(rows) - 1)); top = max(0, self.sel - 7)
        for i, (t, sub) in enumerate(rows[top:top + 8]):
            y = 28 + i * 31; sel = top + i == self.sel
            if sel: rrect(d, (3, y - 1, W - 3, y + 29), fill=PANEL)
            d.text((6, y), fit(d, t, font(13, True), W - 12), font=font(13, True), fill=FG)
            if sub: d.text((6, y + 16), fit(d, sub, font(10), W - 12), font=font(10), fill=DIM)
        if not rows: ctext(d, W / 2, 120, 'Nothing here', font(14), DIM)
        st = self.status() if callable(self.status) else self.status
        if st: d.rectangle((0, H - 44, W, H - 27), fill=BG); d.text((6, H - 42), fit(d, st, font(11), W - 12), font=font(11), fill=ACC)
    def key(self, k):
        rows = self.rows() if callable(self.rows) else self.rows
        if k == 'UP': self.sel = max(0, self.sel - 1)
        elif k == 'DOWN': self.sel = min(max(0, len(rows) - 1), self.sel + 1)
        elif k in ('OK', 'CALL') and rows: return self.pick(self.sel)
        elif k in ('RSK', 'END'): return 'back'

class NotifApp(PickList):
    def __init__(self):
        mark_notifs_seen()
        rev = lambda: jload('notifs.json', [])[::-1]
        super().__init__('Notifications', lambda: [(n['title'], time.strftime('%d %b %H:%M', time.localtime(n['t'])) + '  ' + n.get('text', '')) for n in rev()],
                         lambda i: ('open', TextBox(rev()[i]['title'][:20], rev()[i]['title'] + '\n\n' + rev()[i].get('text', ''))),
                         lambda: [('Clear all', lambda: (jsave('notifs.json', []), 'popback')[1])])

# ---- RSS / Atom reader + podcasts
DEFAULT_FEEDS = [{'name': 'BBC News', 'url': 'http://feeds.bbci.co.uk/news/rss.xml', 'kind': 'news'}, {'name': 'Hacker News', 'url': 'https://hnrss.org/frontpage', 'kind': 'news'},
                 {'name': 'NPR News Now', 'url': 'https://feeds.npr.org/500005/podcast.xml', 'kind': 'podcast'}, {'name': 'BBC Global News Podcast', 'url': 'https://podcasts.files.bbci.co.uk/p02nq0gn.rss', 'kind': 'podcast'}]
def parse_feed(xml):
    root = ET.fromstring(xml); items = []; ns = {'a': 'http://www.w3.org/2005/Atom'}
    for it in root.iter('item'):
        enc = it.find('enclosure')
        items.append({'title': strip_html(it.findtext('title', '')), 'date': (it.findtext('pubDate', '') or '')[:16],
                      'text': strip_html(it.findtext('description', '') or it.findtext('{http://purl.org/rss/1.0/modules/content/}encoded', '')),
                      'link': it.findtext('link', ''), 'audio': enc.get('url') if enc is not None and 'audio' in (enc.get('type') or 'audio') else ''})
    for it in root.findall('a:entry', ns):
        l = it.find('a:link', ns)
        items.append({'title': strip_html(it.findtext('a:title', '', ns)), 'date': (it.findtext('a:updated', '', ns) or '')[:10],
                      'text': strip_html(it.findtext('a:summary', '', ns) or it.findtext('a:content', '', ns)), 'link': l.get('href') if l is not None else '', 'audio': ''})
    return items
class FeedItems(PickList):
    def __init__(self, feed):
        self.feed = feed; self.items = []; self.msg = 'Loading...'; self.cache = DATA + '/feedcache-' + hashlib.md5(feed['url'].encode()).hexdigest()[:10] + '.xml'
        try: self.items = parse_feed(open(self.cache, 'rb').read())
        except Exception: pass
        threading.Thread(target=self.load, daemon=True).start()
        super().__init__(feed['name'][:18], lambda: [(i['title'], i['date'] + ('  [audio]' if i['audio'] else '')) for i in self.items], self.read, self.opts_, status=lambda: self.msg)
    def load(self):
        try:
            raw = http_get(self.feed['url'], 20, True); open(self.cache, 'wb').write(raw); self.items = parse_feed(raw); self.msg = '%d items' % len(self.items)
        except Exception as e: self.msg = ('offline, showing saved: ' if self.items else 'Failed: ') + str(e)[:30]
    def read(self, i):
        it = self.items[i]
        return ('open', TextBox(it['title'][:20], it['title'] + '\n\n' + (it['text'] or '(no text)') + ('\n\n[audio episode: Options > Download]' if it['audio'] else '')))
    def opts_(self):
        o = [('Refresh', lambda: (threading.Thread(target=self.load, daemon=True).start(), 'popback')[1])]
        if self.items and self.items[self.sel]['audio']: o.insert(0, ('Download episode', lambda: self.download(self.items[self.sel])))
        return o
    def download(self, it):
        def work():
            try:
                self.msg = 'Downloading episode...'; fn = storage_dir('podcasts') + '/' + re.sub(r'[^\w\- ]', '', it['title'])[:50].strip() + '.mp3'
                req = urllib.request.Request(it['audio'], headers={'User-Agent': 'jgmDeb/1.0'})
                with urllib.request.urlopen(req, timeout=30) as r, open(fn, 'wb') as f:
                    tot = int(r.headers.get('Content-Length') or 0); got = 0
                    while True:
                        b = r.read(65536)
                        if not b: break
                        f.write(b); got += len(b); self.msg = 'Downloading %d%% (%d MB)' % (got * 100 // tot if tot else 0, got >> 20)
                self.msg = 'Saved: ' + os.path.basename(fn); notify('Podcast downloaded', it['title'])
            except Exception as e: self.msg = 'Download failed: ' + str(e)[:40]
        threading.Thread(target=work, daemon=True).start(); return 'popback'
def FeedList():
    feeds = jload('feeds.json', None) or list(DEFAULT_FEEDS)
    def add(): return ('popopen', Editor('Feed URL', 'https://', on_done=lambda u: (feeds.append({'name': urllib.parse.urlparse(u).netloc or u, 'url': u, 'kind': 'news'}), jsave('feeds.json', feeds))))
    def rm():
        if feeds: feeds.pop(pl.sel); jsave('feeds.json', feeds)
        return 'popback'
    pl = PickList('News & podcasts', lambda: [(f['name'], ('podcast  ' if f.get('kind') == 'podcast' else '') + f['url']) for f in feeds], lambda i: ('open', FeedItems(feeds[i])),
                  lambda: [('Add feed', add), ('Remove feed', rm)])
    return pl

# ---- weather (open-meteo, no key needed)
WCODE = {0: 'Clear', 1: 'Mostly clear', 2: 'Partly cloudy', 3: 'Overcast', 45: 'Fog', 48: 'Fog', 51: 'Drizzle', 53: 'Drizzle', 55: 'Drizzle', 61: 'Rain', 63: 'Rain', 65: 'Heavy rain',
         71: 'Snow', 73: 'Snow', 75: 'Heavy snow', 80: 'Showers', 81: 'Showers', 82: 'Heavy showers', 95: 'Thunderstorm', 96: 'Thunderstorm', 99: 'Thunderstorm'}
class Weather(App):
    title = 'Weather'; left = 'City'; tick = 2
    def __init__(self): self.data = jload('weather.json', None); self.msg = ''; self.refresh()
    def options(self): return [('Change city', lambda: ('popopen', Editor('City', CFG.get('city', ''), on_done=self.setcity))), ('Refresh', lambda: (self.refresh(), 'popback')[1])]
    def setcity(self, c): CFG['city'] = c.strip(); save_cfg(); self.refresh()
    def refresh(self):
        def work():
            try:
                city = CFG.get('city', '')
                if not city: self.msg = 'Set a city (Options)'; return
                self.msg = 'Updating...'
                g = json.loads(http_get('https://geocoding-api.open-meteo.com/v1/search?count=1&name=' + urllib.parse.quote(city)))['results'][0]
                w = json.loads(http_get('https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&current=temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=auto&forecast_days=4' % (g['latitude'], g['longitude'])))
                self.data = {'name': g['name'], 'cur': w['current'], 'daily': w['daily'], 't': time.time()}; jsave('weather.json', self.data); self.msg = ''
            except Exception as e: self.msg = 'Offline' if self.data else 'Failed: ' + str(e)[:30]
        threading.Thread(target=work, daemon=True).start()
    def draw(self, d, img):
        if not self.data: ctext(d, W / 2, 120, self.msg or 'No data', font(14), DIM); return
        c = self.data['cur']; ctext(d, W / 2, 34, self.data['name'], font(16, True), FG)
        ctext(d, W / 2, 62, '%d°' % round(c['temperature_2m']), font(54, True), ACC); ctext(d, W / 2, 128, WCODE.get(c['weather_code'], 'Weather'), font(16), FG)
        ctext(d, W / 2, 152, 'Wind %d km/h   Humidity %d%%' % (c['wind_speed_10m'], c['relative_humidity_2m']), font(11), DIM)
        dd = self.data['daily']
        for i in range(1, 4):
            y = 180 + (i - 1) * 28; day = datetime.datetime.strptime(dd['time'][i], '%Y-%m-%d').strftime('%a')
            d.text((10, y), day, font=font(14, True), fill=FG); d.text((60, y), WCODE.get(dd['weather_code'][i], ''), font=font(13), fill=FG)
            d.text((W - 80, y), '%d° / %d°' % (round(dd['temperature_2m_max'][i]), round(dd['temperature_2m_min'][i])), font=font(13), fill=ACC)
        if self.msg: ctext(d, W / 2, 268, self.msg, font(10), DIM)
    def key(self, k): return 'back' if k in ('RSK', 'END') else None

# ---- music + radio (mpg123). Needs a sound card; the phone has none until the modem / audio DSP is brought up.
STATIONS = [('SomaFM Groove Salad', 'http://ice1.somafm.com/groovesalad-128-mp3'), ('SomaFM Defcon', 'http://ice1.somafm.com/defcon-128-mp3'),
            ('BBC World Service', 'http://stream.live.vc.bbcmedia.co.uk/bbc_world_service'), ('Radio Paradise', 'http://stream.radioparadise.com/mp3-128')]
def audio_ok(): return os.path.exists('/proc/asound/cards') and 'no soundcards' not in rd('/proc/asound/cards')
class Player(App):
    title = 'Now playing'; right = 'Stop'; tick = 1
    def __init__(self, name, src): self.name = name; self.proc = None; self.paused = False; self.vol = 40; self.err = ''; self.src = src; self.start()
    def setvol(self): sh('amixer -q -c 0 cset name="RX3 Digital Volume" %d' % int(self.vol * 0.84))
    def start(self):
        if not audio_ok(): self.err = 'No audio device.\nStart the modem first (Settings).'; return
        sh('sh /opt/jgm/audio-speaker.sh'); self.setvol()
        try:
            if self.src.startswith('http') or self.src.lower().endswith('.mp3'): cmd = ['mpg123', '-q', '--control', self.src]
            elif os.path.exists('/usr/bin/ffmpeg'): cmd = ['ffmpeg', '-loglevel', 'quiet', '-i', self.src, '-f', 'alsa', 'default']
            else: self.err = 'This format needs ffmpeg.\nInstall: apt install ffmpeg'; return
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        except Exception as e: self.err = str(e)
    def send(self, c):
        if self.src.startswith('http') or self.src.lower().endswith('.mp3'):
            try: self.proc.stdin.write(c.encode()); self.proc.stdin.flush()
            except Exception: pass
        elif c == 's' and self.proc: self.proc.send_signal(signal.SIGSTOP if self.paused else signal.SIGCONT)
    def stop(self):
        if self.proc: self.proc.kill(); self.proc = None
    def draw(self, d, img):
        ctext(d, W / 2, 50, '♫', font(40, True), ACC)
        for i, l in enumerate(wrap(d, self.name, font(15, True), W - 20)[:3]): ctext(d, W / 2, 110 + i * 20, l, font(15, True), FG)
        if self.err:
            for i, l in enumerate(self.err.split('\n')): ctext(d, W / 2, 190 + i * 15, l, font(12), WARN)
        else: ctext(d, W / 2, 200, ('Paused' if self.paused else 'Playing') + '   vol %d%%' % self.vol, font(13), DIM); ctext(d, W / 2, 224, 'OK pause   UP/DOWN volume', font(10), DIM)
    def key(self, k):
        if k == 'OK' and self.proc: self.paused = not self.paused; self.send('s')
        elif k in ('UP', 'DOWN') and self.proc: self.vol = max(0, min(100, self.vol + (5 if k == 'UP' else -5))); self.setvol()
        elif k in ('RSK', 'END'): self.stop(); return 'back'
def Radio(): return PickList('Radio', [(n, u) for n, u in STATIONS], lambda i: ('open', Player(STATIONS[i][0], STATIONS[i][1])), status=lambda: '' if audio_ok() else 'No audio output yet')
def MusicApp():
    lst = []
    for base in (MEDIA, DATA + '/music', DATA + '/podcasts', '/media/sd'):
        for root, _, fs in os.walk(base):
            for f in sorted(fs):
                if f.lower().endswith(('.mp3', '.m4a', '.ogg', '.opus', '.wav', '.flac', '.webm')): lst.append(os.path.join(root, f))
            if root.count('/') - base.count('/') > 3: break
    return PickList('Music', [(os.path.basename(f), os.path.dirname(f)) for f in lst], lambda i: ('open', Player(os.path.basename(lst[i]), lst[i])),
                    lambda: [('Radio stations', lambda: ('popopen', Radio()))], status=lambda: '' if audio_ok() else 'No audio output yet')

# ---- YouTube downloader (yt-dlp)
class YtDl(App):
    title = 'YT download'; left = 'Menu'; tick = 1
    def __init__(self): self.msg = 'Search or paste a link'; self.prog = 0; self.job = None; self.results = []; self.pending = None
    def options(self):
        return [('Search', lambda: ('popopen', Editor('Search YouTube', '', on_done=self.search))), ('Paste URL', lambda: ('popopen', Editor('Video URL', 'https://', on_done=lambda u: setattr(self, 'pending', self.ask(u, u))))),
                ('Downloaded files', lambda: ('popopen', MediaFiles())), ('Update yt-dlp', lambda: (self.update(), 'popback')[1])]
    def search(self, q):
        def work():
            self.msg = 'Searching...'
            try:
                out = sh('yt-dlp --flat-playlist --print "%(id)s|%(title)s|%(duration_string)s" "ytsearch10:' + q.replace('"', '') + '"', 60)
                self.results = [l.split('|', 2) for l in out.split('\n') if l.count('|') >= 2]; self.msg = '%d results (press OK)' % len(self.results) if self.results else 'No results'
                if self.results: self.pending = ('open', PickList('Results', [(r[1], r[2]) for r in self.results], lambda i: self.ask('https://www.youtube.com/watch?v=' + self.results[i][0], self.results[i][1])))
            except Exception as e: self.msg = 'Failed: ' + str(e)[:30]
        threading.Thread(target=work, daemon=True).start()
    def ask(self, url, title):
        return ('popopen', Popup(title[:22], [('Audio only (m4a)', lambda: self.go(url, title, 'audio')), ('Video 360p', lambda: self.go(url, title, 'video')), ('Cancel', lambda: 'popback')]))
    def go(self, url, title, mode):
        def work():
            d = storage_dir('video' if mode == 'video' else 'music'); fmt = 'best[height<=360][ext=mp4]/best[height<=360]/best' if mode == 'video' else 'bestaudio[ext=m4a]/bestaudio'
            self.msg = 'Starting...'; self.prog = 0
            try:
                self.job = subprocess.Popen(['yt-dlp', '-f', fmt, '--newline', '--no-playlist', '-o', d + '/%(title).60s.%(ext)s', url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for l in self.job.stdout:
                    m = re.search(r'(\d+(?:\.\d+)?)%', l)
                    if m: self.prog = float(m.group(1)); self.msg = 'Downloading ' + title[:20] + ' ' + m.group(1) + '%'
                    elif 'ERROR' in l: self.msg = l.strip()[:60]
                self.job.wait(); ok = self.job.returncode == 0
                self.msg = ('Done: ' if ok else 'Failed: ') + title[:30]; self.prog = 100 if ok else 0
                if ok: notify('Download finished', title)
            except Exception as e: self.msg = 'Error: ' + str(e)[:40]
            self.job = None
        threading.Thread(target=work, daemon=True).start(); return 'popback2'
    def update(self): threading.Thread(target=lambda: setattr(self, 'msg', sh('yt-dlp -U 2>&1 | tail -1', 90)[:50]), daemon=True).start()
    def draw(self, d, img):
        ctext(d, W / 2, 60, 'YouTube', font(26, True), ACC); ctext(d, W / 2, 96, 'downloader', font(16), FG)
        for i, l in enumerate(wrap(d, self.msg, font(13), W - 20)[:4]): ctext(d, W / 2, 140 + i * 18, l, font(13), FG)
        if self.job: d.rectangle((20, 220, W - 20, 236), outline=FG); d.rectangle((22, 222, 22 + (W - 44) * self.prog / 100, 234), fill=ACC)
        ctext(d, W / 2, 262, 'Menu: search / paste link', font(11), DIM)
    def key(self, k):
        if self.pending: p, self.pending = self.pending, None; return p
        if k in ('RSK', 'END'): return 'back'
        if k == 'OK': return ('open', Popup('YT download', self.options()))
def MediaFiles():
    fl = []
    for sub in ('music', 'video'):
        d = ('/media/sd/jgm/' if sd_mounted() else DATA + '/') + sub
        if os.path.isdir(d): fl += [os.path.join(d, f) for f in sorted(os.listdir(d))]
    pl = PickList('Downloads', [(os.path.basename(f), '%.1f MB' % (os.path.getsize(f) / 1e6)) for f in fl], lambda i: ('open', Player(os.path.basename(fl[i]), fl[i])),
                  lambda: [('Delete file', lambda: (os.remove(fl.pop(pl.sel)) if fl else None, 'popback')[1])])
    return pl

# ---- WiFi hotspot (soft AP): hostapd via /opt/jgm/hotspot.sh
class Hotspot(App):
    title = 'Hotspot'; left = 'Options'; tick = 2
    def __init__(self): self.msg = ''
    def on(self): return bool(sh('pgrep hostapd'))
    def options(self):
        return [('Stop hotspot', lambda: (sh('sh /opt/jgm/hotspot.sh stop'), 'popback')[1])] if self.on() else [('Start hotspot', lambda: (self.start(), 'popback')[1])]
    def start(self): self.msg = 'Starting...'; threading.Thread(target=lambda: setattr(self, 'msg', sh('sh /opt/jgm/hotspot.sh start 2>&1 | tail -2', 40)[:80]), daemon=True).start()
    def draw(self, d, img):
        ctext(d, W / 2, 50, 'WiFi hotspot', font(20, True), ACC); ctext(d, W / 2, 90, 'ON' if self.on() else 'OFF', font(32, True), ACC if self.on() else DIM)
        ctext(d, W / 2, 140, 'Network: jgmDeb', font(14), FG); ctext(d, W / 2, 162, 'Password: ' + (rd(DATA + '/hotspot-pass') or 'jgmdeb1234'), font(14), FG)
        for i, l in enumerate(wrap(d, self.msg, font(11), W - 16)[:4]): ctext(d, W / 2, 200 + i * 14, l, font(11), WARN)
    def key(self, k): return 'back' if k in ('RSK', 'END') else None

# --------------------------------------------------------------------------------------------- Store, app loader, updater
import tarfile, importlib.util, io, shutil
APPS_DIR = DATA + '/apps'; BACKUP_DIR = DATA + '/backup'; LOAD_ERRORS = {}
DEFAULT_STORE = 'https://raw.githubusercontent.com/jgm240/jgmDebStore/main/'
def store_url(): return CFG.get('store_url', DEFAULT_STORE).rstrip('/') + '/'
def os_version(): return rd('/opt/jgm/VERSION', '0.0.0')
def vtuple(v): return tuple(int(x) for x in re.findall(r'\d+', str(v))) or (0,)
def fetch_index(): return json.loads(http_get(store_url() + 'index.json', 20))
def download_verified(rel, sha, size_cb=None):
    """download <store>/<rel>, check its sha256, return the bytes"""
    req = urllib.request.Request(store_url() + rel, headers={'User-Agent': 'jgmDeb-store/1.0'}); buf = io.BytesIO()
    with urllib.request.urlopen(req, timeout=30) as r:
        tot = int(r.headers.get('Content-Length') or 0)
        while True:
            b = r.read(32768)
            if not b: break
            buf.write(b)
            if size_cb: size_cb(buf.tell(), tot)
            if buf.tell() > 40_000_000: raise IOError('package too large')
    data = buf.getvalue()
    if hashlib.sha256(data).hexdigest() != sha: raise IOError('checksum mismatch - download corrupted or tampered')
    return data
def safe_extract(data, dest):
    """extract a .tar.gz refusing absolute paths, '..' and links"""
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as t:
        for m in t.getmembers():
            n = os.path.normpath(m.name)
            if n.startswith('/') or n.startswith('..') or not (m.isfile() or m.isdir()): raise IOError('unsafe path in package: ' + m.name)
        t.extractall(dest)
def installed_apps():
    out = {}
    try:
        for d in sorted(os.listdir(APPS_DIR)):
            try: out[d] = json.load(open(os.path.join(APPS_DIR, d, 'manifest.json')))
            except Exception: pass
    except Exception: pass
    return out
def register_app(man):
    """import an installed app and add it to the app list (the app module gets `shell` = this program, so it can use App, Editor, ListMenu ...)"""
    aid = man['id']; d = os.path.join(APPS_DIR, aid)
    try:
        if d not in sys.path: sys.path.insert(0, d)
        spec = importlib.util.spec_from_file_location('jgmapp_' + aid, os.path.join(d, man.get('entry', 'app.py'))); mod = importlib.util.module_from_spec(spec); mod.shell = sys.modules[__name__]
        spec.loader.exec_module(mod); factory = getattr(mod, man.get('factory', 'make'))
        ic = os.path.join(d, 'icon.png')
        if os.path.exists(ic) and icon: icon_mod.register_custom(aid, ic)
        meta = (aid, man.get('name', aid), '', tuple(man.get('color', [110, 120, 130])), factory)
        for i, a in enumerate(APP_META):
            if a[0] == aid: APP_META[i] = meta; break
        else: APP_META.append(meta)
        APP_BY_ID[aid] = meta; ALL_APPS_META[:] = [(a[0], a[1]) for a in APP_META]; LOAD_ERRORS.pop(aid, None)
    except Exception as e: LOAD_ERRORS[aid] = str(e)[:60]; traceback.print_exc()
def unregister_app(aid):
    APP_META[:] = [a for a in APP_META if a[0] != aid]; APP_BY_ID.pop(aid, None); ALL_APPS_META[:] = [(a[0], a[1]) for a in APP_META]
    CFG['home'] = [h if h != aid else 'settings' for h in CFG['home']]; save_cfg()
def install_package(entry, progress=None):
    """entry: an index.json app record. Installs/updates to /data/jgm/apps/<id>"""
    data = download_verified(entry['file'], entry['sha256'], progress); aid = entry['id']; tmp = APPS_DIR + '/.new-' + aid
    shutil.rmtree(tmp, ignore_errors=True); safe_extract(data, tmp)
    man = json.load(open(tmp + '/manifest.json'))
    if man['id'] != aid: raise IOError('package id mismatch')
    dst = APPS_DIR + '/' + aid; shutil.rmtree(dst + '.old', ignore_errors=True)
    if os.path.isdir(dst): os.rename(dst, dst + '.old')
    os.rename(tmp, dst); shutil.rmtree(dst + '.old', ignore_errors=True); register_app(man); return man
def remove_app(aid):
    shutil.rmtree(APPS_DIR + '/' + aid, ignore_errors=True); unregister_app(aid)

class StoreApp(PickList):
    """jgmDeb Store: apps published in the store repo (index.json). OK = install / update / remove."""
    def __init__(self):
        self.index = None; self.msg = 'Loading store...'; threading.Thread(target=self.load, daemon=True).start()
        super().__init__('Store', self.rows_, self.pick_, lambda: [('Update all', self.update_all), ('Refresh', lambda: (threading.Thread(target=self.load, daemon=True).start(), 'popback')[1]),
                                                                  ('Store address', lambda: ('popopen', Editor('Store URL', store_url(), on_done=lambda u: (CFG.__setitem__('store_url', u.strip()), save_cfg()))))], status=lambda: self.msg)
    def load(self):
        try: self.index = fetch_index(); self.msg = '%d apps' % len(self.index.get('apps', []))
        except Exception as e: self.msg = 'Store unreachable: ' + str(e)[:30]
    def apps(self): return (self.index or {}).get('apps', [])
    def state(self, a):
        inst = installed_apps().get(a['id'])
        if not inst: return 'not installed'
        return 'installed v%s' % inst['version'] + ('  - UPDATE v%s' % a['version'] if vtuple(a['version']) > vtuple(inst['version']) else '')
    def rows_(self): return [(a['name'] + '  v' + a['version'], self.state(a) + ' | ' + a.get('description', '')) for a in self.apps()]
    def pick_(self, i):
        a = self.apps()[i]; inst = installed_apps().get(a['id']); items = []
        if not inst: items.append(('Install', lambda: self.do(a)))
        elif vtuple(a['version']) > vtuple(inst['version']): items.append(('Update to v' + a['version'], lambda: self.do(a)))
        if inst: items.append(('Remove', lambda: (remove_app(a['id']), setattr(self, 'msg', 'Removed ' + a['name']), 'popback')[2]))
        items.append(('Details', lambda: ('popopen', TextBox(a['name'][:18], '%s\nversion %s   %.0f KB\nby %s\n\n%s\n\nApps run with full access to the phone. Only install apps you trust.' % (a['name'], a['version'], a.get('size', 0) / 1024, a.get('author', '?'), a.get('long', a.get('description', '')))))))
        items.append(('Cancel', lambda: 'popback'))
        return ('open', Popup(a['name'][:20], items))
    def do(self, a):
        def work():
            try: self.msg = 'Installing %s...' % a['name']; install_package(a, lambda n, t: setattr(self, 'msg', 'Downloading %s %d%%' % (a['name'], n * 100 // t if t else 0))); self.msg = 'Installed ' + a['name']; notify('App installed', a['name'])
            except Exception as e: self.msg = 'Failed: ' + str(e)[:40]
        threading.Thread(target=work, daemon=True).start(); return 'popback'
    def update_all(self):
        def work():
            n = 0
            for a in self.apps():
                inst = installed_apps().get(a['id'])
                if inst and vtuple(a['version']) > vtuple(inst['version']):
                    try: self.msg = 'Updating ' + a['name']; install_package(a); n += 1
                    except Exception as e: self.msg = 'Failed: ' + str(e)[:40]; return
            self.msg = '%d app(s) updated' % n
        threading.Thread(target=work, daemon=True).start(); return 'popback'

def apply_system_update(entry, progress=None):
    """download + verify the system package, back up /opt/jgm, install the new files (only under /opt/jgm), compile-check them first"""
    data = download_verified(entry['file'], entry['sha256'], progress); tmp = DATA + '/update-tmp'
    shutil.rmtree(tmp, ignore_errors=True); safe_extract(data, tmp); man = json.load(open(tmp + '/manifest.json'))
    files = man['files']
    for src, dst in files.items():
        if not dst.startswith('/opt/jgm/') or '..' in dst: raise IOError('refusing path ' + dst)
        if dst.endswith('.py'):
            compile(open(os.path.join(tmp, src)).read(), dst, 'exec')          # syntax check before touching anything
    os.makedirs(BACKUP_DIR, exist_ok=True); bk = '%s/opt-jgm-%s.tar.gz' % (BACKUP_DIR, os_version())
    with tarfile.open(bk, 'w:gz') as t: t.add('/opt/jgm', arcname='opt-jgm', filter=lambda ti: None if '__pycache__' in ti.name or ti.name.endswith(('.wav', '.pyc')) else ti)
    for src, dst in files.items():
        shutil.copyfile(os.path.join(tmp, src), dst + '.new'); os.chmod(dst + '.new', 0o755 if dst.endswith('.sh') else 0o644); os.replace(dst + '.new', dst)
    open('/opt/jgm/VERSION', 'w').write(man['version'] + '\n'); shutil.rmtree(tmp, ignore_errors=True)
    return man
def rollback_system():
    bks = sorted(glob.glob(BACKUP_DIR + '/opt-jgm-*.tar.gz'), key=os.path.getmtime)
    if not bks: raise IOError('no backup')
    with tarfile.open(bks[-1]) as t:
        for m in t.getmembers():
            if m.isfile() and m.name.startswith('opt-jgm/'): dst = '/opt/jgm/' + m.name[8:]; open(dst, 'wb').write(t.extractfile(m).read())
    return bks[-1]
def restart_ui(): os._exit(0)          # init respawns the shell (inittab "ui" entry)

class SystemUpdate(App):
    title = 'Software update'; left = 'Action'; tick = 1
    def __init__(self): self.info = None; self.msg = 'Checking...'; self.busy = False; threading.Thread(target=self.check, daemon=True).start()
    def check(self):
        try:
            self.info = fetch_index().get('system'); self.msg = 'Up to date' if not self.newer() else 'Update available'
        except Exception as e: self.msg = 'Cannot reach store: ' + str(e)[:30]
    def newer(self): return bool(self.info) and vtuple(self.info['version']) > vtuple(os_version())
    def options(self):
        o = [('Check again', lambda: (threading.Thread(target=self.check, daemon=True).start(), 'popback')[1])]
        if self.newer() and not self.busy: o.insert(0, ('Install update', self.install))
        if glob.glob(BACKUP_DIR + '/opt-jgm-*.tar.gz'): o.append(('Roll back to previous', self.rollback))
        return o
    def install(self):
        def work():
            self.busy = True
            try:
                man = apply_system_update(self.info, lambda n, t: setattr(self, 'msg', 'Downloading %d%%' % (n * 100 // t if t else 0)))
                self.msg = 'Updated to %s. Restarting...' % man['version']; notify('Software updated', man['version']); time.sleep(2); restart_ui()
            except Exception as e: self.msg = 'Failed: ' + str(e)[:50]; self.busy = False
        threading.Thread(target=work, daemon=True).start(); return 'popback'
    def rollback(self):
        try: b = rollback_system(); self.msg = 'Rolled back. Restarting...'; threading.Thread(target=lambda: (time.sleep(2), restart_ui()), daemon=True).start()
        except Exception as e: self.msg = 'Failed: ' + str(e)[:40]
        return 'popback'
    def draw(self, d, img):
        ctext(d, W / 2, 40, 'jgmDeb', font(26, True), ACC); ctext(d, W / 2, 76, 'version ' + os_version(), font(14), FG)
        if self.info: ctext(d, W / 2, 108, 'latest: ' + self.info['version'], font(14), ACC if self.newer() else DIM)
        for i, l in enumerate(wrap(d, (self.info or {}).get('notes', '') if self.newer() else '', font(11), W - 20)[:5]): d.text((10, 136 + i * 15), l, font=font(11), fill=DIM)
        ctext(d, W / 2, 238, self.msg, font(13, True), WARN if self.msg.startswith(('Failed', 'Cannot')) else FG)
        ctext(d, W / 2, 262, 'Left soft key: actions', font(10), DIM)
    def key(self, k): return 'back' if k in ('RSK', 'END') else None

def check_updates_bg():
    """once per boot, after WiFi is up: tell the user (notification) if the system or an installed app has an update"""
    for _ in range(30):
        time.sleep(10)
        if wlan_ip(): break
    else: return
    try:
        idx = fetch_index(); sysv = (idx.get('system') or {}).get('version')
        if sysv and vtuple(sysv) > vtuple(os_version()): notify('System update available', 'jgmDeb ' + sysv + ' (Settings > Software update)')
        inst = installed_apps(); n = [a['name'] for a in idx.get('apps', []) if a['id'] in inst and vtuple(a['version']) > vtuple(inst[a['id']]['version'])]
        if n: notify('App updates available', ', '.join(n))
    except Exception: pass

def load_installed_apps():
    for man in installed_apps().values(): register_app(man)

APP_META = [  # id, name, glyph, colour, factory
    ('voicemail', 'Voicemail', '☎', (120, 130, 240), lambda: Stub('Voicemail', ['Not available yet', 'needs the modem'])),
    ('phone', 'Phone', '☎', (60, 190, 120), Phone), ('contacts', 'Contacts', '☺', (70, 150, 240), Contacts),
    ('messages', 'Messages', '✉', (240, 170, 60), Messages), ('settings', 'Settings', '⚙', (150, 160, 170), Settings),
    ('clock', 'Clock', '⏰', (230, 110, 100), Clock), ('calc', 'Calculator', '+−', (170, 120, 230), Calc),
    ('browser', 'Browser', '⌁', (60, 200, 220), lambda: Stub('Browser', ['Needs WiFi or mobile data.', 'Neither works yet.'])),
    ('notes', 'Notes', '✎', (240, 210, 80), Notes), ('files', 'Files', '⌂', (180, 200, 120), Files),
    ('terminal', 'Terminal', '>_', (80, 90, 100), Terminal), ('calendar', 'Calendar', '☷', (240, 110, 150), Calendar),
    ('torch', 'Flashlight', '☀', (250, 220, 100), Torch), ('snake', 'Snake', '●', (90, 210, 90), Snake),
    ('camera', 'Camera', '◉', (200, 90, 90), lambda: Stub('Camera', ['Camera not supported yet', 'needs the vendor camera stack'])),
    ('music', 'Music', '♫', (150, 110, 220), MusicApp), ('radio', 'Radio', '', (110, 200, 190), Radio), ('news', 'News', '', (240, 120, 100), FeedList), ('weather', 'Weather', '', (90, 170, 245), Weather),
    ('store', 'Store', '', (70, 140, 220), StoreApp), ('ytdl', 'YT download', '', (240, 80, 80), YtDl), ('notifs', 'Notifications', '', (255, 190, 80), NotifApp), ('hotspot', 'Hotspot', '', (120, 210, 150), Hotspot),
    ('wifi', 'WiFi', '≈', (80, 170, 240), WifiApp),
    ('monitor', 'Monitor', '', (60, 80, 100), lambda: Monitor()), ('battery', 'Battery', '', (110, 224, 140), lambda: BatteryApp()),
    ('alarm', 'Alarm', '', (255, 150, 120), lambda: AlarmApp())]
ALL_APPS_META = [(a[0], a[1]) for a in APP_META]
APP_BY_ID = {a[0]: a for a in APP_META}
load_installed_apps()

class Grid(App):
    """Home screen (9 configurable keys) or all-apps view."""
    def __init__(self, ids, home=True): self.ids = ids; self.sel = 0; self.home = home; self.left = 'Menu' if home else ''; self.right = 'Contacts' if home else 'Back'
    def draw(self, d, img):
        if CFG['wallpaper'] == 'gradient':
            for y in range(24, H - 26, 4): c = int(8 + (y - 24) * 18 / (H - 50)); d.rectangle((0, y, W, y + 4), fill=(c, c + 10, c + 14) if CFG['dark'] else (238 - c, 242 - c, 242 - c))
        page = self.sel // 9 * 9; shown = self.ids[page:page + 9]
        for i, aid in enumerate(shown):
            a = APP_BY_ID.get(aid)
            if not a: continue
            cx, cy = 40 + (i % 3) * 80, 62 + (i // 3) * 84
            if page + i == self.sel: d.rounded_rectangle((cx - 36, cy - 30, cx + 36, cy + 48), max(2, rr() + 4), outline=ACC, width=2)
            if icon: ic = icon(a[0], 46, ICON_THEMES[CFG.get('icon_theme', 0) % 5], CFG.get('radius', 2)); img.paste(ic, (cx - 23, cy - 26), ic)
            else: d.ellipse((cx - 22, cy - 24, cx + 22, cy + 20), fill=a[3])
            if self.home and page == 0: d.text((cx - 32, cy - 28), str(i + 1), font=font(10, True), fill=ACC)
            ctext(d, cx, cy + 24, fit(d, a[1], font(11, True), 74), font(11, True), FG)
        if len(self.ids) > 9: ctext(d, W / 2, 290, 'page %d/%d' % (page // 9 + 1, (len(self.ids) + 8) // 9), font(10), DIM)
    def key(self, k):
        n = len(self.ids)
        if self.home and len(k) == 1 and k in '123456789':
            i = int(k) - 1
            if i < len(self.ids): return ('open', APP_BY_ID[self.ids[i]][4]()) if self.ids[i] in APP_BY_ID else None
        elif self.home and k == '0': return ('open', APP_BY_ID['files'][4]())
        elif k == 'UP': self.sel = (self.sel - 3) % n
        elif k == 'DOWN': self.sel = (self.sel + 3) % n
        elif k == 'LEFT': self.sel = (self.sel - 1) % n
        elif k == 'RIGHT': self.sel = (self.sel + 1) % n
        elif k in ('OK', 'CALL'): return ('open', APP_BY_ID[self.ids[self.sel]][4]())
        elif k == 'LSK' and self.home: return ('open', Grid([a[0] for a in APP_META], False))
        elif k == 'RSK':
            if self.home: return ('open', Contacts())
            return 'back'
        elif k == 'END' and not self.home: return 'back'

# --------------------------------------------------------------------------------------------- main loop
def render(app):
    img = Image.new('RGB', (W, H), BG); d = ImageDraw.Draw(img)
    try: app.draw(d, img)
    except Exception:
        traceback.print_exc(); d.rectangle((0, 24, W, H - 28), fill=BG); d.text((10, 40), 'This screen hit an error.', font=font(14, True), fill=RED); d.text((10, 64), 'Press Back.', font=font(13), fill=FG)
    left = app.left_label() if hasattr(app, 'left_label') else app.left
    chrome(d, getattr(app, 'title', ''), left, getattr(app, 'right', 'Back')); return img

def handle(stack, res):
    """apply a key result to the app stack"""
    if res is None: return
    if res == 'back':
        if len(stack) > 1: stack.pop()
    elif res == 'popback': stack.pop()                                   # close the popup
    elif res == 'popback2': stack.pop(); stack.pop() if len(stack) > 1 else None
    elif isinstance(res, tuple):
        if res[0] == 'open': stack.append(res[1])
        elif res[0] == 'replace': stack.pop(); stack.append(res[1])
        elif res[0] == 'popopen': stack.pop(); stack.append(res[1])
        elif res[0] == 'run': subprocess.call(res[1], shell=True)
        elif res[0] == 'popit':                                          # run the chosen option, then apply ITS result
            stack.pop(); r = res[1]()
            if r == 'popback': r = None
            elif r == 'popback2': r = 'back'
            elif isinstance(r, tuple) and r[0] == 'popopen': r = ('open', r[1])
            handle(stack, r)
        elif res[0] == 'popset':                                         # home layout slot chosen
            i, aid = res[1]; CFG['home'][i] = aid; save_cfg()

def home_ids():
    """the 9 configured home keys, then every other app (so the D-pad scrolls on without opening the Menu)"""
    return list(CFG['home']) + [a[0] for a in APP_META if a[0] not in CFG['home']]

def main():
    try: os.setgroups([0, 3003, 3004])           # Android-kernel network groups (inet, inet_raw): ping and apt need them
    except Exception: pass
    scr, keys = Screen(), Keys(); subprocess.call('pkill jgmscreen 2>/dev/null', shell=True)
    STATE['slider_closed'] = bool(CFG.get('slider_enabled', True) and keys.slider_closed())
    stack = [Grid(home_ids())]
    if not CFG['setup_done']: stack.append(Setup())
    S = {'on': True, 'locked': False, 'lock': None, 'last': time.time()}
    def screen(on):
        S['on'] = on; wr(LCD, CFG['brightness'] if on else 0); wr(KBL, 12 if on else 0)
    def lock():
        if CFG['autolock'] and CFG['setup_done']: S['locked'] = True; S['lock'] = LockScreen()
    screen(True)
    if STATE['slider_closed'] and CFG['slider_lock'] and CFG['setup_done']: lock(); screen(False)
    threading.Thread(target=check_updates_bg, daemon=True).start()
    dirty = True; last_tick = 0; last_alarm = 0; last_batt = 0; battery()
    if CFG.get('governor'): BATT.governor(CFG['governor'])
    while True:
        now = time.time()
        if S.get('unseen') and not S['on']: wr('/sys/class/leds/red/brightness', 60 if int(now * 1.5) % 3 == 0 else 0)
        if now - last_batt > 5:
            last_batt = now; BATT.update(); BATT.charge_led(BATT.charging and not S['on'])
            S['unseen'] = len(unseen_notifs())
            for ev, p in BATT.pop_events():
                msg = {'low': 'Battery low (%d%%).\nConnect a charger.', 'critical': 'Battery critical (%d%%)!\nShutting down soon.', 'shutdown': 'Battery empty.\nShutting down in 20 s.'}[ev] % p
                if not S['on']: screen(True)
                notify('Battery ' + ev, msg.replace('\n', ' ')); S['locked'] = False; S['last'] = now; stack.append(TextBox('Battery', msg, 'OK')); dirty = True
            if BATT.shutdown_due(): save_cfg(); BATT.save(now); subprocess.call('sync; poweroff', shell=True)
            if isinstance(stack[-1], (BatteryApp, Monitor, Grid)) or S['locked']: dirty = True
        app = S['lock'] if S['locked'] else stack[-1]
        if S['on'] and CFG['timeout'] and now - S['last'] > CFG['timeout'] and not isinstance(app, (Terminal, Ringing)):
            lock(); screen(False)
        if CFG['alarm']['on'] and now - last_alarm > 5:
            last_alarm = now; n = datetime.datetime.now(); a = CFG['alarm']; day = n.strftime('%Y-%m-%d')
            if n.hour == a['h'] and n.minute == a['m'] and a.get('fired') != day:
                a['fired'] = day; save_cfg(); S['locked'] = False; screen(True); S['last'] = now; stack.append(Ringing()); dirty = True
        if isinstance(app, Terminal) and app.pump(): dirty = True
        if S['on'] and (dirty or (app.tick and now - last_tick > app.tick)) and now - scr.last >= 0.12:
            if isinstance(stack[0], Grid) and stack[0].home: stack[0].ids = home_ids()
            scr.show(render(app)); dirty = False; last_tick = now
        for k in keys.poll(0.12):
            if k in ('SLIDE_CLOSE', 'SLIDE_OPEN'):
                if not CFG.get('slider_enabled', True): continue
                STATE['slider_closed'] = (k == 'SLIDE_CLOSE'); dirty = True
                if CFG.get('slider_calls', True) and isinstance(stack[-1], Calling) and not S['locked']:      # slide open = answer, slide closed = hang up
                    handle(stack, stack[-1].key('CALL' if k == 'SLIDE_OPEN' else 'END')); continue
                if STATE['slider_closed'] and CFG['slider_lock']: lock(); screen(False)
                elif not STATE['slider_closed']:
                    screen(True); S['last'] = time.time()
                    if CFG['slide_unlock'] and not CFG['pin']: S['locked'] = False
                continue
            if k == 'POWER_LONG':
                screen(True); S['locked'] = False; S['last'] = time.time()
                if not isinstance(stack[-1], Popup) or stack[-1].title != 'Power': stack.append(power_menu())
                dirty = True; continue
            if not S['on']: screen(True); S['last'] = time.time(); dirty = True; continue          # first key only wakes the screen
            S['last'] = time.time(); app = S['lock'] if S['locked'] else stack[-1]
            if S['locked'] and k == 'END': screen(False); continue
            if S['locked']:
                r = app.key(k)
                if r == 'unlock':
                    if CFG['pin'] and not isinstance(app, PinEntry): S['lock'] = PinEntry()
                    else: S['locked'] = False
                elif r == 'cancel': S['lock'] = LockScreen()
                dirty = True; continue
            if k == 'END' and len(stack) == 1 and isinstance(app, Grid):                            # red key on the home screen: sleep
                lock(); screen(False); continue
            try:
                if k == 'LSK' and not isinstance(app, (Popup, Grid, Editor)) and app.options(): res = ('open', Popup('Options', app.options()))
                elif k == 'LSK' and isinstance(app, Editor): res = ('open', Popup('Options', app.options()))
                else: res = app.key(k)
            except Exception: res = None; traceback.print_exc()
            handle(stack, res); dirty = True
        if not dirty and not (S['lock'] or stack[-1]).tick: time.sleep(0.02)

if __name__ == '__main__':
    while True:
        try: main()
        except KeyboardInterrupt: break
        except Exception: traceback.print_exc(); time.sleep(2)
