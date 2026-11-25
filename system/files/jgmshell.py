#!/usr/bin/env python3
"""jgmDeb phone shell v2 (own work): keypad launcher + apps, drawn straight to /dev/fb0 (240x320 RGB565 SPI panel).

No toolkit: Pillow draws an RGB image, numpy converts it to RGB565, one FBIOPAN_DISPLAY pushes it. The panel takes ~100 ms per
full frame, so we redraw only when something changed (never faster than ~8 fps).

Keys (evdev matrix_keypad): 1..9 0 * #   LSK (KEY_MENU)  RSK (KEY_BACK)  D-pad  OK  CALL (KEY_SEND)  END (power key)
Soft keys everywhere: LEFT soft = options for the current screen (when it shows a label), RIGHT soft = back / clear.
"""
import math, threading, os, sys, time, struct, mmap, fcntl, glob, json, select, datetime, subprocess, traceback, random, pty, signal
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
ACCENTS = [('Theme', None), ('Green', (45, 226, 166)), ('Blue', (80, 160, 255)), ('Orange', (250, 160, 60)), ('Pink', (240, 100, 170)), ('Yellow', (240, 210, 80))]
DEFAULT_CFG = {'accent': 0, 'wallpaper': 'none', 'clock24': True, 'fontscale': 1, 'timeout': 30,
               'home': ['voicemail', 'phone', 'contacts', 'messages', 'settings', 'clock', 'calc', 'browser', 'notes'], 'dark': True,
               'setup_done': False, 'pin': '', 'autolock': True, 'slider_enabled': True, 'slider_lock': True, 'slide_unlock': True, 'slider_flip': False, 'brightness': 255, 'icon_theme': 0, 'radius': 2, 'ui_theme': 0, 'wall': 'auto',
               'alarm': {'on': False, 'h': 7, 'm': 0, 'fired': ''}}
def load_cfg():
    c = dict(DEFAULT_CFG)
    try: c.update(json.load(open(DATA + '/config.json')))
    except Exception: pass
    try:
        if 'ui_theme' not in json.load(open(DATA + '/config.json')): c['ui_theme'] = 0 if c.get('dark', True) else 1
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
# name, background, panel (bars, selection), text, dim text, accent, wallpaper texture, matching icon style
UI_THEMES = [('Dark', (10, 18, 20), (26, 42, 48), (228, 238, 238), (116, 138, 140), (45, 226, 166), None, 'normal'),
             ('Light', (238, 242, 242), (210, 220, 222), (16, 24, 26), (100, 112, 114), (14, 150, 108), None, 'normal'),
             ('Midnight', (8, 12, 28), (26, 36, 72), (222, 230, 250), (110, 124, 170), (110, 160, 255), None, 'glass'),
             ('Forest', (10, 22, 14), (30, 56, 38), (226, 240, 226), (120, 150, 124), (130, 214, 110), None, 'normal'),
             ('Sunset', (28, 14, 26), (70, 34, 58), (252, 232, 226), (176, 128, 130), (255, 150, 90), None, 'glass'),
             ('Nord', (46, 52, 64), (59, 66, 82), (236, 239, 244), (150, 160, 178), (136, 192, 208), None, 'normal'),
             ('Solarized', (0, 43, 54), (7, 54, 66), (238, 232, 213), (131, 148, 150), (42, 161, 152), None, 'normal'),
             ('Paper', (240, 232, 214), (222, 211, 188), (44, 36, 28), (128, 116, 98), (176, 84, 40), 'paper', 'paper'),
             ('Wood', (46, 30, 18), (74, 48, 28), (248, 232, 208), (184, 154, 116), (244, 176, 84), 'wood', 'wood'),
             ('Metal', (44, 48, 54), (76, 82, 90), (238, 242, 246), (164, 172, 182), (124, 192, 255), 'metal', 'metal'),
             ('High contrast', (0, 0, 0), (34, 34, 34), (255, 255, 255), (200, 200, 200), (255, 226, 0), None, 'mono'),
             ('Terminal', (0, 10, 2), (0, 30, 10), (130, 255, 160), (52, 150, 82), (170, 255, 110), None, 'neon')]
ICON_STYLES = ['auto', 'normal', 'dark', 'metal', 'wood', 'paper', 'glass', 'neon', 'mono']
def theme():
    global BG, PANEL, FG, DIM, ACC, WARN, RED
    i = CFG.get('ui_theme', 0) % len(UI_THEMES); _, BG, PANEL, FG, DIM, acc, _, _ = UI_THEMES[i]
    ACC = ACCENTS[CFG['accent'] % len(ACCENTS)][1] or acc; WARN, RED = (250, 176, 60), (236, 92, 80)
    CFG['dark'] = sum(BG) < 330
theme()
_bgc = {}
def icon_style():
    n = ICON_STYLES[CFG.get('icon_theme', 0) % len(ICON_STYLES)]
    return UI_THEMES[CFG.get('ui_theme', 0) % len(UI_THEMES)][7] if n == 'auto' else n
def bg_image():
    """wallpaper for the current theme: its own texture (wood/metal/paper), a soft gradient, or flat colour"""
    th = CFG.get('ui_theme', 0) % len(UI_THEMES); mode = CFG.get('wall', 'auto'); key = (th, mode, BG, ACC)
    if key not in _bgc:
        tex = UI_THEMES[th][6]
        if mode in ('auto', 'texture') and tex and icon_mod: im = icon_mod.texture(tex)
        elif mode in ('auto', 'gradient'):
            im = Image.new('RGB', (W, H), BG); dd = ImageDraw.Draw(im)
            for y in range(H): t = y / (H - 1) * 0.12; dd.line((0, y, W, y), fill=tuple(int(BG[j] * (1 - t) + ACC[j] * t) for j in range(3)))
        else: im = Image.new('RGB', (W, H), BG)
        _bgc.clear(); _bgc[key] = im
    return _bgc[key].copy()
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
UI_RADIUS = [0, 3, 6, 10, 14]
def rr(): return UI_RADIUS[max(0, min(4, CFG.get('radius', 2)))]
def rrect(d, box, fill=None, outline=None, width=1): d.rounded_rectangle(box, rr(), fill=fill, outline=outline, width=width)
def now_str(): return datetime.datetime.now().strftime('%H:%M' if CFG['clock24'] else '%I:%M %p').lstrip('0')
def chrome(d, title, left, right):
    if rr(): rrect(d, (3, 2, W - 3, 21), fill=PANEL)
    else: d.rectangle((0, 0, W, 22), fill=PANEL)
    d.text((6, 3), now_str(), font=font(13, True), fill=FG)
    if STATE['slider_closed'] and CFG.get('slider_enabled', True): d.text((tw(d, now_str(), font(13, True)) + 14, 6), 'SCL', font=font(10, True), fill=WARN)
    cap, st, _ = battery(); bt = cap + '%' + (' +' if st.lower().startswith('charg') else '')
    d.text((W - 6 - tw(d, bt, font(12)), 4), bt, font=font(12), fill=ACC if cap not in ('0', '1', '2', '3') else WARN)
    if title: ctext(d, W / 2, 4, title, font(12, True), DIM)
    if rr(): rrect(d, (3, H - 25, W - 3, H - 3), fill=PANEL)
    else: d.rectangle((0, H - 26, W, H), fill=PANEL)
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
        rrect(d, (2, 24, W - 2, H - 28), fill=BG, outline=PANEL, width=2)
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
    def __init__(self, title, items): self.title = title; self.items = items; self.sel = 0; self.left = ''; self.right = 'Back'; self.top = 0
    def layout(self, d):
        """-> [(label lines, value lines, height)] for every item; the selected one grows to show all of its text, the others are cut to one line"""
        lf, vf = font(14), font(12); lh = lf.size + 4; avail = W - 20; out = []
        for i, it in enumerate(self.items):
            v = str(it[1]()) if it[1] else ''; lab = it[0]
            if tw(d, lab, lf) + (tw(d, v, vf) + 10 if v else 0) <= avail: out.append(([lab], [v] if v else [], lh + 10, True)); continue
            if i != self.sel:
                vw = min(tw(d, v, vf), avail * 0.45) if v else 0
                out.append(([fit(d, lab, lf, avail - vw - 10)], [fit(d, v, vf, vw)] if v else [], lh + 10, True)); continue
            ll = wrap(d, lab, lf, avail)[:3]; vl = wrap(d, v, vf, avail)[:3] if v else []
            out.append((ll, vl, len(ll) * lh + len(vl) * (vf.size + 3) + 10, False))
        return out
    def draw(self, d, img):
        lay = self.layout(d); area = H - 28 - 27; self.sel = max(0, min(self.sel, len(lay) - 1)) if lay else 0
        if self.top > self.sel: self.top = self.sel
        while self.top < self.sel and sum(x[2] for x in lay[self.top:self.sel + 1]) > area: self.top += 1
        lf, vf = font(14), font(12); y = 27
        for i in range(self.top, len(lay)):
            ll, vl, h, single = lay[i]
            if y + h > 27 + area and i != self.sel: break
            sel = i == self.sel
            if sel: rrect(d, (3, y - 1, W - 3, y + h - 3), fill=PANEL)
            if single:
                d.text((10, y + 4), ll[0], font=lf, fill=FG)
                if vl: d.text((W - 8 - tw(d, vl[0], vf), y + 6), vl[0], font=vf, fill=ACC)
            else:
                yy = y + 4
                for l in ll: d.text((10, yy), l, font=lf, fill=FG); yy += lf.size + 4
                for l in vl: d.text((10, yy), l, font=vf, fill=ACC); yy += vf.size + 3
            y += h
        if self.top > 0: d.polygon([(W - 9, 28), (W - 4, 28), (W - 6.5, 24)], fill=DIM)
        if y > 27 + area or self.top + sum(1 for _ in lay[self.top:]) > 0 and y >= 27 + area: d.polygon([(W - 9, H - 33), (W - 4, H - 33), (W - 6.5, H - 29)], fill=DIM)
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
            rrect(d, (x, y, x + 56, y + 30), fill=PANEL); ctext(d, x + 28, y + 5, c, font(18, True), FG)
    def key(self, k):
        if len(k) == 1 and k in '0123456789*#': self.num += k; self.msg = ''
        elif k == 'RSK':
            if self.num: self.num = self.num[:-1]
            else: return 'back'
        elif k == 'END': return 'back'
        elif k in ('CALL', 'OK'): return ('open', Calling(self.num)) if self.num else None
try: import jgmvoice, jgmsim
except Exception: jgmvoice = jgmsim = None
try:
    import jgmsave; PWR = jgmsave.Power()
except Exception: jgmsave = PWR = None
CALL_ANSWERED = set()
def contact_name(num):
    n = re.sub(r'[^0-9+]', '', num or '')
    for c in jload('contacts.json', []):
        if n and re.sub(r'[^0-9+]', '', c['number'])[-8:] == n[-8:]: return c['name']
    return ''
