"""jgmDeb: graphical (X11) apps on the phone (own work).

Xvfb gives a virtual X screen whose pixels live in a memory-mapped file (-fbdir); the jgmDeb shell reads that file and draws it on the 240x320 panel, and sends the
keypad as pointer / keyboard events through the XTEST extension. matchbox-window-manager keeps every window full-screen. The .desktop files of installed packages
are the list of graphical apps ("Applications").

XServer.start() / .stop() / .running() / .grab() -> PIL image; XInput: move, click, wheel, key, type_text; apps(): installed graphical apps; launch(app).
"""
import os, re, struct, mmap, subprocess, time, glob, configparser, shlex, hashlib
import numpy as np
from PIL import Image, ImageDraw

DISPLAY = ':1'
FBDIR = '/run/xvfb'
SIZE = (480, 640)                         # exactly 2x the panel: "fit" zoom is a clean 2:1 downscale
ENV = {'DISPLAY': DISPLAY, 'HOME': '/root', 'USER': 'root', 'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': '/run/user/0', 'NO_AT_BRIDGE': '1', 'GDK_BACKEND': 'x11',
       'LIBGL_ALWAYS_SOFTWARE': '1', 'GTK_THEME': 'Adwaita', 'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'}

def sh(cmd, t=20):
    try: return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t).stdout.strip()
    except Exception as e: return str(e)