class Calling(App):
    """A voice call: outgoing (dials through the modem) or an answered incoming call (cid given). END hangs up, OK toggles speaker, digits send DTMF."""
    title = 'Call'; right = 'End'; tick = 1
    def __init__(self, num, cid=None):
        self.num = num; self.cid = cid; self.t0 = None; self.state = 'Dialing...' if cid is None else 'Connecting...'; self.err = ''; self.speaker = False; self.finished = False; self.finished_at = 0; self.audio = False; self.rb = False
        if cid is None: jsave('calls.json', (jload('calls.json', []) + [{'n': num, 't': time.time(), 'dir': 'out'}])[-50:])
        threading.Thread(target=self.run, daemon=True).start()
    def finish(self, text):
        self.state = text; self.finished = True; self.finished_at = time.time() + 2
        if self.rb: jgmvoice.ringback(False); self.rb = False
        if self.audio: jgmvoice.audio('off'); self.audio = False
    def run(self):
        try:
            if jgmvoice is None: self.err = 'Voice support missing'; return self.finish('Failed')
            if not jgmsim.modem_up(): self.err = 'The modem is off. Settings > Modem / audio > Start modem.'; return self.finish('No modem')
            if self.cid is None:
                st = jgmsim.setup(25)
                if not st['ok']: self.err = st['why']; return self.finish('No SIM')
                if not st.get('registered'): self.err = 'No network yet'; return self.finish('No network')
                jgmvoice.register(); self.cid = jgmvoice.dial(self.num); jgmvoice.audio('speaker' if self.speaker else 'handset'); self.audio = True       # voice path up from the start, so ringback is heard
            seen = False; started = time.time()
            while not self.finished:
                time.sleep(1); cs = {c['id']: c for c in jgmvoice.calls()}; c = cs.get(self.cid)
                if not c:
                    if seen or time.time() - started > 8:
                        why = jgmvoice.end_reason_text(self.cid); jgmvoice.debug('call %s ended: %s (seen=%s)' % (self.cid, why or 'no reason', seen)); self.err = ('Reason: ' + why) if why else ''; return self.finish(why or 'Call ended')
                    continue
                seen = True; name = c['state_name']
                if c['state'] != getattr(self, 'last_state', None): jgmvoice.debug('call %d state %d (%s) dir %s' % (self.cid, c['state'], name, c['dir'])); self.last_state = c['state']
                if c['state'] == 5 and not self.rb: jgmvoice.ringback(True); self.rb = True           # far end is ringing: play the local ringback tone
                if c['state'] != 5 and self.rb: jgmvoice.ringback(False); self.rb = False
                if c['state'] == 3 and self.t0 is None:
                    self.t0 = time.time(); self.state = 'Connected'
                    if not self.audio: jgmvoice.audio('speaker' if self.speaker else 'handset'); self.audio = True
                elif c['state'] != 3: self.state = {1: 'Dialing...', 5: 'Ringing...', 10: 'Dialing...', 8: 'Ending...'}.get(c['state'], name)
        except Exception as e: self.err = str(e)[:60]; jgmvoice.debug('call exception: ' + repr(e)); self.finish('Call failed')
    def draw(self, d, img):
        nm = contact_name(self.num); ctext(d, W / 2, 56, nm or self.num, font(22, True), FG)
        if nm: ctext(d, W / 2, 86, self.num, font(13), DIM)
        txt = self.state if self.t0 is None else '%02d:%02d' % divmod(int(time.time() - self.t0), 60)
        ctext(d, W / 2, 120, txt, font(26 if self.t0 else 16, True), ACC)
        if self.t0: ctext(d, W / 2, 160, 'Speaker ' + ('on' if self.speaker else 'off') + '  (OK)', font(12), DIM)
        for i, l in enumerate(wrap(d, self.err, font(12), W - 20)): ctext(d, W / 2, 190 + i * 15, l, font(12), WARN)
    def key(self, k):
        if k in ('RSK', 'END'):
            if self.cid is not None and jgmvoice: threading.Thread(target=lambda: jgmvoice.hangup(self.cid), daemon=True).start()
            if self.rb: jgmvoice.ringback(False); self.rb = False
            if self.audio: jgmvoice.audio('off')
            return 'back'
        if k == 'OK' and self.t0: self.speaker = not self.speaker; jgmvoice.audio('speaker' if self.speaker else 'handset')
        elif len(k) == 1 and k in '0123456789*#' and self.t0 and self.cid is not None: threading.Thread(target=lambda: jgmvoice.send_dtmf(self.cid, k), daemon=True).start()
class IncomingCall(App):
    title = 'Incoming call'; left = 'Answer'; right = 'Decline'; tick = 1
    def __init__(self, call): self.call = call; self.finished = False; self.finished_at = 0; self.t = time.time()
    def draw(self, d, img):
        nm = contact_name(self.call['number']); ctext(d, W / 2, 56, nm or self.call['number'] or 'Unknown', font(22, True), FG)
        if nm: ctext(d, W / 2, 86, self.call['number'], font(13), DIM)
        ctext(d, W / 2, 130, 'Incoming call', font(16), ACC if int(time.time() * 2) % 2 else FG)
        ctext(d, W / 2, 170, 'CALL / slide open = answer', font(11), DIM); ctext(d, W / 2, 188, 'END = decline', font(11), DIM)
    def answer(self):
        CALL_ANSWERED.add(self.call['id'])
        try: jgmvoice.answer(self.call['id'])
        except Exception: pass
        return ('replace', Calling(self.call['number'], self.call['id']))
    def key(self, k):
        if k in ('LSK', 'CALL', 'OK'): return self.answer()
        if k in ('RSK', 'END'):
            threading.Thread(target=lambda: jgmvoice.hangup(self.call['id']), daemon=True).start(); CALL_ANSWERED.add(-self.call['id']); return 'back'