class XServer:
    def __init__(self, size=SIZE): self.size = size; self._mm = None; self._file = None; self._hdr = None
    def running(self): return bool(sh('pgrep -x Xvfb'))
    def start(self, wait=15):
        if self.running() and os.path.exists(FBDIR + '/Xvfb_screen0'): return True
        os.makedirs(FBDIR, exist_ok=True); os.makedirs('/run/user/0', mode=0o700, exist_ok=True)
        w, h = self.size
        subprocess.Popen(['Xvfb', DISPLAY, '-screen', '0', '%dx%dx24' % (w, h), '-fbdir', FBDIR, '-nolisten', 'tcp', '-ac', '-dpi', '110'], env=ENV, start_new_session=True,
                         stdout=open('/var/log/xvfb.log', 'w'), stderr=subprocess.STDOUT)
        end = time.time() + wait
        while time.time() < end and not os.path.exists(FBDIR + '/Xvfb_screen0'): time.sleep(0.2)
        for _ in range(40):                                   # the framebuffer file exists before the server accepts clients
            if sh('DISPLAY=%s xset q >/dev/null 2>&1 && echo ok' % DISPLAY, 5) == 'ok': break
            time.sleep(0.25)
        for _ in range(4):                                    # the window manager sometimes exits if it races the server start: retry
            time.sleep(1.0)
            subprocess.Popen(['matchbox-window-manager', '-use_titlebar', 'no', '-use_cursor', 'no'], env=ENV, start_new_session=True, stdout=open('/var/log/xwm.log', 'w'), stderr=subprocess.STDOUT)
            time.sleep(1.5)
            if sh('pgrep -x matchbox-window'): break
        sh('DISPLAY=%s xsetroot -solid "#1b2630"' % DISPLAY)
        sh('echo "XTerm*faceName: DejaVu Sans Mono\nXTerm*faceSize: 13\nXTerm*scrollBar: false\nXTerm*saveLines: 500" | DISPLAY=%s xrdb -merge 2>/dev/null' % DISPLAY)
        return self.running()
    def stop(self):
        self._close(); sh('pkill -x matchbox-window-manager; pkill -x Xvfb'); sh('rm -rf %s' % FBDIR)
    def _close(self):
        try:
            if self._mm: self._mm.close()
            if self._file: self._file.close()
        except Exception: pass
        self._mm = self._file = self._hdr = None
    def _open(self):
        self._close(); self._file = open(FBDIR + '/Xvfb_screen0', 'rb'); self._mm = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        h = struct.unpack('>25I', self._mm[:100]); hdr_size, w, ht, bpp, bpl, ncolors = h[0], h[4], h[5], h[11], h[12], h[19]
        self._hdr = (hdr_size + ncolors * 12, w, ht, bpp, bpl)
    def grab(self):
        """-> PIL RGB image of the whole X screen, or None"""
        try:
            if self._mm is None: self._open()
            off, w, h, bpp, bpl = self._hdr
            a = np.frombuffer(self._mm, dtype=np.uint8, count=h * bpl, offset=off).reshape(h, bpl // 4, 4)[:, :w, :3][:, :, ::-1]
            return Image.fromarray(np.ascontiguousarray(a), 'RGB')
        except Exception: self._close(); return None

class XInput:
    """pointer + keyboard through XTEST over one persistent connection (python3-xlib)"""
    def __init__(self): self.d = None; self.x = self.y = 0
    def conn(self):
        if self.d is None:
            from Xlib import display
            self.d = display.Display(DISPLAY); self.w, self.h = self.d.screen().width_in_pixels, self.d.screen().height_in_pixels
        return self.d
    def close(self):
        try:
            if self.d: self.d.close()
        except Exception: pass
        self.d = None
    def _fake(self, typ, detail=0, x=0, y=0):
        from Xlib.ext import xtest
        d = self.conn(); xtest.fake_input(d, typ, detail, x=x, y=y); d.sync()
    def move(self, x, y):
        from Xlib import X
        self.conn(); self.x = max(0, min(self.w - 1, int(x))); self.y = max(0, min(self.h - 1, int(y))); self._fake(X.MotionNotify, 0, self.x, self.y)
    def click(self, button=1, double=False):
        from Xlib import X
        for _ in range(2 if double else 1): self._fake(X.ButtonPress, button); self._fake(X.ButtonRelease, button)
    def wheel(self, up=True): self.click(4 if up else 5)
    def _keycode(self, keysym_name):
        from Xlib import XK
        d = self.conn(); ks = XK.string_to_keysym(keysym_name)
        if ks == 0 and len(keysym_name) == 1: ks = ord(keysym_name)
        kc = d.keysym_to_keycode(ks)
        shift = bool(kc) and d.keycode_to_keysym(kc, 0) != ks and d.keycode_to_keysym(kc, 1) == ks
        return kc, shift
    def key(self, name, ctrl=False, alt=False, shift=False):
        """name: X keysym name ('Return', 'Escape', 'BackSpace', 'a', 'F4' ...)"""
        from Xlib import X
        kc, need_shift = self._keycode(name)
        if not kc: return False
        mods = []
        if ctrl: mods.append(self._keycode('Control_L')[0])
        if alt: mods.append(self._keycode('Alt_L')[0])
        if shift or need_shift: mods.append(self._keycode('Shift_L')[0])
        for m in mods: self._fake(X.KeyPress, m)
        self._fake(X.KeyPress, kc); self._fake(X.KeyRelease, kc)
        for m in reversed(mods): self._fake(X.KeyRelease, m)
        return True
    def type_text(self, text):
        names = {' ': 'space', '\n': 'Return', '\t': 'Tab'}
        for ch in text: self.key(names.get(ch, ch))
    def windows(self):
        """[(window id, title)] of the managed windows"""
        out = []
        for l in sh('DISPLAY=%s wmctrl -l 2>/dev/null' % DISPLAY).splitlines():
            p = l.split(None, 3)
            if len(p) >= 4: out.append((p[0], p[3]))
        return out
    def focus(self, wid): sh('DISPLAY=%s wmctrl -i -a %s' % (DISPLAY, wid))
    def close_active(self): sh('DISPLAY=%s wmctrl -c :ACTIVE:' % DISPLAY)

# ---------------------------------------------------------------------------------------------------------------- .desktop files = the list of graphical apps
APP_DIRS = ['/usr/share/applications', '/usr/local/share/applications']
SKIP_CATS = {'Settings', 'DesktopSettings', 'X-XFCE-SettingsDialog', 'ConsoleOnly'}
def _clean_exec(cmd):
    cmd = re.sub(r'%[fFuUdDnNickvm]', '', cmd.replace('%%', '\0')).replace('\0', '%')
    return ' '.join(cmd.split())
def apps():
    """installed graphical apps: [{'id','name','comment','exec','icon','cats','terminal','file'}], sorted by name"""
    out = {}
    for d in APP_DIRS:
        for f in glob.glob(d + '/*.desktop'):
            cp = configparser.RawConfigParser(strict=False, interpolation=None)
            try: cp.read(f, encoding='utf-8')
            except Exception: continue
            if not cp.has_section('Desktop Entry'): continue
            e = dict(cp.items('Desktop Entry'))
            if e.get('type', 'Application') != 'Application' or e.get('nodisplay', '').lower() == 'true' or e.get('hidden', '').lower() == 'true': continue
            cats = [c for c in e.get('categories', '').split(';') if c]
            if SKIP_CATS & set(cats) or not e.get('exec'): continue
            only = e.get('onlyshowin', '')
            if only and not any(x in only for x in ('X-Generic', 'Unity', 'GNOME', 'XFCE', 'LXDE', 'MATE')): continue
            name = e.get('name') or os.path.basename(f)[:-8]
            out[os.path.basename(f)[:-8]] = {'id': os.path.basename(f)[:-8], 'name': name, 'comment': e.get('comment', ''), 'exec': _clean_exec(e['exec']), 'icon': e.get('icon', ''),
                                             'cats': cats, 'terminal': e.get('terminal', '').lower() == 'true', 'file': f}
    return sorted(out.values(), key=lambda a: a['name'].lower())
def launch(app):
    """start the app on the X screen; returns the Popen"""
    cmd = app['exec']
    if app.get('terminal'): cmd = 'xterm -e ' + cmd
    return subprocess.Popen(['sh', '-c', 'exec ' + cmd], env=ENV, start_new_session=True, stdout=open('/var/log/xapp.log', 'a'), stderr=subprocess.STDOUT, cwd='/root')

_icon_cache = {}
def icon(app, size=40):
    """PIL RGBA icon: the theme's PNG if one is installed, otherwise a coloured tile with the app's initial"""
    key = (app['id'], size)
    if key in _icon_cache: return _icon_cache[key]
    name = app.get('icon') or ''; im = None
    cands = [name] if name.startswith('/') else []
    if name and not name.startswith('/'):
        for sz in ('48x48', '64x64', '32x32', '128x128', '256x256', '24x24', '22x22', '16x16'):
            cands += glob.glob('/usr/share/icons/*/%s/apps/%s.png' % (sz, name))
        cands += ['/usr/share/pixmaps/%s.png' % name]
    for c in cands:
        try: im = Image.open(c).convert('RGBA').resize((size, size), Image.LANCZOS); break
        except Exception: continue
    if im is None:
        h = int(hashlib.md5(app['name'].encode()).hexdigest()[:6], 16); col = (60 + h % 120, 60 + (h >> 8) % 120, 60 + (h >> 16) % 120)
        im = Image.new('RGBA', (size, size), (0, 0, 0, 0)); d = ImageDraw.Draw(im); d.rounded_rectangle((0, 0, size - 1, size - 1), size // 5, fill=col + (255,))
        d.text((size / 2, size / 2), (app['name'][:1] or '?').upper(), fill=(255, 255, 255), anchor='mm', font=_font(int(size * 0.55)))
    _icon_cache[key] = im; return im
_fonts = {}
def _font(sz):
    from PIL import ImageFont
    if sz not in _fonts:
        try: _fonts[sz] = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', sz)
        except Exception: _fonts[sz] = ImageFont.load_default()
    return _fonts[sz]

CATEGORY_GROUPS = [('Games', {'Game'}), ('Internet', {'Network', 'WebBrowser', 'Email', 'Chat', 'InstantMessaging', 'FileTransfer'}), ('Office', {'Office', 'WordProcessor', 'Spreadsheet', 'Calendar', 'Presentation'}),
                   ('Graphics', {'Graphics', 'Photography', 'Viewer', '2DGraphics', 'RasterGraphics'}), ('Audio & Video', {'AudioVideo', 'Audio', 'Video', 'Player', 'Music'}),
                   ('Education', {'Education', 'Science', 'Math', 'Languages'}), ('Utilities', {'Utility', 'Accessories', 'TextEditor', 'Calculator', 'Archiving', 'FileManager'}),
                   ('System', {'System', 'Monitor', 'TerminalEmulator'}), ('Development', {'Development', 'IDE'})]
def group_of(cats):
    s = set(cats)
    for name, keys in CATEGORY_GROUPS:
        if s & keys: return name
    return 'Other'