INCOMING = []; ENDED = set()
def on_incoming_call(c): INCOMING.append(c)
def on_call_end(c):
    ENDED.add(c['id'])
    if c['dir'] == 'in' and c['id'] not in CALL_ANSWERED and -c['id'] not in CALL_ANSWERED and c['state'] != 3:
        jsave('calls.json', (jload('calls.json', []) + [{'n': c['number'], 't': time.time(), 'dir': 'in', 'seen': False}])[-50:]); notify('Missed call', contact_name(c['number']) or c['number'])
    CALL_ANSWERED.discard(c['id']); CALL_ANSWERED.discard(-c['id'])
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
            if sel: rrect(d, (3, y - 2, W - 3, y + 27), fill=PANEL)
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
    """Import the contacts stored on the SIM card (ADN) through the modem."""
    title = 'SIM contacts'; left = ''; tick = 1
    def __init__(self, cb):
        self.cb = cb; self.msg = 'Starting...'; self.found = []; self.new = []; self.done = False; self.pin = None
        threading.Thread(target=self.work, daemon=True).start()
    @staticmethod
    def norm(n): return re.sub(r'[^0-9+]', '', n or '')
    def work(self):
        try:
            import jgmsim
            if not jgmsim.modem_up():
                self.msg = 'Starting the modem...'; subprocess.Popen('sh /opt/jgm/modem-up.sh', shell=True, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(40):
                    time.sleep(3)
                    if jgmsim.modem_up(): break
                else: self.msg = 'The modem did not start'; return
            self.msg = 'Reading the SIM...'; st = jgmsim.setup(30)
            if not st['ok']:
                self.msg = st['why'] + (' (%s tries left)' % st.get('pin_retries', '?') if st.get('pin_retries') is not None else ''); self.need_pin = st['why'] == 'PIN required'; return
            self.found = jgmsim.read_adn(); have = {self.norm(c['number']) for c in jload('contacts.json', [])}
            self.new = [(n, num) for n, num in self.found if self.norm(num) not in have]
            self.msg = '%d on the SIM, %d new' % (len(self.found), len(self.new)); self.done = True
        except Exception as e: self.msg = 'Failed: ' + str(e)[:50]
    def draw(self, d, img):
        for i, l in enumerate(wrap(d, self.msg, font(15, True), W - 20)): d.text((10, 36 + i * 22), l, font=font(15, True), fill=ACC if self.done else FG)
        for i, (n, num) in enumerate(self.new[:7]): d.text((10, 90 + i * 22), fit(d, n + '  ' + num, font(13), W - 20), font=font(13), fill=FG)
        if len(self.new) > 7: d.text((10, 90 + 7 * 22), '+ %d more' % (len(self.new) - 7), font=font(12), fill=DIM)
        if self.done and self.new: ctext(d, W / 2, H - 52, 'OK = import %d contacts' % len(self.new), font(13, True), ACC)
        elif self.done: ctext(d, W / 2, H - 52, 'Nothing new to import', font(13), DIM)
    def key(self, k):
        if k == 'OK' and self.done and self.new:
            cs = jload('contacts.json', []); cs += [{'name': n or num, 'number': num} for n, num in self.new]; jsave('contacts.json', cs); self.cb()
            notify('SIM contacts imported', '%d contacts' % len(self.new)); self.new = []; self.msg = 'Imported!'
        elif k == 'OK' and getattr(self, 'need_pin', False):
            def pin_done(pin):
                import jgmsim
                ok, err = jgmsim.verify_pin(pin); self.need_pin = False; self.msg = 'Starting...'; threading.Thread(target=self.work, daemon=True).start() if ok else setattr(self, 'msg', 'Wrong PIN')
            return ('open', Editor('SIM PIN', '', numeric=True, on_done=pin_done))
        elif k in ('RSK', 'END'): return 'back'
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
            if sel: rrect(d, (3, y - 2, W - 3, y + 35), fill=PANEL)
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
        rrect(d, (8, 30, W - 8, 100), fill=PANEL, outline=DIM)
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
                if day == self.d: rrect(d, (x - 14, y - 3, x + 14, y + 22), fill=ACC); ctext(d, x, y, str(day), font(14, True), (10, 18, 20))
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
            if top + i == self.sel: rrect(d, (3, y - 1, W - 3, y + 20), fill=PANEL)
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
            if i == self.f: rrect(d, (3, y - 4, W - 3, y + 28), fill=PANEL)
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
    def theme_t(): CFG['ui_theme'] = (CFG.get('ui_theme', 0) + 1) % len(UI_THEMES); save_cfg(); theme()
    def clk(): CFG['clock24'] = not CFG['clock24']; save_cfg()
    def fscale(): CFG['fontscale'] = {1: 2, 2: 3, 3: 1}.get(CFG['fontscale'], 1); save_cfg(); _fc.clear()
    def bright():
        p = bl_path()
        if not p: return None
        cur = int(rd(p, '0') or 0); mx = int(rd(os.path.dirname(p) + '/max_brightness', '255') or 255)
        nxt = max(10, int(((cur * 4 // mx) + 1) % 5 * mx / 4) or mx // 4); open(p, 'w').write(str(nxt))
    def tout(): CFG['timeout'] = {15: 30, 30: 60, 60: 0, 0: 15}.get(CFG['timeout'], 30); save_cfg()
    display = ListMenu('Display', [('Brightness', lambda: (rd(bl_path() or '') or 'n/a'), lambda: bright()), ('Theme', lambda: UI_THEMES[CFG.get('ui_theme', 0) % len(UI_THEMES)][0], theme_t),
                                   ('Accent colour', lambda: ACCENTS[CFG['accent'] % len(ACCENTS)][0], accent), ('Text size', lambda: ['', 'Normal', 'Large', 'Huge'][CFG['fontscale']], fscale),
                                   ('Icon style', lambda: ICON_STYLES[CFG.get('icon_theme', 0) % len(ICON_STYLES)].title(), lambda: (CFG.__setitem__('icon_theme', (CFG.get('icon_theme', 0) + 1) % len(ICON_STYLES)), save_cfg())[1]),
                                   ('Nightstand clock', lambda: ['Off', 'Timed', 'On charge'][ns_mode()], lambda: ('open', ns_menu())),
                                   ('Wallpaper', lambda: CFG.get('wall', 'auto').title(), lambda: (CFG.__setitem__('wall', {'auto': 'gradient', 'gradient': 'flat', 'flat': 'auto'}.get(CFG.get('wall', 'auto'), 'auto')), save_cfg())[1]),
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


def adb_menu():
    usb = lambda: os.path.exists(DATA + '/adb-usb'); net = lambda: rd(DATA + '/adb-tcp') if os.path.exists(DATA + '/adb-tcp') else ''
    def tog_usb():
        if usb(): os.remove(DATA + '/adb-usb')
        else: open(DATA + '/adb-usb', 'w').close()
    def set_net(v):
        if v is None:
            if os.path.exists(DATA + '/adb-tcp'): os.remove(DATA + '/adb-tcp')
        else: open(DATA + '/adb-tcp', 'w').write(v)
        return 'popback2'
    def tog_net():
        if net(): set_net(None); return None
        return ('open', Popup('adb over WiFi', [('Enable (anyone on the WiFi gets a root shell)', lambda: set_net('lan')), ('Cancel', lambda: 'popback')]))
    return ListMenu('ADB', [('USB debugging', lambda: 'On' if usb() else 'Off', tog_usb),
                            ('adb over WiFi', lambda: 'On (open!)' if net() == 'lan' else 'Off', tog_net),
                            ('IP address', lambda: wlan_ip() or 'no WiFi', lambda: None),
                            ('How to use', None, lambda: ('open', TextBox('ADB', 'USB: plug the phone in and run\n  adb devices\n  adb shell\n  adb push FILE /path\n  adb pull /path FILE\n  adb reboot [recovery|bootloader|edl]\n\nWiFi: turn on "adb over WiFi", then\n  adb connect %s:5555\nThere is NO authentication: anyone on the same network gets a root shell. Use it only on networks you trust and turn it off afterwards.' % (wlan_ip() or '<phone IP>'))))])

def mobile_menu():
    cache = {'t': 0, 'st': None}
    def st():
        if time.time() - cache['t'] > 4 and jgmsim:
            cache['t'] = time.time()
            def work():
                try: cache['st'] = jgmsim.status() if jgmsim.modem_up() else None
                except Exception: cache['st'] = None
            threading.Thread(target=work, daemon=True).start()
        return cache['st']
    def mode_name(): return jgmsim.NET_MODES[jgmsim.get_mode()][0] if jgmsim else 'n/a'
    def choose():
        def pick(i):
            def f():
                def work():
                    try: jgmsim.apply_mode(i)
                    except Exception: pass
                threading.Thread(target=work, daemon=True).start(); cache['t'] = 0; return 'popback'
            return f
        items = [(n, pick(i)) for i, (n, _) in enumerate(jgmsim.NET_MODES)]
        return ('open', Popup('Network mode', items))
    def stat_text():
        v = st()
        if not jgmsim or not jgmsim.modem_up(): return 'modem off'
        if not v: return '...'
        return ('%s %s' % (v['rat'], ('%d dBm' % v['dbm']) if v['dbm'] is not None else '')) if v['registered'] else 'searching'
    return ListMenu('Mobile network', [('Network mode', mode_name, choose), ('Connection', stat_text, lambda: None),
                                       ('About modes', None, lambda: ('open', TextBox('Network modes', '2G only / 3G only / 2G + 3G: plain circuit-switched voice. Calls work like on an old phone. 2G + 3G is the default. Many operators have switched 3G off, so 3G only may show no service.\n\n4G / VoLTE: allows LTE. Voice on LTE needs an IMS data connection that this phone does not set up yet, so calls made there fail with "busy". Use it only if 2G/3G is gone in your area (and expect no calls).\n\nThe modem must be running (Settings > Modem / audio).')))])

PS_MODES = ['Off', 'Light (CPU + idle)', 'Standard (+ WiFi sleep)', 'Deep (+ suspend)']
def power_saver_menu():
    def cyc_mode():
        CFG['ps_mode'] = (CFG.get('ps_mode', 0) + 1) % 4; save_cfg()
        if CFG['ps_mode'] == 0 and PWR: PWR.screen_on()
    def drain():
        v = PWR.drain_per_hour() if PWR else None
        return 'measuring...' if v is None else '%.1f %%/h' % v
    def susp():
        if not PWR: return None
        def work():
            n = notify
            dt = PWR.suspend_once(30, 60); n('Suspend test', 'slept %.0f s (asked for 30)' % dt if dt else 'could not suspend (see /data/jgm/power.log)')
        wr(LCD, 0); threading.Thread(target=lambda: (time.sleep(1), work(), wr(LCD, CFG['brightness'])), daemon=True).start(); return None
    def live_idle():
        cur = rd('/sys/module/lpm_levels/parameters/sleep_disabled', 'Y'); wr('/sys/module/lpm_levels/parameters/sleep_disabled', 'Y' if cur == 'N' else 'N')
    def bright():
        lv = [60, 120, 190, 255]; CFG['brightness'] = lv[(min(range(4), key=lambda i: abs(lv[i] - CFG['brightness'])) + 1) % 4]; save_cfg(); wr(LCD, CFG['brightness'])
    def tout(): CFG['timeout'] = {15: 30, 30: 60, 60: 15, 0: 15}.get(CFG['timeout'], 15); save_cfg()
    return ListMenu('Power saver', [('Mode', lambda: PS_MODES[CFG.get('ps_mode', 0)], cyc_mode),
                                    ('Battery use', drain, lambda: None),
                                    ('Screen timeout', lambda: ('%ds' % CFG['timeout']) if CFG['timeout'] else 'never', tout),
                                    ('Brightness', lambda: '%d%%' % round(CFG['brightness'] * 100 / 255), bright),
                                    ('Suspend with modem on', lambda: 'Yes' if CFG.get('ps_suspend_modem') else 'No', lambda: (CFG.__setitem__('ps_suspend_modem', not CFG.get('ps_suspend_modem')), save_cfg())[1]),
                                    ('Deep CPU idle now', lambda: 'allowed' if rd('/sys/module/lpm_levels/parameters/sleep_disabled', 'Y') == 'N' else 'blocked', live_idle),
                                    ('Last suspend', lambda: ('%.0f s (%d total)' % (PWR.last_suspend, PWR.suspends)) if PWR and PWR.last_suspend else 'none', lambda: None),
                                    ('Test suspend (30 s)', None, susp),
                                    ('How it works', None, lambda: ('open', TextBox('Power saver', 'Light: after the screen is off 8 s the CPU drops to its lowest speeds, extra cores go offline and the deep CPU idle state is allowed.\n\nStandard: also turns WiFi off after 1 minute (back when you wake the phone; skipped while an ssh/adb session is open or on the charger).\n\nDeep: also suspends the whole system while the screen is off, with a wake-up timer so it never sleeps forever. Skipped on the charger, during calls/downloads/music and (by default) while the modem is on, because the modem call wake-up is unproven.\n\nTest suspend sleeps 30 s: if the screen/keys are dead afterwards, wait for the timer or hold the power key 10 s. Create /data/jgm/no-powersave to disable everything.')))])

class Settings(ListMenu):
    def __init__(self):
        items = [('Display & theme', None, lambda: ('open', make_settings())), ('Home screen keys', None, lambda: ('open', home_layout())),
                 ('Date & time', None, lambda: ('open', DateTimeSet())), ('Lock & security', None, lambda: ('open', lock_menu())),
                 ('Slider', lambda: ('' if not CFG['slider_enabled'] else ('Closed' if STATE['slider_closed'] else 'Open')), lambda: ('open', slider_menu())),
                 ('WiFi', None, lambda: ('open', WifiApp())),
                 ('Mobile network', lambda: jgmsim.NET_MODES[jgmsim.get_mode()][0] if jgmsim else 'n/a', lambda: ('open', mobile_menu())),
                 ('Modem / audio', lambda: 'Running' if audio_ok() else 'Off', lambda: ('open', modem_menu())),
                 ('Bluetooth', lambda: 'BLE' if bt_available() else 'n/a', lambda: ('open', BluetoothApp())),
                 ('ADB', lambda: ('USB' if os.path.exists(DATA + '/adb-usb') else 'Off') + (' + WiFi' if os.path.exists(DATA + '/adb-tcp') else ''), lambda: ('open', adb_menu())),
                 ('Power saver', lambda: PS_MODES[CFG.get('ps_mode', 0)].split(' ')[0], lambda: ('open', power_saver_menu())),
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
        rrect(d, (8, y + 17, W - 8, y + 25), outline=DIM); rrect(d, (9, y + 18, max(11, 8 + (W - 18) * max(0, min(100, pct)) / 100), y + 24), fill=ACC)
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
        rrect(d, (40, 130, W - 40, 160), outline=FG, width=2); rrect(d, (44, 134, max(46, 44 + (W - 88) * (int(cap) if cap.isdigit() else 0) / 100), 156), fill=ACC)
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
            para(52, 'Theme:  ' + UI_THEMES[CFG.get('ui_theme', 0) % len(UI_THEMES)][0] + '   (UP/DOWN)', 14); para(90, 'Colour:  ' + ACCENTS[CFG['accent'] % len(ACCENTS)][0] + '   (LEFT/RIGHT)', 14, ACC)
            rrect(d, (60, 140, W - 60, 220), fill=PANEL, outline=ACC, width=3); ctext(d, W / 2, 168, 'Preview', font(18, True), ACC)
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
            if k in ('UP', 'DOWN'): CFG['ui_theme'] = (CFG.get('ui_theme', 0) + (1 if k == 'DOWN' else -1)) % len(UI_THEMES); theme(); save_cfg()
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
    def __init__(self):
        self.msg = 'Search or paste a link'; self.prog = 0; self.job = None; self.results = []; self.pending = None
        if not os.path.exists('/usr/local/bin/yt-dlp'): threading.Thread(target=self.fetch_ytdlp, daemon=True).start()
    def fetch_ytdlp(self):
        try:
            self.msg = 'Installing yt-dlp...'
            req = urllib.request.Request('https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp', headers={'User-Agent': 'jgmDeb'})
            data = urllib.request.urlopen(req, timeout=90).read(); open('/usr/local/bin/yt-dlp.new', 'wb').write(data); os.chmod('/usr/local/bin/yt-dlp.new', 0o755); os.replace('/usr/local/bin/yt-dlp.new', '/usr/local/bin/yt-dlp'); self.msg = 'yt-dlp ready'
        except Exception as e: self.msg = 'yt-dlp install failed: ' + str(e)[:30]
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
        if self.job: rrect(d, (20, 220, W - 20, 236), outline=FG); rrect(d, (22, 222, max(26, 22 + (W - 44) * self.prog / 100), 234), fill=ACC)
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
# --------------------------------------------------------------------------------------------- browser (text/HTML reader) and Bluetooth
from html.parser import HTMLParser
class _Page(HTMLParser):
    """HTML -> paragraphs of words; links are numbered. Skips scripts, styles, head."""
    BLOCK = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'table', 'section', 'article', 'header', 'footer', 'blockquote', 'pre', 'form', 'hr'}
    def __init__(self, base):
        super().__init__(convert_charrefs=True); self.base = base; self.paras = [[]]; self.links = []; self.skip = 0; self.cur = None; self.head = 0; self.title = ''; self.intitle = False
    def nl(self):
        if self.paras[-1]: self.paras.append([])
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style', 'noscript', 'svg', 'template'): self.skip += 1
        elif tag == 'title': self.intitle = True
        elif tag in self.BLOCK:
            self.nl()
            if tag == 'li': self.paras[-1].append(('•', None, 0))
            if tag[0] == 'h' and len(tag) == 2: self.head = 1
        elif tag == 'a' and a.get('href') and not a['href'].startswith(('javascript:', '#', 'mailto:')):
            self.links.append(urllib.parse.urljoin(self.base, a['href'])); self.cur = len(self.links) - 1
        elif tag == 'img': self.paras[-1].append(('[img]' if not a.get('alt') else '[' + a['alt'][:20] + ']', self.cur, 0))
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg', 'template'): self.skip = max(0, self.skip - 1)
        elif tag == 'title': self.intitle = False
        elif tag in self.BLOCK:
            self.nl()
            if tag[0] == 'h' and len(tag) == 2: self.head = 0
        elif tag == 'a': self.cur = None
    def handle_data(self, data):
        if self.intitle: self.title += data.strip()
        if self.skip: return
        for w in data.split(): self.paras[-1].append((w, self.cur, self.head))
def fetch_page(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Mobile; jgmDeb) Gecko/1.0 Firefox/115.0', 'Accept': 'text/html,*/*;q=0.8', 'Accept-Language': 'en'})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(2_000_000); final = r.geturl(); ct = r.headers.get('Content-Type', '')
        enc = r.headers.get_content_charset() or (re.search(rb'charset=["\']?([\w-]+)', raw[:2000]) or [None, b'utf-8'])[1]
    if isinstance(enc, bytes): enc = enc.decode()
    if 'html' not in ct and 'text' not in ct: return final, 'Download', '[%s - %d KB: not a web page]' % (ct or 'unknown type', len(raw) // 1024), [], [[('[%s]' % (ct or 'file'), None, 0)]]
    p = _Page(final); p.feed(raw.decode(enc or 'utf-8', 'replace')); return final, p.title, '', p.links, [x for x in p.paras if x]
class Browser(App):
    title = 'Browser'; left = 'Menu'; tick = 1
    HOME = 'https://lite.duckduckgo.com/lite/'
    def __init__(self, url=None):
        self.hist = []; self.url = ''; self.page_title = ''; self.paras = []; self.links = []; self.lines = []; self.sel = -1; self.top = 0; self.msg = ''; self.pending = None; self.wrapw = None
        self.go(url or CFG.get('browser_home', self.HOME))
    def go(self, url, push=True):
        if not re.match(r'^[a-z]+://', url): url = ('https://' + url) if '.' in url and ' ' not in url else 'https://lite.duckduckgo.com/lite/?q=' + urllib.parse.quote(url)
        def work():
            self.msg = 'Loading...'
            try:
                final, title, note, links, paras = fetch_page(url)
                if push and self.url: self.hist.append(self.url)
                self.url, self.page_title, self.links, self.paras = final, title, links, paras; self.lines = []; self.top = 0; self.sel = 0 if links else -1; self.msg = note
                self.title = (title or urllib.parse.urlparse(final).netloc)[:22]
            except Exception as e: self.msg = 'Failed: ' + str(e)[:40]
        threading.Thread(target=work, daemon=True).start()
    def layout(self, d):
        f = font(12); wrap_w = W - 12; self.lines = []; space = tw(d, ' ', f)
        for para in self.paras:
            line = []; x = 0
            for w, link, head in para:
                fw = tw(d, w, font(12, bool(head)))
                if x + fw > wrap_w and line: self.lines.append(line); line = []; x = 0
                line.append((w, link, head, x)); x += fw + space
            if line: self.lines.append(line)
            self.lines.append([])
    def link_line(self, li):
        for i, l in enumerate(self.lines):
            if any(seg[1] == li for seg in l): return i
        return 0
    def options(self):
        return [('Go to address / search', lambda: ('popopen', Editor('Address or search', '', on_done=lambda u: self.go(u)))), ('Back', self.back), ('Reload', lambda: (self.go(self.url, False), 'popback')[1]),
                ('Set as home page', lambda: (CFG.__setitem__('browser_home', self.url), save_cfg(), 'popback')[2]), ('Bookmarks', lambda: ('popopen', self.bookmarks())),
                ('Add bookmark', lambda: (jsave('bookmarks.json', jload('bookmarks.json', []) + [{'t': self.page_title or self.url, 'u': self.url}]), 'popback')[1]),
                ('Download via YT downloader', lambda: ('popopen', YtDl()) if 'youtu' in self.url else None)]
    def bookmarks(self):
        bm = jload('bookmarks.json', [])
        return PickList('Bookmarks', [(b['t'], b['u']) for b in bm], lambda i: (self.go(bm[i]['u']), 'popback2')[1])
    def back(self):
        if self.hist: self.go(self.hist.pop(), False)
        return 'popback'
    def draw(self, d, img):
        if not self.lines and self.paras: self.layout(d)
        f = font(12); y0 = 26; rows = (H - 56 - y0) // 15; top = self.top
        if self.sel >= 0 and self.lines:                       # keep the selected link on screen
            ll = self.link_line(self.sel)
            if ll < top: top = ll
            elif ll >= top + rows: top = ll - rows + 1
            self.top = top
        for i, l in enumerate(self.lines[top:top + rows]):
            y = y0 + i * 15
            for w, link, head, x in l:
                sel = link is not None and link == self.sel
                fw = tw(d, w, font(12, bool(head)))
                if sel: rrect(d, (6 + x - 2, y - 1, 6 + x + fw + 2, y + 14), fill=ACC)
                d.text((6 + x, y), w, font=font(12, bool(head)), fill=(10, 18, 20) if sel else (ACC if link is not None else (FG if not head else ACC)))
        st = self.msg or (self.links[self.sel] if 0 <= self.sel < len(self.links) else '')
        d.rectangle((0, H - 46, W, H - 28), fill=BG); d.text((6, H - 44), fit(d, st or self.url, font(10), W - 12), font=font(10), fill=DIM)
        if not self.paras and not self.msg: ctext(d, W / 2, 120, 'Loading...', font(14), DIM)
    def key(self, k):
        if k == 'UP': self.top = max(0, self.top - 3);
        elif k == 'DOWN': self.top = min(max(0, len(self.lines) - 3), self.top + 3)
        elif k in ('RIGHT', '6', '#'):
            if self.links: self.sel = min(len(self.links) - 1, self.sel + 1)
        elif k in ('LEFT', '4', '*'):
            if self.links: self.sel = max(0, self.sel - 1)
        elif k in ('OK', 'CALL'):
            if 0 <= self.sel < len(self.links): self.go(self.links[self.sel])
        elif k == '0': return ('open', Popup('Browser', [('Go to address', lambda: ('popopen', Editor('Address or search', '', on_done=lambda u: self.go(u))))]))
        elif k in ('RSK', 'END'):
            if self.hist: self.go(self.hist.pop(), False)
            else: return 'back'
        elif k == '2': self.top = max(0, self.top - 12)
        elif k == '8': self.top = min(max(0, len(self.lines) - 3), self.top + 12)
        if k in ('UP', 'DOWN', '2', '8'): self.sel_from_scroll()
    def sel_from_scroll(self): pass

# ---- Bluetooth (Low Energy only: the controller is driven in user space by jgmble.py)
try: import jgmble
except Exception: jgmble = None
def bt_available(): return bool(jgmble and jgmble.BLE.available() and os.path.exists('/dev/smd3'))
class BluetoothApp(PickList):
    def __init__(self):
        self.found = {}; self.msg = 'Scanning...' if bt_available() else 'Bluetooth not available (WiFi/WCNSS not started?)'
        super().__init__('Bluetooth', lambda: [((v['name'] or 'Unknown device'), '%s   %d dBm   %s' % (a, v['rssi'], 'random' if v['type'] else 'public')) for a, v in sorted(self.found.items(), key=lambda kv: -kv[1]['rssi'])],
                         self.info, lambda: [('Scan again', lambda: (self.scan(), 'popback')[1])], status=lambda: self.msg)
        if bt_available(): self.scan()
    def scan(self):
        def work():
            try: self.msg = 'Scanning...'; self.found = jgmble.get().scan(6); self.msg = '%d devices (Low Energy only)' % len(self.found)
            except Exception as e: self.msg = 'Scan failed: ' + str(e)[:40]
        threading.Thread(target=work, daemon=True).start()
    def info(self, i):
        addr = sorted(self.found, key=lambda a: -self.found[a]['rssi'])[i]; v = self.found[addr]
        def probe():
            def work():
                try:
                    b = jgmble.get(); b.connect(addr, v['type'], 8); ch = b.discover(); nm = ''
                    try: nm = b.read('00002a00-0000-1000-8000-00805f9b34fb').decode('utf8', 'replace')
                    except Exception: pass
                    b.disconnect(); self.msg = '%s: %d services/characteristics%s' % (addr[-5:], len(ch), ('  name "%s"' % nm) if nm else '')
                except Exception as e: self.msg = 'Connect failed: ' + str(e)[:40]
                finally:
                    try: jgmble.get().disconnect()
                    except Exception: pass
            self.msg = 'Connecting...'; threading.Thread(target=work, daemon=True).start(); return 'popback'
        return ('open', Popup(v['name'] or addr, [('Connect and read info', probe), ('Cancel', lambda: 'popback')]))
# --------------------------------------------------------------------------------------------- Nightstand clock
from PIL import ImageFilter
_bf = {}
def bigfont(size, bold=True):
    k = (size, bold)
    if k not in _bf: _bf[k] = ImageFont.truetype(FDIR + ('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'), size)
    return _bf[k]
NS_DISMISSED = [False]
def ns_mode(): return CFG.get('ns_mode', 1 if CFG.get('ns_auto') else 0)
def ns_in_window():
    now = datetime.datetime.now(); m = now.hour * 60 + now.minute; a, b = CFG.get('ns_start', 21 * 60), CFG.get('ns_end', 7 * 60)
    return (a <= m < b) if a < b else (m >= a or m < b)
def ns_wanted():
    """should the Nightstand clock be shown when the phone would lock / go idle?  mode 1 = in the set time window, mode 2 = whenever locked while charging"""
    mode = ns_mode(); charging = bool(BATT and BATT.online)
    if mode == 2: return charging
    if mode == 1: return ns_in_window() and (charging or not CFG.get('ns_need_charge', True))
    return False
def fmt_min(m): return '%02d:%02d' % (m // 60, m % 60)
def parse_hhmm(t):
    t = re.sub(r'\D', '', t or '')
    if not t: return None
    if len(t) <= 2: h, mi = int(t), 0
    else: h, mi = int(t[:-2]), int(t[-2:])
    return h * 60 + mi if 0 <= h < 24 and 0 <= mi < 60 else None
def ns_menu():
    def cyc(): CFG['ns_mode'] = (ns_mode() + 1) % 3; NS_DISMISSED[0] = False; save_cfg()
    def setter(key, title):
        def done(t):
            v = parse_hhmm(t)
            if v is not None: CFG[key] = v; NS_DISMISSED[0] = False; save_cfg()
        return lambda: ('open', Editor(title + ' (HHMM, 24 h)', fmt_min(CFG.get(key, 0)).replace(':', ''), numeric=True, on_done=done))
    return ListMenu('Nightstand', [('Turn on', lambda: ['Never (manual)', 'At set times', 'On lock & charging'][ns_mode()], cyc),
                                   ('From', lambda: fmt_min(CFG.get('ns_start', 21 * 60)), setter('ns_start', 'Start time')), ('Until', lambda: fmt_min(CFG.get('ns_end', 7 * 60)), setter('ns_end', 'End time')),
                                   ('Only when charging', lambda: 'Yes' if CFG.get('ns_need_charge', True) else 'No', lambda: (CFG.__setitem__('ns_need_charge', not CFG.get('ns_need_charge', True)), save_cfg())[1]),
                                   ('Open now', None, lambda: ('open', NightClock())),
                                   ('How it works', None, lambda: ('open', TextBox('Nightstand', 'At set times: the clock appears when the phone would lock or sleep inside the From-Until window (and the screen is off). Only when charging = leave it off the charger and it stays dark.\n\nOn lock & charging: any time the phone locks or sleeps while plugged in, the clock shows instead.\n\nEND exits; UP/DOWN brightness, LEFT/RIGHT style, 1 orientation, * colour.')))])

NS_STYLES = ['Flip', 'Analog', 'Night sky', 'Matrix', 'Breathe', 'LED']
NS_LEVELS = [3, 8, 18, 40, 90]
NS_COLORS = ['Accent', 'Red', 'Amber', 'White']
SEG = {'0': 'abcdef', '1': 'bc', '2': 'abdeg', '3': 'abcdg', '4': 'bcfg', '5': 'acdfg', '6': 'acdefg', '7': 'abc', '8': 'abcdefg', '9': 'abcdfg'}
class NightClock(App):
    """Dim bedside clock: six animated styles, portrait or landscape (either way up), alarm aware.
    LEFT/RIGHT style - UP/DOWN brightness - 1 orientation - * colour - OK show/hide details - END/RSK exit"""
    title = ''; left = ''; right = ''
    def __init__(self):
        self.style = CFG.get('ns_style', 0) % len(NS_STYLES); self.orient = CFG.get('ns_orient', 0) % 3; self.color = CFG.get('ns_color', 0) % 4; self.level = CFG.get('ns_level', 1) % 5; self.info = True
        rnd = random.Random(7); self.stars = [(rnd.random(), rnd.random(), rnd.random() * 6.3, 0.5 + rnd.random() * 1.5) for _ in range(70)]
        self.cols = None; self.prev = None; self.flip_t0 = 0; self.meteor = None; self.msg = ''; self.msg_t = 0; self.t0 = time.time()
        wr(LCD, NS_LEVELS[self.level]); wr(KBL, 0)
    # ---- timing: fast redraws only while something moves
    @property
    def tick(self):
        if self.style in (1, 2, 3, 4): return 0.22
        if self.style == 0 and time.time() - self.flip_t0 < 0.7: return 0.1
        return 1.0
    def pal(self):
        c = [ACC, (255, 54, 40), (255, 150, 40), (225, 225, 225)][self.color]; return c
    def dim(self, f, base=None): c = base or self.pal(); return tuple(int(v * f) for v in c)
    def times(self):
        n = datetime.datetime.now(); h = n.hour if CFG['clock24'] else (n.hour % 12 or 12)
        return n, '%02d' % h if CFG['clock24'] else str(h).rjust(2), '%02d' % n.minute
    # ---- frame
    def render_full(self):
        land = self.orient != 0; Wc, Hc = (320, 240) if land else (240, 320)
        img = Image.new('RGB', (Wc, Hc), (0, 0, 0)); d = ImageDraw.Draw(img)
        getattr(self, 'draw_' + ['flip', 'analog', 'sky', 'matrix', 'breathe', 'led'][self.style])(d, img, Wc, Hc)
        if self.info: self.draw_info(d, Wc, Hc)
        if self.msg and time.time() - self.msg_t < 2: ctext(d, Wc / 2, 6, self.msg, font(12, True), self.dim(0.8))
        return img.rotate(90 if self.orient == 1 else -90, expand=True) if land else img
    def draw_info(self, d, Wc, Hc):
        n = datetime.datetime.now(); parts = [n.strftime('%a %d %b')]
        a = CFG.get('alarm', {})
        if a.get('on'): parts.append('alarm %02d:%02d' % (a['h'], a['m']))
        cap, st, _ = battery(); parts.append(cap + '%' + (' +' if st.lower().startswith('charg') else ''))
        ctext(d, Wc / 2, Hc - 22, '   '.join(parts), font(12), self.dim(0.5))
    # ---- 1. flip clock
    def card(self, ch, w, h, f):
        im = Image.new('RGB', (w, h), (0, 0, 0)); dd = ImageDraw.Draw(im); r = max(4, rr() * 2)
        dd.rounded_rectangle((0, 0, w - 1, h - 1), r, fill=(26, 26, 30))
        ft = bigfont(int(h * 0.78)); bb = dd.textbbox((0, 0), ch, font=ft); dd.text(((w - (bb[2] - bb[0])) / 2 - bb[0], (h - (bb[3] - bb[1])) / 2 - bb[1] - h * 0.02), ch, font=ft, fill=self.dim(f))
        dd.line((0, h // 2, w, h // 2), fill=(0, 0, 0), width=3); return im
    def draw_flip(self, d, img, Wc, Hc):
        n, hh, mm = self.times(); cur = hh + mm; land = Wc > Hc
        if self.prev is None: self.prev = cur
        if cur != self.prev and time.time() - self.flip_t0 > 0.7: self.old = self.prev; self.flip_t0 = time.time(); self.prev = cur
        cw, ch = (66, 130) if land else (46, 100); gap = 6; pair = 14
        total = 4 * cw + 2 * gap + pair; x0 = (Wc - total) // 2; y0 = (Hc - ch) // 2 - 6
        p = (time.time() - self.flip_t0) / 0.6 if time.time() - self.flip_t0 < 0.6 else None
        for i in range(4):
            x = x0 + i * (cw + gap) + (pair - gap if i >= 2 else 0); new = cur[i]; old = getattr(self, 'old', cur)[i]
            if p is None or old == new: img.paste(self.card(new, cw, ch, 1.0), (x, y0)); continue
            newc, oldc = self.card(new, cw, ch, 1.0), self.card(old, cw, ch, 1.0); half = ch // 2
            img.paste(newc.crop((0, 0, cw, half)), (x, y0)); img.paste(oldc.crop((0, half, cw, ch)), (x, y0 + half))
            if p < 0.5: hgt = max(1, int(half * (1 - p * 2))); img.paste(oldc.crop((0, 0, cw, half)).resize((cw, hgt)), (x, y0 + half - hgt))
            else: hgt = max(1, int(half * (p * 2 - 1))); img.paste(newc.crop((0, half, cw, ch)).resize((cw, hgt)), (x, y0 + half))
        cx = x0 + 2 * cw + gap + pair // 2 + (gap // 2) - 2
        for dy in (-14, 14): d.ellipse((cx - 3, y0 + ch // 2 + dy - 3, cx + 3, y0 + ch // 2 + dy + 3), fill=self.dim(0.9))
    # ---- 2. analog with a smooth sweep
    def draw_analog(self, d, img, Wc, Hc):
        n = datetime.datetime.now(); sec = n.second + n.microsecond / 1e6; cx, cy = Wc / 2, Hc / 2 - 8; R = min(Wc, Hc) / 2 - 22
        d.ellipse((cx - R - 6, cy - R - 6, cx + R + 6, cy + R + 6), outline=self.dim(0.25), width=2)
        for i in range(60):
            a = math.radians(i * 6); r0 = R - (12 if i % 5 == 0 else 5); d.line((cx + math.sin(a) * r0, cy - math.cos(a) * r0, cx + math.sin(a) * R, cy - math.cos(a) * R), fill=self.dim(0.9 if i % 5 == 0 else 0.3), width=3 if i % 5 == 0 else 1)
        def hand(frac, length, width, f): a = math.radians(frac * 360); d.line((cx - math.sin(a) * 12, cy + math.cos(a) * 12, cx + math.sin(a) * length, cy - math.cos(a) * length), fill=self.dim(f), width=width)
        hand((n.hour % 12 + n.minute / 60) / 12, R * 0.5, 6, 0.95); hand((n.minute + sec / 60) / 60, R * 0.78, 4, 0.95); hand(sec / 60, R * 0.88, 1, 0.7)
        d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=self.dim(1.0))
    # ---- 3. night sky
    def draw_sky(self, d, img, Wc, Hc):
        t = time.time() - self.t0
        for x, y, ph, sp in self.stars:
            b = 0.25 + 0.75 * (0.5 + 0.5 * math.sin(t * sp + ph)); v = int(200 * b); px, py = int(x * Wc), int(y * Hc)
            d.point((px, py), fill=(v, v, v + 20));
            if b > 0.9: d.point((px + 1, py), fill=(v // 2, v // 2, v // 2)); d.point((px, py + 1), fill=(v // 2, v // 2, v // 2))
        mx, my = (Wc - 44, 46) if Wc > Hc else (Wc - 46, 52); d.ellipse((mx - 20, my - 20, mx + 20, my + 20), fill=(236, 232, 210)); d.ellipse((mx - 9, my - 24, mx + 31, my + 16), fill=(0, 0, 0))
        if self.meteor is None and random.random() < 0.015: self.meteor = (random.random() * Wc * 0.6, random.random() * Hc * 0.3, t)
        if self.meteor:
            x, y, t1 = self.meteor; q = (t - t1) / 0.6
            if q > 1: self.meteor = None
            else: d.line((x + q * 90 - 30, y + q * 45 - 15, x + q * 90, y + q * 45), fill=(220, 220, 255), width=1)
        n, hh, mm = self.times(); d.text((Wc / 2, Hc / 2 + 6), hh.strip() + ':' + mm, font=bigfont(64 if Wc < Hc else 72), fill=self.dim(0.85), anchor='mm')
    # ---- 4. matrix rain
    def draw_matrix(self, d, img, Wc, Hc):
        step = 15; n_cols = Wc // step
        if self.cols is None or len(self.cols) != n_cols: self.cols = [[random.random() * Hc, 6 + random.random() * 12, random.randint(4, 11)] for _ in range(n_cols)]
        f = font(10, mono=True); glyphs = '0123456789ABCDEF'
        for i, (y, sp, ln) in enumerate(self.cols):
            for j in range(ln):
                yy = y - j * 11
                if 0 <= yy < Hc: d.text((i * step + 2, yy), random.choice(glyphs), font=f, fill=self.dim(1.0 if j == 0 else max(0.1, 0.6 - j * 0.09), (120, 255, 150) if self.color == 0 else None))
            self.cols[i][0] = y + sp
            if y - ln * 11 > Hc: self.cols[i] = [-random.random() * 80, 6 + random.random() * 12, random.randint(4, 11)]
        n, hh, mm = self.times(); w, h = (150, 86) if Wc < Hc else (190, 90); bx, by = (Wc - w) // 2, (Hc - h) // 2 - 6
        d.rounded_rectangle((bx, by, bx + w, by + h), max(4, rr() * 2), fill=(0, 0, 0), outline=self.dim(0.5, (120, 255, 150) if self.color == 0 else None), width=2)
        d.text((Wc / 2, by + h / 2), hh.strip() + ':' + mm, font=bigfont(54), fill=self.dim(1.0, (170, 255, 190) if self.color == 0 else None), anchor='mm')
    # ---- 5. breathing glow
    def draw_breathe(self, d, img, Wc, Hc):
        n, hh, mm = self.times(); t = time.time() - self.t0; f = 0.55 + 0.45 * math.sin(t * 2 * math.pi / 7)
        txt = hh.strip() + ':' + mm; size = 78 if Wc < Hc else 104; layer = Image.new('L', (Wc, Hc), 0); ImageDraw.Draw(layer).text((Wc / 2, Hc / 2 - 4), txt, font=bigfont(size), fill=255, anchor='mm')
        glow = layer.filter(ImageFilter.GaussianBlur(9)); col = self.pal()
        base = Image.new('RGB', (Wc, Hc), tuple(int(v * 0.5 * f) for v in col)); img.paste(base, (0, 0), glow)
        d.text((Wc / 2, Hc / 2 - 4), txt, font=bigfont(size), fill=self.dim(0.35 + 0.65 * f), anchor='mm')
    # ---- 6. LED segments
    def draw_led(self, d, img, Wc, Hc):
        n, hh, mm = self.times(); land = Wc > Hc; dw, dh = (54, 104) if land else (40, 80); th = 7 if land else 6; gap = 8; colon = 22
        total = 4 * dw + 2 * gap + colon + gap; x0 = (Wc - total) // 2; y0 = (Hc - dh) // 2 - 6
        def digit(x, ch):
            h2 = dh / 2; segs = {'a': (x + th, y0, x + dw - th, y0 + th), 'g': (x + th, y0 + h2 - th / 2, x + dw - th, y0 + h2 + th / 2), 'd': (x + th, y0 + dh - th, x + dw - th, y0 + dh),
                                  'f': (x, y0 + th, x + th, y0 + h2 - 2), 'b': (x + dw - th, y0 + th, x + dw, y0 + h2 - 2), 'e': (x, y0 + h2 + 2, x + th, y0 + dh - th), 'c': (x + dw - th, y0 + h2 + 2, x + dw, y0 + dh - th)}
            on = SEG.get(ch, '')
            for s_, box in segs.items(): d.rounded_rectangle(box, 2, fill=self.dim(1.0) if s_ in on else self.dim(0.07))
        s = hh + mm; xx = x0
        for i, ch in enumerate(s):
            digit(xx, ch if ch != ' ' else ''); xx += dw + gap
            if i == 1:
                lit = int(time.time()) % 2 == 0
                for dy in (-14, 14): d.rectangle((xx + colon / 2 - 4, y0 + dh / 2 + dy - 4, xx + colon / 2 + 4, y0 + dh / 2 + dy + 4), fill=self.dim(1.0 if lit else 0.1))
                xx += colon + gap
    # ---- keys
    def say(self, m): self.msg = m; self.msg_t = time.time()
    def save(self): CFG.update(ns_style=self.style, ns_orient=self.orient, ns_color=self.color, ns_level=self.level); save_cfg()
    def exit(self): wr(LCD, CFG['brightness']); wr(KBL, 12); NS_DISMISSED[0] = True; return 'back'
    def key(self, k):
        if k == 'LEFT': self.style = (self.style - 1) % len(NS_STYLES); self.cols = None; self.say(NS_STYLES[self.style]); self.save()
        elif k == 'RIGHT': self.style = (self.style + 1) % len(NS_STYLES); self.cols = None; self.say(NS_STYLES[self.style]); self.save()
        elif k == 'UP': self.level = min(4, self.level + 1); wr(LCD, NS_LEVELS[self.level]); self.say('Brightness %d/5' % (self.level + 1)); self.save()
        elif k == 'DOWN': self.level = max(0, self.level - 1); wr(LCD, NS_LEVELS[self.level]); self.say('Brightness %d/5' % (self.level + 1)); self.save()
        elif k == '1': self.orient = (self.orient + 1) % 3; self.say(['Portrait', 'Landscape (left)', 'Landscape (right)'][self.orient]); self.save()
        elif k == '*': self.color = (self.color + 1) % 4; self.say(NS_COLORS[self.color]); self.save()
        elif k == 'OK': self.info = not self.info
        elif k in ('RSK', 'END', 'LSK'): return self.exit()

# --------------------------------------------------------------------------------------------- X11 apps, Applications launcher, Software center
try: import jgmx
except Exception: jgmx = None
_X = {'srv': None, 'inp': None}
X_PACKAGES = ['xvfb', 'matchbox-window-manager', 'x11-xserver-utils', 'python3-xlib', 'wmctrl', 'xterm', 'x11-apps', 'dbus-x11']
def x_ready(): return bool(jgmx) and bool(shutil.which('Xvfb')) and bool(shutil.which('matchbox-window-manager'))
def xsrv():
    if _X['srv'] is None: _X['srv'] = jgmx.XServer()
    return _X['srv']
def xinp():
    if _X['inp'] is None: _X['inp'] = jgmx.XInput()
    return _X['inp']
def mem_available_mb():
    for l in rd('/proc/meminfo').splitlines():
        if l.startswith('MemAvailable'): return int(l.split()[1]) // 1024
    return 0
def x_launch(app):
    """make sure the X screen is up, start the app on it, return the viewer"""
    v = XView(); v.starting = True
    def work():
        try:
            if not x_ready(): v.msg = 'X server not installed (Software > Install X)'; v.starting = False; return
            if mem_available_mb() < 60: v.msg = 'Low memory (%d MB free)' % mem_available_mb()
            xsrv().start()
            if app: jgmx.launch(app)
        except Exception as e: v.msg = str(e)[:40]
        v.starting = False
    threading.Thread(target=work, daemon=True).start(); return v

class XView(App):
    """The panel as a window onto the X screen. Pointer mode: arrows move, OK click, CALL double-click, 5 click, 0 right-click, 2/8 scroll, 1 zoom, # keyboard mode, * Escape.
    Keyboard mode: digits type letters (multitap), OK Enter, RSK Backspace, arrows = arrow keys, END back to pointer mode. LSK = menu. END (pointer mode) = back to the launcher."""
    title = ''; left = ''; right = ''
    def __init__(self):
        self.zoom = CFG.get('x_zoom', 'fit'); self.px, self.py = 240, 320; self.kb = False; self.mt = MultiTap(); self.last_move = 0.0; self.step = 8; self.msg = ''; self.msg_t = 0.0; self.starting = False; self.err = ''
    @property
    def tick(self): return 0.2
    def say(self, m): self.msg = m; self.msg_t = time.time()
    def render_full(self):
        img = Image.new('RGB', (W, H), (16, 22, 28)); d = ImageDraw.Draw(img)
        fr = xsrv().grab() if jgmx and _X['srv'] and xsrv().running() else None
        if fr is None:
            ctext(d, W / 2, 130, 'Starting X...' if self.starting else (self.msg or 'X is not running'), font(14, True), (200, 210, 215)); ctext(d, W / 2, 170, 'END = back', font(11), (120, 135, 140)); return img
        sw, sh_ = fr.size
        if self.zoom == 'fit': img.paste(fr.resize((W, H), Image.BILINEAR), (0, 0)); ox = oy = 0; sx = W / sw; sy = H / sh_
        else:
            ox = max(0, min(sw - W, self.px - W // 2)); oy = max(0, min(sh_ - H, self.py - H // 2)); img.paste(fr.crop((ox, oy, ox + W, oy + H)), (0, 0)); sx = sy = 1.0
        cx, cy = (self.px - ox) * sx, (self.py - oy) * sy
        d.line((cx - 7, cy, cx + 7, cy), fill=(255, 255, 255), width=1); d.line((cx, cy - 7, cx, cy + 7), fill=(255, 255, 255), width=1); d.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), outline=(255, 60, 60))
        tag = ('KB ' if self.kb else '') + ('1:1' if self.zoom != 'fit' else '')
        if tag: d.rectangle((0, 0, 8 + 7 * len(tag), 13), fill=(0, 0, 0)); d.text((3, 1), tag, font=font(10, True), fill=(120, 255, 160))
        if self.msg and time.time() - self.msg_t < 2: ctext(d, W / 2, H - 20, self.msg, font(11, True), (255, 255, 255))
        return img
    def move(self, dx, dy):
        now = time.time(); self.step = min(48, self.step + 6) if now - self.last_move < 0.3 else 8; self.last_move = now
        self.px = max(0, min(479, self.px + dx * self.step)); self.py = max(0, min(639, self.py + dy * self.step))
        try: xinp().move(self.px, self.py)
        except Exception as e: self.say('X input: ' + str(e)[:30])
    def sendkey(self, name, **kw):
        try: xinp().key(name, **kw)
        except Exception as e: self.say('X input: ' + str(e)[:30])
    def options(self):
        def keys():
            ks = [('Enter', 'Return', {}), ('Escape', 'Escape', {}), ('Tab', 'Tab', {}), ('Backspace', 'BackSpace', {}), ('Delete', 'Delete', {}), ('Ctrl+C', 'c', {'ctrl': True}), ('Ctrl+V', 'v', {'ctrl': True}),
                  ('Ctrl+A', 'a', {'ctrl': True}), ('Ctrl+S', 's', {'ctrl': True}), ('Ctrl+Z', 'z', {'ctrl': True}), ('Alt+F4', 'F4', {'alt': True}), ('F5', 'F5', {})]
            return ('popopen', Popup('Keys', [(n, (lambda k=k, kw=kw: (self.sendkey(k, **kw), 'popback')[1])) for n, k, kw in ks]))
        def windows():
            ws = xinp().windows() if jgmx else []
            return ('popopen', Popup('Windows', [(t[:26] or w, (lambda w=w: (xinp().focus(w), 'popback')[1])) for w, t in ws] or [('(no windows)', lambda: 'popback')]))
        def zoom(): self.zoom = 'fit' if self.zoom != 'fit' else '1:1'; CFG['x_zoom'] = self.zoom; save_cfg(); return 'popback'
        def mode(): self.kb = not self.kb; return 'popback'
        def quitx(): xsrv().stop(); xinp().close(); _X['inp'] = None; return 'back'
        return [('Type text...', lambda: ('popopen', Editor('Type', '', on_done=lambda t: threading.Thread(target=lambda: xinp().type_text(t), daemon=True).start()))), ('Keys...', keys), ('Windows...', windows),
                ('Zoom: ' + ('fit' if self.zoom == 'fit' else '1:1 (pan)'), zoom), ('Mode: ' + ('keyboard' if self.kb else 'pointer'), mode),
                ('Close window', lambda: (xinp().close_active(), 'popback')[1]), ('Quit X (free memory)', quitx)]
    def key(self, k):
        if k == 'END':
            if self.kb: self.kb = False; self.say('Pointer mode'); return None
            return 'back'
        if self.kb:
            if k in ('UP', 'DOWN', 'LEFT', 'RIGHT'): self.sendkey({'UP': 'Up', 'DOWN': 'Down', 'LEFT': 'Left', 'RIGHT': 'Right'}[k])
            elif k == 'OK': self.sendkey('Return')
            elif k == 'RSK': self.sendkey('BackSpace')
            elif k in T9:
                now = time.time(); ch = T9[k]
                if self.mt.last == k and now - self.mt.t < 0.9: self.sendkey('BackSpace'); self.mt.i = (self.mt.i + 1) % len(ch)
                else: self.mt.i = 0
                self.mt.last = k; self.mt.t = now; self.sendkey(ch[self.mt.i])
            return None
        if k == 'UP': self.move(0, -1)
        elif k == 'DOWN': self.move(0, 1)
        elif k == 'LEFT': self.move(-1, 0)
        elif k == 'RIGHT': self.move(1, 0)
        elif k in ('OK', '5'):
            try: xinp().click(1)
            except Exception as e: self.say('X input: ' + str(e)[:30])
        elif k == 'CALL':
            try: xinp().click(1, double=True)
            except Exception: pass
        elif k == '0':
            try: xinp().click(3)
            except Exception: pass
        elif k == '2':
            try: xinp().wheel(True)
            except Exception: pass
        elif k == '8':
            try: xinp().wheel(False)
            except Exception: pass
        elif k == '1': self.zoom = 'fit' if self.zoom != 'fit' else '1:1'; CFG['x_zoom'] = self.zoom; save_cfg(); self.say('Zoom: ' + ('fit' if self.zoom == 'fit' else '1:1'))
        elif k == '#': self.kb = True; self.say('Keyboard mode')
        elif k == '*': self.sendkey('Escape')
        elif k == 'RSK': return 'back'
        return None

class IconList(PickList):
    """PickList whose rows may carry an icon: rows = [(title, sub, icon or None)]"""
    def draw(self, d, img):
        rows = self.rows() if callable(self.rows) else self.rows
        self.sel = min(self.sel, max(0, len(rows) - 1)); top = max(0, self.sel - 5)
        for i, row in enumerate(rows[top:top + 6]):
            t, sub = row[0], row[1]; ic = row[2] if len(row) > 2 else None; y = 28 + i * 40; sel = top + i == self.sel
            if sel: rrect(d, (3, y - 1, W - 3, y + 37), fill=PANEL)
            x0 = 8
            if ic is not None: img.paste(ic, (6, y + 1), ic); x0 = 52
            d.text((x0, y + 2), fit(d, t, font(13, True), W - x0 - 6), font=font(13, True), fill=FG)
            if sub: d.text((x0, y + 20), fit(d, sub, font(10), W - x0 - 6), font=font(10), fill=DIM)
        if not rows: ctext(d, W / 2, 120, 'Nothing here', font(14), DIM)
        st = self.status() if callable(self.status) else self.status
        if st: d.rectangle((0, H - 44, W, H - 27), fill=BG); d.text((6, H - 42), fit(d, st, font(11), W - 12), font=font(11), fill=ACC)

class ApplicationsApp(IconList):
    """Installed graphical apps (the .desktop files of installed packages), grouped by category."""
    def __init__(self):
        self.group = 'All'; self.apps_ = jgmx.apps() if jgmx else []; self.msg = ''
        super().__init__('Applications', self.rows_, self.pick_, self.opts_, status=lambda: self.msg or ('X server not installed - Software > Install X' if not x_ready() else ''))
    def shown(self): return [a for a in self.apps_ if self.group == 'All' or jgmx.group_of(a['cats']) == self.group]
    def rows_(self): return [(a['name'], a['comment'] or ', '.join(a['cats'][:3]), jgmx.icon(a, 38)) for a in self.shown()]
    def pick_(self, i): return ('open', x_launch(self.shown()[i]))
    def opts_(self):
        groups = ['All'] + sorted({jgmx.group_of(a['cats']) for a in self.apps_})
        o = [('Category: ' + self.group, lambda: (setattr(self, 'group', groups[(groups.index(self.group) + 1) % len(groups)]), setattr(self, 'sel', 0), 'popback')[2]),
             ('Pin to home / menu', self.pin), ('Open X screen', lambda: ('popopen', x_launch(None))), ('Rescan apps', lambda: (setattr(self, 'apps_', jgmx.apps()), 'popback')[1])]
        return o
    def pin(self):
        sh_ = self.shown()
        if not sh_: return 'popback'
        a = sh_[self.sel]; pins = CFG.setdefault('x_pinned', [])
        if a['id'] not in pins: pins.append(a['id']); save_cfg(); register_x_apps()
        self.msg = 'Pinned ' + a['name']; return 'popback'
def register_x_apps():
    """pinned graphical apps become entries of the app list (and can be put on the home screen)"""
    if not jgmx: return
    by = {a['id']: a for a in jgmx.apps()}
    for aid in CFG.get('x_pinned', []):
        a = by.get(aid)
        if not a: continue
        meta = ('x_' + aid, a['name'], '', (90, 100, 120), (lambda a=a: x_launch(a)))
        for i, m in enumerate(APP_META):
            if m[0] == 'x_' + aid: APP_META[i] = meta; break
        else: APP_META.append(meta)
        APP_BY_ID[meta[0]] = meta
    ALL_APPS_META[:] = [(a[0], a[1]) for a in APP_META]

# ---- software center (catalog from Debian's AppStream data, installs through apt)
FEATURED = ['xterm', 'lxterminal', 'galculator', 'mousepad', 'pcmanfm', 'gpicview', 'geany', 'dillo', 'netsurf-gtk', 'xpdf', 'atril', 'sylpheed', 'hexchat', 'audacious', 'abiword', 'gnumeric',
            'gnome-mines', 'gnome-sudoku', 'gnome-2048', 'quadrapassel', 'gweled', 'xboard', 'gnugo', 'frozen-bubble', 'pingus', 'fceux', 'dosbox', 'osmo', 'orage', 'lxtask', 'mtpaint', 'fbreader']
_cat = {'t': 0, 'apps': None}
def catalog():
    if _cat['apps'] is None:
        for p in (DATA + '/catalog.json', '/opt/jgm/catalog.json'):
            try: _cat['apps'] = json.load(open(p))['apps']; break
            except Exception: continue
        else: _cat['apps'] = []
    return _cat['apps']
_inst = {'t': 0, 's': set()}
def installed_pkgs():
    if time.time() - _inst['t'] > 20:
        _inst['s'] = set(sh("dpkg-query -W -f='${Package} ${Status}\\n' 2>/dev/null | awk '$NF==\"installed\"{print $1}'", 30).split()); _inst['t'] = time.time()
    return _inst['s']
def fmt_kb(kb): return '%.1f MB' % (kb / 1024) if kb >= 1024 else '%d KB' % kb
def free_root_mb(): return int(sh("df -m / | awk 'NR==2{print $4}'") or 0)
APT_ENV = 'DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none '
class AptJob:
    """one apt command at a time; .status is the latest line, .done/.ok when finished"""
    cur = None
    def __init__(self, cmd, label, after=None):
        self.cmd = cmd; self.label = label; self.status = label + '...'; self.done = False; self.ok = False; self.after = after; AptJob.cur = self
        threading.Thread(target=self.run, daemon=True).start()
    def run(self):
        try:
            p = subprocess.Popen(['sh', '-c', APT_ENV + self.cmd + ' 2>&1'], stdout=subprocess.PIPE, text=True, bufsize=1)
            for l in p.stdout:
                l = l.strip()
                m = re.match(r'(Get:\d+|Unpacking|Setting up|Preparing|Selecting|Removing|Reading|Building|Processing)\s*(.*)', l)
                if m: self.status = (m.group(1).split(':')[0] + ' ' + m.group(2))[:48]
            p.wait(); self.ok = p.returncode == 0
        except Exception as e: self.status = str(e)[:40]
        self.status = ('Done: ' if self.ok else 'Failed: ') + self.label; self.done = True; _inst['t'] = 0
        if self.after: self.after(self.ok)
def lists_fresh():
    try: return time.time() - os.path.getmtime('/var/lib/apt/lists/lock') < 86400 * 2 or any(f.endswith('Packages') or f.endswith('Packages.xz') or f.endswith('Packages.lz4') for f in os.listdir('/var/lib/apt/lists'))
    except Exception: return False
def apt_update_cmd(): return 'sh /opt/jgm/apt-setup.sh; apt-get update -o Acquire::Languages=none'

class SoftDetail(App):
    title = 'Software'; left = 'Actions'; tick = 1
    def __init__(self, a):
        self.a = a; self.job = None; self.plan = None; self.msg = ''; self.title = a['n'][:20]
    def installed(self): return self.a['p'] in installed_pkgs()
    def desktop_app(self):
        if not jgmx: return None
        files = sh('dpkg -L %s 2>/dev/null | grep "\\.desktop$"' % self.a['p']).split()
        by = {x['file']: x for x in jgmx.apps()}
        return next((by[f] for f in files if f in by), None)
    def options(self):
        if self.job and not self.job.done: return [('Working...', lambda: 'popback')]
        if self.installed():
            o = [('Remove', self.remove)]
            if self.desktop_app(): o.insert(0, ('Open', lambda: ('popopen', x_launch(self.desktop_app()))))
            return o
        return [('Install', self.plan_install)]
    def plan_install(self):
        def work():
            self.msg = 'Checking...'
            out = sh('apt-get -s install --no-install-recommends %s 2>&1' % self.a['p'], 120)
            m = re.search(r'After this operation, ([\d.,]+) (kB|MB|GB) of additional disk space', out); inst = len(re.findall(r'^Inst ', out, re.M))
            if 'Unable to locate' in out or ('E:' in out and not m): self.msg = 'Not available (run Update lists)'; return
            need = float(m.group(1).replace(',', '')) * {'kB': 1 / 1024, 'MB': 1, 'GB': 1024}[m.group(2)] if m else 0; free = free_root_mb()
            self.plan = (need, inst, free); self.msg = ''
            if need > free - 20: self.msg = 'Not enough space: needs %.0f MB, %d MB free' % (need, free); self.plan = None
        threading.Thread(target=work, daemon=True).start(); return 'popback'
    def go_install(self):
        steps = 'sh /opt/jgm/apt-setup.sh; '
        if not lists_fresh(): steps = apt_update_cmd() + ' && '
        self.job = AptJob(steps + 'apt-get install -y --no-install-recommends ' + self.a['p'], 'Install ' + self.a['n'][:20], lambda ok: notify('Installed' if ok else 'Install failed', self.a['n'])); self.plan = None
    def remove(self):
        self.job = AptJob('apt-get remove -y --autoremove ' + self.a['p'], 'Remove ' + self.a['n'][:20], lambda ok: notify('Removed' if ok else 'Remove failed', self.a['n'])); return 'popback'
    def draw(self, d, img):
        a = self.a; ic = jgmx.icon({'id': a['p'], 'name': a['n'], 'icon': a.get('i', '')}, 44) if jgmx else None
        if ic is not None: img.paste(ic, (8, 30), ic)
        d.text((60, 32), fit(d, a['n'], font(15, True), W - 66), font=font(15, True), fill=FG); d.text((60, 54), a['p'][:30], font=font(10), fill=DIM)
        y = 86
        for l in wrap(d, a['s'] or '(no description)', font(12), W - 16)[:5]: d.text((8, y), l, font=font(12), fill=FG); y += 16
        d.text((8, 178), 'Category: ' + ', '.join(a['c'][:3]), font=font(11), fill=DIM); d.text((8, 194), 'Size: ' + fmt_kb(a['z']) + '   (free space ' + str(free_root_mb()) + ' MB)', font=font(11), fill=DIM)
        st = 'Installed' if self.installed() else 'Not installed'; ctext(d, W / 2, 222, st, font(14, True), ACC if self.installed() else FG)
        if self.job: ctext(d, W / 2, 250, self.job.status, font(11), WARN if self.job.done and not self.job.ok else ACC)
        elif self.msg: ctext(d, W / 2, 250, self.msg, font(11), WARN)
        if self.plan and not self.job:
            need, n, free = self.plan; ctext(d, W / 2, 246, 'Install %d package%s, %.0f MB?' % (n, '' if n == 1 else 's', need), font(12, True), WARN); ctext(d, W / 2, 262, 'OK = install   RSK = cancel', font(11), DIM)
    def key(self, k):
        if self.plan and not self.job:
            if k == 'OK': self.go_install(); return None
            if k in ('RSK', 'END'): self.plan = None; return None
        if k in ('RSK', 'END'): return 'back'
        if k == 'OK': return ('open', Popup(self.a['n'][:20], self.options()))

def soft_rows(apps):
    ins = installed_pkgs()
    return [(a['n'] + ('  [installed]' if a['p'] in ins else ''), (a['s'] or '') + ('' if not a['z'] else '  (' + fmt_kb(a['z']) + ')')) for a in apps]
class SoftList(PickList):
    def __init__(self, title, apps, searchable=True):
        self.apps = apps
        super().__init__(title, lambda: soft_rows(self.apps), lambda i: ('open', SoftDetail(self.apps[i])), (lambda: [('Search in this list', lambda: ('popopen', Editor('Search', '', on_done=self.search)))]) if searchable else None,
                         status=lambda: '%d apps' % len(self.apps))
        self.all = list(apps)
    def search(self, q):
        q = q.lower().strip(); self.apps = [a for a in self.all if q in a['n'].lower() or q in a['s'].lower() or q in a['p'].lower()] if q else list(self.all); self.sel = 0
def soft_by_group(name):
    return [a for a in catalog() if jgmx.group_of(a['c']) == name] if jgmx else []
class SoftHome(PickList):
    """Software: Featured / categories / search / installed / updates, like a desktop app store"""
    def __init__(self):
        self.msg = ''
        super().__init__('Software', self.rows_, self.pick_, self.opts_, status=lambda: (AptJob.cur.status if AptJob.cur and not AptJob.cur.done else self.msg) or ('%d apps in the catalog' % len(catalog())))
    def groups(self): return [g for g, _ in jgmx.CATEGORY_GROUPS] if jgmx else []
    def rows_(self):
        r = [('Featured', 'small apps that suit this phone'), ('Search', 'by name, summary or package')]
        for g in self.groups(): r.append((g, '%d apps' % len(soft_by_group(g))))
        r += [('Installed apps', 'graphical apps on the phone'), ('Updates', 'upgrade installed packages'), ('Install X (graphical apps)', 'X server, window manager, terminal'), ('jgmDeb apps', 'the jgmDeb Store')]
        return r
    def pick_(self, i):
        g = self.groups(); n = len(g)
        if i == 0:
            by = {a['p']: a for a in catalog()}; return ('open', SoftList('Featured', [by[p] for p in FEATURED if p in by]))
        if i == 1: return ('open', Editor('Search apps', '', on_done=lambda q: setattr(self, 'pending', ('open', SoftList('Search: ' + q, [a for a in catalog() if q.lower() in a['n'].lower() or q.lower() in a['s'].lower() or q.lower() in a['p'].lower()])))))
        if 2 <= i < 2 + n: return ('open', SoftList(g[i - 2], soft_by_group(g[i - 2])))
        j = i - 2 - n
        if j == 0: return ('open', ApplicationsApp())
        if j == 1: return ('open', SoftUpdates())
        if j == 2: return ('open', XInstall())
        if j == 3: return ('open', StoreApp())
    def opts_(self): return [('Update package lists', lambda: (AptJob(apt_update_cmd(), 'Update lists'), 'popback')[1]), ('Update catalog', lambda: (threading.Thread(target=self.fetch_catalog, daemon=True).start(), 'popback')[1])]
    def fetch_catalog(self):
        try:
            self.msg = 'Downloading catalog...'; raw = http_get(store_url() + 'catalog.json', 30, True); json.loads(raw); open(DATA + '/catalog.json', 'wb').write(raw); _cat['apps'] = None; self.msg = 'Catalog updated: %d apps' % len(catalog())
        except Exception as e: self.msg = 'Catalog update failed: ' + str(e)[:30]
    def key(self, k):
        p = getattr(self, 'pending', None)
        if p: self.pending = None; return p
        return super().key(k)
class SoftUpdates(App):
    title = 'Updates'; left = 'Actions'; tick = 1
    def __init__(self): self.n = None; self.msg = 'Checking...'; self.job = None; threading.Thread(target=self.check, daemon=True).start()
    def check(self):
        self.msg = 'Updating lists...'; sh(apt_update_cmd(), 300); out = sh('apt-get -s upgrade 2>&1', 120); self.pk = re.findall(r'^Inst (\S+)', out, re.M); self.n = len(self.pk); self.msg = ''
    def options(self): return [('Upgrade all', self.upgrade)] if self.n and not self.job else []
    def upgrade(self):
        self.job = AptJob('apt-get upgrade -y --no-install-recommends', 'Upgrade %d packages' % self.n, lambda ok: notify('Upgrade finished' if ok else 'Upgrade failed', '%d packages' % self.n)); return 'popback'
    def draw(self, d, img):
        ctext(d, W / 2, 50, 'Updates', font(22, True), ACC)
        if self.n is None: ctext(d, W / 2, 120, self.msg, font(13), FG); return
        ctext(d, W / 2, 100, '%d upgradable package%s' % (self.n, '' if self.n == 1 else 's'), font(15, True), FG)
        for i, p in enumerate(self.pk[:8]): d.text((14, 130 + i * 15), p[:34], font=font(11), fill=DIM)
        if self.job: ctext(d, W / 2, 270, self.job.status, font(11), ACC)
    def key(self, k):
        if k in ('RSK', 'END'): return 'back'
        if k == 'OK' and self.options(): return ('open', Popup('Updates', self.options()))
class XInstall(App):
    title = 'Install X'; left = 'Actions'; tick = 1
    def __init__(self): self.job = None
    def options(self): return [] if x_ready() or self.job else [('Install', self.install)]
    def install(self):
        steps = '' if lists_fresh() else apt_update_cmd() + ' && '
        self.job = AptJob(steps + 'apt-get install -y --no-install-recommends ' + ' '.join(X_PACKAGES), 'Install X', lambda ok: notify('X installed' if ok else 'X install failed', 'Applications can now run')); return 'popback'
    def draw(self, d, img):
        ctext(d, W / 2, 50, 'X server', font(22, True), ACC)
        ctext(d, W / 2, 100, 'installed' if x_ready() else 'not installed', font(15, True), FG)
        for i, l in enumerate(wrap(d, 'Runs graphical apps on a virtual screen shown on the phone. Needs: ' + ', '.join(X_PACKAGES), font(11), W - 20)[:6]): d.text((10, 140 + i * 15), l, font=font(11), fill=DIM)
        if self.job: ctext(d, W / 2, 260, self.job.status, font(11), ACC)
    def key(self, k):
        if k in ('RSK', 'END'): return 'back'
        if k == 'OK' and self.options(): return ('open', Popup('Install X', self.options()))

APP_META = [  # id, name, glyph, colour, factory
    ('voicemail', 'Voicemail', '☎', (120, 130, 240), lambda: Stub('Voicemail', ['Not available yet', 'needs the modem'])),
    ('phone', 'Phone', '☎', (60, 190, 120), Phone), ('contacts', 'Contacts', '☺', (70, 150, 240), Contacts),
    ('messages', 'Messages', '✉', (240, 170, 60), Messages), ('settings', 'Settings', '⚙', (150, 160, 170), Settings),
    ('clock', 'Clock', '⏰', (230, 110, 100), Clock), ('calc', 'Calculator', '+−', (170, 120, 230), Calc),
    ('browser', 'Browser', '⌁', (60, 200, 220), Browser), ('bluetooth', 'Bluetooth', '', (70, 120, 230), BluetoothApp),
    ('notes', 'Notes', '✎', (240, 210, 80), Notes), ('files', 'Files', '⌂', (180, 200, 120), Files),
    ('terminal', 'Terminal', '>_', (80, 90, 100), Terminal), ('calendar', 'Calendar', '☷', (240, 110, 150), Calendar),
    ('torch', 'Flashlight', '☀', (250, 220, 100), Torch), ('snake', 'Snake', '●', (90, 210, 90), Snake),
    ('camera', 'Camera', '◉', (200, 90, 90), lambda: Stub('Camera', ['Camera not supported yet', 'needs the vendor camera stack'])),
    ('music', 'Music', '♫', (150, 110, 220), MusicApp), ('radio', 'Radio', '', (110, 200, 190), Radio), ('news', 'News', '', (240, 120, 100), FeedList), ('weather', 'Weather', '', (90, 170, 245), Weather),
    ('nightstand', 'Nightstand', '', (60, 50, 130), NightClock), ('applications', 'Applications', '', (90, 120, 200), ApplicationsApp), ('software', 'Software', '', (230, 120, 60), SoftHome), ('store', 'jgmDeb Store', '', (70, 140, 220), StoreApp), ('ytdl', 'YT download', '', (240, 80, 80), YtDl), ('notifs', 'Notifications', '', (255, 190, 80), NotifApp), ('hotspot', 'Hotspot', '', (120, 210, 150), Hotspot),
    ('wifi', 'WiFi', '≈', (80, 170, 240), WifiApp),
    ('monitor', 'Monitor', '', (60, 80, 100), lambda: Monitor()), ('battery', 'Battery', '', (110, 224, 140), lambda: BatteryApp()),
    ('alarm', 'Alarm', '', (255, 150, 120), lambda: AlarmApp())]
ALL_APPS_META = [(a[0], a[1]) for a in APP_META]
APP_BY_ID = {a[0]: a for a in APP_META}
load_installed_apps()
register_x_apps()

class Grid(App):
    """Home screen (9 configurable keys) or all-apps view."""
    def __init__(self, ids, home=True): self.ids = ids; self.sel = 0; self.home = home; self.left = 'Menu' if home else ''; self.right = 'Contacts' if home else 'Back'
    def draw(self, d, img):
        page = self.sel // 9 * 9; shown = self.ids[page:page + 9]
        for i, aid in enumerate(shown):
            a = APP_BY_ID.get(aid)
            if not a: continue
            cx, cy = 40 + (i % 3) * 80, 62 + (i // 3) * 84
            if page + i == self.sel: d.rounded_rectangle((cx - 36, cy - 30, cx + 36, cy + 48), max(2, rr() + 4), outline=ACC, width=2)
            if icon: ic = icon(a[0], 46, icon_style(), CFG.get('radius', 2)); img.paste(ic, (cx - 23, cy - 26), ic)
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
    if hasattr(app, 'render_full'):
        try: return app.render_full()
        except Exception: traceback.print_exc()
    img = bg_image(); d = ImageDraw.Draw(img)
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
    def cur(): return stack[-1] if (not S['locked'] or isinstance(stack[-1], NightClock)) else S['lock']
    def to_nightstand():
        lock(); screen(True); stack.append(NightClock()); S['last'] = time.time()       # NightClock sets its own dim backlight, so it must come after screen(True)
    last_ns = 0; off_since = None
    screen(True)
    if STATE['slider_closed'] and CFG['slider_lock'] and CFG['setup_done']: lock(); screen(False)
    threading.Thread(target=check_updates_bg, daemon=True).start()
    if jgmvoice: jgmvoice.Watcher(on_incoming_call, on_call_end).start()
    dirty = True; last_tick = 0; last_alarm = 0; last_batt = 0; battery()
    if CFG.get('governor'): BATT.governor(CFG['governor'])
    while True:
        now = time.time()
        if S.get('unseen') and not S['on']: wr('/sys/class/leds/red/brightness', 60 if int(now * 1.5) % 3 == 0 else 0)
        while INCOMING:
            c = INCOMING.pop(0)
            if not isinstance(stack[-1], (Calling, IncomingCall)): screen(True); S['locked'] = False; S['last'] = now; stack.append(IncomingCall(c)); dirty = True
        if isinstance(stack[-1], (Calling, IncomingCall)) and stack[-1].finished and now >= stack[-1].finished_at: stack.pop(); dirty = True
        if isinstance(stack[-1], IncomingCall) and stack[-1].call['id'] in ENDED: ENDED.discard(stack[-1].call['id']); stack.pop(); dirty = True
        if now - last_batt > (5 if S['on'] else 30):
            last_batt = now; BATT.update(); BATT.charge_led(BATT.charging and not S['on'])
            S['unseen'] = len(unseen_notifs())
            for ev, p in BATT.pop_events():
                msg = {'low': 'Battery low (%d%%).\nConnect a charger.', 'critical': 'Battery critical (%d%%)!\nShutting down soon.', 'shutdown': 'Battery empty.\nShutting down in 20 s.'}[ev] % p
                if not S['on']: screen(True)
                notify('Battery ' + ev, msg.replace('\n', ' ')); S['locked'] = False; S['last'] = now; stack.append(TextBox('Battery', msg, 'OK')); dirty = True
            if BATT.shutdown_due(): save_cfg(); BATT.save(now); subprocess.call('sync; poweroff', shell=True)
            if isinstance(stack[-1], (BatteryApp, Monitor, Grid)) or S['locked']: dirty = True
        app = cur()
        if S['on'] and CFG['timeout'] and now - S['last'] > CFG['timeout'] and not isinstance(app, (Terminal, Ringing, NightClock, XView)):
            if ns_wanted() and not (ns_mode() == 1 and NS_DISMISSED[0]): to_nightstand(); dirty = True; continue
            lock(); screen(False)
        if PWR:                                                       # power saver: stages after the screen goes off
            try:
                if not S['on']:
                    if off_since is None: off_since = now
                    PWR.screen_off_tick(now - off_since, CFG.get('ps_mode', 0), bool(BATT and BATT.online))
                    if CFG.get('ps_mode', 0) >= 3 and now - off_since > 90 and PWR.enabled() and not INCOMING and not isinstance(stack[-1], (Calling, IncomingCall, Ringing)):
                        busy = bool(AptJob.cur and not AptJob.cur.done) or bool(sh('pgrep -x mpg123; pgrep -x ffmpeg; pgrep -x aplay'))
                        if PWR.can_suspend(bool(BATT and BATT.online), bool(jgmsim and jgmsim.modem_up()), CFG.get('ps_suspend_modem', False), busy):
                            nxt = None
                            a_ = CFG.get('alarm', {})
                            if a_.get('on'):
                                t_ = datetime.datetime.now().replace(hour=a_['h'], minute=a_['m'], second=0, microsecond=0)
                                if t_ <= datetime.datetime.now(): t_ += datetime.timedelta(days=1)
                                nxt = (t_ - datetime.datetime.now()).total_seconds() - 5
                            PWR.suspend_once(nxt); last_batt = 0; continue
                elif off_since is not None: off_since = None; PWR.screen_on()
            except Exception: traceback.print_exc()
        if now - last_ns > 5:                                   # timed mode: show the clock when the window opens, even if the screen is already off
            last_ns = now
            if ns_mode() == 1:
                if not ns_in_window(): NS_DISMISSED[0] = False
                elif ns_wanted() and not NS_DISMISSED[0] and not isinstance(stack[-1], (NightClock, Calling, IncomingCall, Ringing)) and (not S['on'] or now - S['last'] > (CFG['timeout'] or 30)): to_nightstand(); dirty = True
        if CFG['alarm']['on'] and now - last_alarm > 5:
            last_alarm = now; n = datetime.datetime.now(); a = CFG['alarm']; day = n.strftime('%Y-%m-%d')
            if n.hour == a['h'] and n.minute == a['m'] and a.get('fired') != day:
                a['fired'] = day; save_cfg(); S['locked'] = False; screen(True); S['last'] = now; stack.append(Ringing()); dirty = True
        if isinstance(app, Terminal) and app.pump(): dirty = True
        if S['on'] and (dirty or (app.tick and now - last_tick > app.tick)) and now - scr.last >= 0.12:
            if isinstance(stack[0], Grid) and stack[0].home: stack[0].ids = home_ids()
            scr.show(render(app)); dirty = False; last_tick = now
        for k in keys.poll(0.12 if S['on'] else (0.5 if not PWR or PWR.stage < 1 else 1.5)):
            if k in ('SLIDE_CLOSE', 'SLIDE_OPEN'):
                if not CFG.get('slider_enabled', True): continue
                STATE['slider_closed'] = (k == 'SLIDE_CLOSE'); dirty = True
                if CFG.get('slider_calls', True) and isinstance(stack[-1], (Calling, IncomingCall)):      # slide open = answer, slide closed = hang up / decline
                    screen(True); S['locked'] = False; handle(stack, stack[-1].key('CALL' if k == 'SLIDE_OPEN' else 'END')); continue
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
            S['last'] = time.time(); app = cur()
            if S['locked'] and k == 'END' and not isinstance(app, NightClock): screen(False); continue
            if S['locked'] and not isinstance(app, NightClock):
                r = app.key(k)
                if r == 'unlock':
                    if CFG['pin'] and not isinstance(app, PinEntry): S['lock'] = PinEntry()
                    else: S['locked'] = False
                elif r == 'cancel': S['lock'] = LockScreen()
                dirty = True; continue
            if k == 'END' and len(stack) == 1 and isinstance(app, Grid):                            # red key on the home screen: sleep
                if ns_wanted(): NS_DISMISSED[0] = False; to_nightstand(); dirty = True; continue
                lock(); screen(False); continue
            try:
                if k == 'LSK' and not isinstance(app, (Popup, Grid, Editor)) and app.options(): res = ('open', Popup('Options', app.options()))
                elif k == 'LSK' and isinstance(app, Editor): res = ('open', Popup('Options', app.options()))
                else: res = app.key(k)
            except Exception: res = None; traceback.print_exc()
            handle(stack, res); dirty = True
        if not dirty and not cur().tick: time.sleep(0.02)

if __name__ == '__main__':
    while True:
        try: main()
        except KeyboardInterrupt: break
        except Exception: traceback.print_exc(); time.sleep(2)
