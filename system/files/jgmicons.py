"""jgmDeb app icons (own work): drawn with Pillow at 4x and scaled down (anti-aliased), no font symbols or image files needed.
icon(id, size) -> RGBA image. Colours are chosen here; the glyph is drawn in white/dark on a rounded-square or circular base."""
import math
from PIL import Image, ImageDraw

WHITE = (250, 252, 252); DARK = (18, 26, 30)
_cache = {}

def _canvas(size, top, bottom, shape='round'):
    S = size * 4
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    grad = Image.new('RGBA', (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1); gd.line([(0, y), (S, y)], fill=tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)) + (255,))
    mask = Image.new('L', (S, S), 0); md = ImageDraw.Draw(mask)
    if shape == 'circle': md.ellipse((0, 0, S - 1, S - 1), fill=255)
    else: md.rounded_rectangle((0, 0, S - 1, S - 1), int(S * 0.24), fill=255)
    img.paste(grad, (0, 0), mask)
    return img, ImageDraw.Draw(img), S / 100.0

def P(s, pts): return [(x * s, y * s) for x, y in pts]
def R(s, b): return tuple(v * s for v in b)

# --------------------------------------------------------------------------------------------- glyphs (100x100 space)
def g_phone(d, s):
    d.polygon(P(s, [(24, 34), (34, 22), (46, 31), (42, 43), (48, 54), (59, 60), (71, 56), (79, 66), (68, 78), (55, 77), (36, 63), (24, 46)]), fill=WHITE)
def g_contacts(d, s):
    d.ellipse(R(s, (35, 20, 65, 50)), fill=WHITE); d.pieslice(R(s, (20, 54, 80, 112)), 180, 360, fill=WHITE)
def g_messages(d, s, base):
    d.rounded_rectangle(R(s, (16, 22, 84, 66)), int(9 * s), fill=WHITE); d.polygon(P(s, [(30, 62), (30, 82), (52, 64)]), fill=WHITE)
    for i, w in enumerate((56, 56, 38)): d.rounded_rectangle(R(s, (26, 32 + i * 10, 26 + w, 37 + i * 10)), int(2.5 * s), fill=base)
def g_settings(d, s, base):
    cx = cy = 50
    for i in range(8):
        a = math.radians(i * 45); c, sn = math.cos(a), math.sin(a)
        pts = [(cx + c * 18 - sn * 6, cy + sn * 18 + c * 6), (cx + c * 33 - sn * 6, cy + sn * 33 + c * 6), (cx + c * 33 + sn * 6, cy + sn * 33 - c * 6), (cx + c * 18 + sn * 6, cy + sn * 18 - c * 6)]
        d.polygon(P(s, pts), fill=WHITE)
    d.ellipse(R(s, (26, 26, 74, 74)), fill=WHITE); d.ellipse(R(s, (39, 39, 61, 61)), fill=base)
def g_clock(d, s, base):
    d.ellipse(R(s, (16, 16, 84, 84)), fill=WHITE)
    for i in range(12):
        a = math.radians(i * 30); r0, r1 = (34, 40) if i % 3 else (31, 40)
        d.line([(50 + math.sin(a) * r0) * s, (50 - math.cos(a) * r0) * s, (50 + math.sin(a) * r1) * s, (50 - math.cos(a) * r1) * s], fill=base, width=int(2.6 * s))
    d.line([50 * s, 50 * s, 50 * s, 30 * s], fill=DARK, width=int(5 * s)); d.line([50 * s, 50 * s, 64 * s, 58 * s], fill=DARK, width=int(4 * s)); d.ellipse(R(s, (46, 46, 54, 54)), fill=DARK)
def g_calc(d, s, base):
    d.rounded_rectangle(R(s, (24, 12, 76, 88)), int(7 * s), fill=WHITE); d.rounded_rectangle(R(s, (30, 18, 70, 36)), int(3 * s), fill=DARK)
    for r in range(3):
        for c in range(3): d.rounded_rectangle(R(s, (30 + c * 14, 44 + r * 14, 40 + c * 14, 54 + r * 14)), int(2 * s), fill=base)
def g_browser(d, s, base):
    d.ellipse(R(s, (14, 14, 86, 86)), fill=WHITE)
    d.ellipse(R(s, (14, 14, 86, 86)), outline=base, width=int(4 * s)); d.ellipse(R(s, (34, 14, 66, 86)), outline=base, width=int(3.5 * s))
    d.line(R(s, (50, 14, 50, 86)), fill=base, width=int(3.5 * s)); d.line(R(s, (14, 50, 86, 50)), fill=base, width=int(3.5 * s))
    d.arc(R(s, (14, 26, 86, 74)), 0, 360, fill=base, width=int(3 * s))
def g_notes(d, s, base):
    d.rounded_rectangle(R(s, (24, 12, 72, 88)), int(6 * s), fill=WHITE)
    for i in range(5): d.line(R(s, (32, 28 + i * 12, 64, 28 + i * 12)), fill=base, width=int(3 * s))
    d.polygon(P(s, [(78, 38), (86, 46), (56, 82), (47, 84), (50, 75)]), fill=(255, 205, 60)); d.polygon(P(s, [(78, 38), (86, 46), (82, 50), (74, 42)]), fill=(236, 96, 86))
def g_files(d, s, base):
    d.polygon(P(s, [(12, 30), (40, 30), (46, 38), (88, 38), (88, 84), (12, 84)]), fill=(255, 226, 120)); d.rounded_rectangle(R(s, (12, 44, 88, 84)), int(5 * s), fill=WHITE)
def g_terminal(d, s, base):
    d.rounded_rectangle(R(s, (12, 18, 88, 82)), int(7 * s), fill=DARK)
    d.line(R(s, (26, 36, 42, 50)), fill=(70, 230, 160), width=int(6 * s)); d.line(R(s, (42, 50, 26, 64)), fill=(70, 230, 160), width=int(6 * s)); d.line(R(s, (50, 66, 72, 66)), fill=WHITE, width=int(6 * s))
def g_calendar(d, s, base):
    d.rounded_rectangle(R(s, (14, 16, 86, 86)), int(7 * s), fill=WHITE); d.rounded_rectangle(R(s, (14, 16, 86, 38)), int(7 * s), fill=(236, 70, 100)); d.rectangle(R(s, (14, 30, 86, 38)), fill=(236, 70, 100))
    for r in range(3):
        for c in range(4): d.ellipse(R(s, (22 + c * 16, 46 + r * 12, 29 + c * 16, 53 + r * 12)), fill=DARK if (r, c) != (1, 2) else (236, 70, 100))
def g_torch(d, s, base):
    d.polygon(P(s, [(38, 14), (62, 14), (68, 30), (32, 30)]), fill=WHITE); d.rounded_rectangle(R(s, (38, 30, 62, 88)), int(4 * s), fill=WHITE)
    d.ellipse(R(s, (42, 36, 58, 52)), fill=(255, 210, 60))
    for dx, dy in ((18, 8), (50, 4), (82, 8)): d.line(R(s, (50, 14, dx, dy)), fill=WHITE, width=int(3 * s))
def g_snake(d, s, base):
    pts = [(20, 74), (30, 64), (42, 66), (52, 58), (50, 46), (60, 38), (72, 40), (80, 30)]
    for i, (x, y) in enumerate(pts): d.ellipse(R(s, (x - 8, y - 8, x + 8, y + 8)), fill=WHITE if i % 2 else (210, 255, 210))
    d.ellipse(R(s, (82, 20, 98, 36)), fill=WHITE); d.ellipse(R(s, (90, 24, 95, 29)), fill=DARK); d.ellipse(R(s, (14, 70, 22, 78)), fill=(236, 90, 86))
def g_camera(d, s, base):
    d.rounded_rectangle(R(s, (12, 30, 88, 82)), int(8 * s), fill=WHITE); d.rounded_rectangle(R(s, (34, 20, 56, 34)), int(4 * s), fill=WHITE)
    d.ellipse(R(s, (32, 38, 68, 74)), fill=DARK); d.ellipse(R(s, (38, 44, 62, 68)), fill=(90, 150, 220)); d.ellipse(R(s, (44, 48, 52, 56)), fill=WHITE); d.ellipse(R(s, (74, 36, 82, 44)), fill=base)
def g_music(d, s, base):
    d.ellipse(R(s, (24, 64, 50, 86)), fill=WHITE); d.ellipse(R(s, (58, 56, 84, 78)), fill=WHITE)
    d.rectangle(R(s, (45, 24, 51, 74)), fill=WHITE); d.rectangle(R(s, (79, 16, 85, 68)), fill=WHITE); d.polygon(P(s, [(45, 24), (85, 14), (85, 28), (45, 38)]), fill=WHITE)
def g_wifi(d, s, base):
    for r in (46, 32, 18): d.arc(R(s, (50 - r, 76 - r, 50 + r, 76 + r)), 225, 315, fill=WHITE, width=int(7 * s))
    d.ellipse(R(s, (43, 70, 57, 84)), fill=WHITE)
def g_voicemail(d, s, base):
    d.rounded_rectangle(R(s, (12, 30, 88, 74)), int(10 * s), fill=WHITE)
    d.ellipse(R(s, (22, 40, 42, 60)), fill=base); d.ellipse(R(s, (58, 40, 78, 60)), fill=base); d.ellipse(R(s, (27, 45, 37, 55)), fill=WHITE); d.ellipse(R(s, (63, 45, 73, 55)), fill=WHITE)
    d.rectangle(R(s, (38, 64, 62, 68)), fill=base)
def g_monitor(d, s, base):
    d.rounded_rectangle(R(s, (12, 16, 88, 84)), int(7 * s), fill=DARK)
    for i, h in enumerate((22, 38, 30, 52, 44)): d.rounded_rectangle(R(s, (20 + i * 13, 76 - h, 30 + i * 13, 76)), int(2 * s), fill=(70, 230, 160) if h > 40 else WHITE)
def g_battery(d, s, base):
    d.rounded_rectangle(R(s, (18, 32, 78, 70)), int(6 * s), fill=WHITE); d.rectangle(R(s, (78, 42, 86, 60)), fill=WHITE)
    d.rounded_rectangle(R(s, (24, 38, 56, 64)), int(3 * s), fill=base); d.polygon(P(s, [(44, 40), (36, 54), (44, 54), (40, 66), (54, 50), (46, 50)]), fill=WHITE)
def g_alarm(d, s, base):
    d.ellipse(R(s, (20, 26, 80, 86)), fill=WHITE)
    d.pieslice(R(s, (14, 12, 40, 38)), 180, 360, fill=WHITE); d.pieslice(R(s, (60, 12, 86, 38)), 180, 360, fill=WHITE)
    d.line(R(s, (30, 84, 24, 92)), fill=WHITE, width=int(4 * s)); d.line(R(s, (70, 84, 76, 92)), fill=WHITE, width=int(4 * s))
    d.ellipse(R(s, (28, 34, 72, 78)), fill=base); d.line(R(s, (50, 58, 50, 42)), fill=WHITE, width=int(4 * s)); d.line(R(s, (50, 58, 61, 64)), fill=WHITE, width=int(4 * s))


def g_store(d, s):
    d.polygon(P(s, [(24, 36), (76, 36), (80, 86), (20, 86)]), fill=WHITE)
    d.arc(R(s, (34, 14, 66, 52)), 180, 360, fill=WHITE, width=int(5 * s))
    d.ellipse(R(s, (36, 50, 44, 58)), fill=DARK); d.ellipse(R(s, (56, 50, 64, 58)), fill=DARK)
def g_news(d, s):
    d.rounded_rectangle(R(s, (22, 20, 78, 80)), int(5 * s), fill=WHITE)
    d.rectangle(R(s, (30, 28, 52, 44)), fill=DARK)
    for y in (30, 38): d.line(R(s, (58, y, 70, y)), fill=DARK, width=int(4 * s))
    for y in (52, 60, 68): d.line(R(s, (30, y, 70, y)), fill=DARK, width=int(4 * s))
def g_weather(d, s):
    d.ellipse(R(s, (50, 14, 82, 46)), fill=(255, 226, 90))
    for a in range(0, 360, 45):
        x, y = 66 + 24 * math.cos(math.radians(a)), 30 + 24 * math.sin(math.radians(a)); d.line(R(s, (66 + 19 * math.cos(math.radians(a)), 30 + 19 * math.sin(math.radians(a)), x, y)), fill=(255, 226, 90), width=int(3 * s))
    d.ellipse(R(s, (14, 44, 46, 76)), fill=WHITE); d.ellipse(R(s, (32, 36, 68, 76)), fill=WHITE); d.ellipse(R(s, (52, 48, 86, 76)), fill=WHITE); d.rectangle(R(s, (30, 56, 70, 76)), fill=WHITE)
def g_ytdl(d, s):
    d.polygon(P(s, [(40, 18), (60, 18), (60, 46), (74, 46), (50, 74), (26, 46), (40, 46)]), fill=WHITE)
    d.rounded_rectangle(R(s, (22, 78, 78, 86)), int(3 * s), fill=WHITE)
def g_bell(d, s):
    d.pieslice(R(s, (24, 20, 76, 80)), 180, 360, fill=WHITE); d.rectangle(R(s, (24, 50, 76, 70)), fill=WHITE); d.rounded_rectangle(R(s, (16, 66, 84, 74)), int(4 * s), fill=WHITE)
    d.ellipse(R(s, (42, 74, 58, 90)), fill=WHITE); d.ellipse(R(s, (46, 12, 54, 22)), fill=WHITE)
def g_radio(d, s):
    d.rounded_rectangle(R(s, (16, 36, 84, 80)), int(8 * s), fill=WHITE); d.line(R(s, (30, 36, 70, 14)), fill=WHITE, width=int(4 * s))
    d.ellipse(R(s, (26, 46, 56, 70)), fill=DARK)
    for y in (48, 58, 68): d.line(R(s, (62, y, 76, y)), fill=DARK, width=int(3 * s))
def g_share(d, s):
    for cx, cy in ((30, 50), (70, 28), (70, 72)): d.ellipse(R(s, (cx - 11, cy - 11, cx + 11, cy + 11)), fill=WHITE)
    d.line(R(s, (30, 50, 70, 28)), fill=WHITE, width=int(5 * s)); d.line(R(s, (30, 50, 70, 72)), fill=WHITE, width=int(5 * s))

# id: (draw, top colour, bottom colour, shape)
ICONS = {
    'phone': (g_phone, (70, 210, 120), (30, 150, 80)), 'contacts': (g_contacts, (90, 170, 250), (40, 100, 220)), 'messages': (g_messages, (255, 190, 80), (240, 130, 30)),
    'settings': (g_settings, (170, 180, 190), (100, 110, 122)), 'clock': (g_clock, (250, 130, 120), (210, 60, 70)), 'calc': (g_calc, (190, 140, 250), (130, 80, 210)),
    'browser': (g_browser, (80, 215, 235), (30, 150, 200)), 'notes': (g_notes, (255, 226, 110), (240, 180, 40)), 'files': (g_files, (150, 190, 110), (90, 140, 60)),
    'terminal': (g_terminal, (74, 84, 96), (34, 42, 52)), 'calendar': (g_calendar, (250, 250, 250), (225, 232, 236)), 'torch': (g_torch, (255, 214, 90), (240, 150, 40)),
    'snake': (g_snake, (100, 214, 110), (30, 140, 70)), 'camera': (g_camera, (220, 110, 110), (170, 50, 70)), 'music': (g_music, (170, 130, 240), (110, 70, 210)),
    'wifi': (g_wifi, (100, 180, 250), (40, 110, 220)), 'voicemail': (g_voicemail, (136, 146, 250), (80, 90, 220)), 'monitor': (g_monitor, (60, 80, 100), (24, 34, 46)),
    'battery': (g_battery, (110, 224, 140), (40, 170, 90)), 'alarm': (g_alarm, (255, 150, 120), (226, 80, 70)),
    'store': (g_store, (70, 140, 220), (40, 100, 200)), 'news': (g_news, (240, 120, 100), (200, 60, 70)), 'weather': (g_weather, (90, 170, 245), (40, 100, 210)), 'ytdl': (g_ytdl, (240, 80, 80), (190, 30, 50)),
    'notifs': (g_bell, (255, 190, 80), (235, 120, 40)), 'radio': (g_radio, (110, 200, 190), (40, 140, 140)), 'hotspot': (g_share, (120, 210, 150), (40, 150, 100))}
_NEEDS_BASE = {'news', 'radio', 'messages', 'settings', 'clock', 'calc', 'browser', 'notes', 'files', 'terminal', 'calendar', 'torch', 'snake', 'camera', 'music', 'wifi', 'voicemail', 'monitor', 'battery', 'alarm'}

# --------------------------------------------------------------------------------------------- themed rendering
import numpy as np
THEMES = ['normal', 'dark', 'metal', 'wood', 'paper']
RADII = [0.0, 0.12, 0.24, 0.36, 0.5]                       # fraction of the icon size: square .. circle
COL = {'store': (70, 140, 220), 'phone': (52, 168, 110), 'contacts': (66, 128, 196), 'messages': (236, 160, 60), 'settings': (112, 120, 132), 'clock': (214, 88, 84), 'calc': (140, 100, 190),
       'browser': (60, 160, 190), 'notes': (226, 186, 70), 'files': (122, 160, 92), 'terminal': (46, 54, 62), 'calendar': (214, 70, 92), 'torch': (230, 150, 50),
       'snake': (80, 170, 90), 'camera': (190, 80, 92), 'music': (140, 100, 200), 'wifi': (70, 140, 210), 'voicemail': (110, 120, 220), 'monitor': (52, 72, 92),
       'battery': (80, 180, 112), 'alarm': (230, 110, 90), 'news': (200, 80, 80), 'weather': (80, 150, 225), 'ytdl': (210, 50, 60), 'notifs': (232, 150, 60),
       'radio': (90, 170, 160), 'hotspot': (90, 180, 130)}

def _glyph_mask(name, S):
    """draw the glyph in a transparent layer and turn it into one alpha mask: white parts solid, coloured accents half, dark/cut-out parts clear"""
    global WHITE, DARK
    spec = ICONS.get(name); fn = spec[0]; s = S / 100.0
    layer = Image.new('RGBA', (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    KEY = (255, 0, 255)
    try: fn(d, s, KEY)
    except TypeError: fn(d, s)
    a = np.asarray(layer).astype(np.float32); rgb, al = a[..., :3], a[..., 3] / 255.0
    white = (rgb.min(axis=2) > 225); black = (rgb.max(axis=2) < 60); key = (np.abs(rgb - np.array(KEY)).sum(axis=2) < 90)
    m = np.where(white, al, np.where(black | key, 0.0, al * 0.6))
    return Image.fromarray((m * 255).astype(np.uint8), 'L')

def _rmask(S, r):
    m = Image.new('L', (S, S), 0); d = ImageDraw.Draw(m)
    if r >= 0.5: d.ellipse((0, 0, S - 1, S - 1), fill=255)
    else: d.rounded_rectangle((0, 0, S - 1, S - 1), int(S * r), fill=255)
    return m

def _grad(S, c0, c1):
    t = np.linspace(0, 1, S)[:, None, None]; a = np.array(c0)[None, None, :] * (1 - t) + np.array(c1)[None, None, :] * t
    return np.broadcast_to(a, (S, S, 3)).copy()

def _wood(S, seed):
    rng = np.random.RandomState(seed); y, x = np.mgrid[0:S, 0:S].astype(np.float32)
    warp = np.sin(x / S * 3 + seed) * 3 + np.sin(x / S * 11) * 0.8
    ring = np.sin((y + warp * (S / 100.0)) / S * 20 + np.sin(x / S * 2) * 1.2)
    fine = rng.rand(S, S) * 0.35 + np.sin(y / S * 190 + x / S * 6) * 0.12
    t = 0.5 + 0.17 * ring + 0.2 * fine
    dark, light = np.array([112, 70, 38], np.float32), np.array([190, 132, 76], np.float32)
    return dark[None, None, :] * (1 - t[..., None]) + light[None, None, :] * t[..., None]

def _metal(S, seed):
    rng = np.random.RandomState(seed); y, x = np.mgrid[0:S, 0:S].astype(np.float32)
    streak = rng.rand(S, 1) * 0.5 + rng.rand(S, S) * 0.12
    sheen = 0.55 + 0.35 * np.sin((x + y) / S * 3.1) * np.cos((x - y) / S * 1.7)
    t = np.clip(sheen * 0.7 + streak * 0.5, 0, 1)
    dark, light = np.array([104, 112, 122], np.float32), np.array([214, 220, 226], np.float32)
    return dark[None, None, :] * (1 - t[..., None]) + light[None, None, :] * t[..., None]

def _bevel(S, mask, hi=60, lo=60, w=3):
    """light top-left edge, dark bottom-right edge inside the mask"""
    m = np.asarray(mask).astype(np.float32) / 255.0; k = w * S // 100 + 1
    up = np.roll(np.roll(m, k, 0), k, 1); dn = np.roll(np.roll(m, -k, 0), -k, 1)
    return np.clip(m - up, 0, 1) * hi, np.clip(m - dn, 0, 1) * lo

_custom = {}
def register_custom(name, path): _custom[name] = path; [_cache.pop(k) for k in list(_cache) if k[0] == name]
def icon(name, size=46, theme='normal', radius=2):
    key = (name, size, theme, radius)
    if key in _cache: return _cache[key]
    S = size * 4; r = RADII[max(0, min(4, radius))]
    if name in _custom:
        try:
            im = Image.open(_custom[name]).convert('RGBA').resize((S, S), Image.LANCZOS); sh = _rmask(S, r); a = np.minimum(np.asarray(im.getchannel('A')), np.asarray(sh)); im.putalpha(Image.fromarray(a))
            out = im.resize((size, size), Image.LANCZOS); _cache[key] = out; return out
        except Exception: pass
    if name not in ICONS:
        img = Image.new('RGBA', (S, S), (110, 120, 130, 255)); img.putalpha(_rmask(S, r)); out = img.resize((size, size), Image.LANCZOS); _cache[key] = out; return out
    base_col = COL.get(name, (110, 120, 130)); gm = _glyph_mask(name, S); gm_np = np.asarray(gm).astype(np.float32) / 255.0
    shape = _rmask(S, r)
    if theme == 'metal': base = _metal(S, hash(name) % 97); gcol = np.array([42, 50, 58], np.float32); ghi = 1; edge = True
    elif theme == 'wood': base = _wood(S, hash(name) % 97); gcol = np.array([58, 30, 14], np.float32); ghi = 1; edge = True
    elif theme == 'paper': base = np.broadcast_to(np.array([240, 234, 220], np.float32), (S, S, 3)).copy(); gcol = np.array(base_col, np.float32) * 0.85; ghi = 0; edge = False
    elif theme == 'dark': base = _grad(S, (34, 40, 46), (22, 27, 32)); gcol = np.array(base_col, np.float32) * 1.15 + 25; ghi = 0; edge = False
    else: c = np.array(base_col, np.float32); base = _grad(S, c * 1.06, c * 0.94); gcol = np.array([252, 252, 252], np.float32); ghi = 0; edge = False
    gcol = np.clip(gcol, 0, 255); img = base.copy()
    if ghi:                                              # embossed / engraved look: light edge below-right, dark glyph
        sh = np.roll(np.roll(gm_np, S // 70 + 1, 0), S // 70 + 1, 1)
        img = img * (1 - sh[..., None] * 0.55) + np.array([255, 245, 230], np.float32) * (sh[..., None] * 0.55 * (1 - gm_np[..., None]))
    img = img * (1 - gm_np[..., None] * 0.92) + gcol[None, None, :] * (gm_np[..., None] * 0.92)
    if edge or theme == 'normal':
        hi, lo = _bevel(S, shape, 70 if edge else 26, 70 if edge else 30, 3 if edge else 2)
        img = img + hi[..., None] - lo[..., None]
    out = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), 'RGB').convert('RGBA'); out.putalpha(shape)
    out = out.resize((size, size), Image.LANCZOS); _cache[key] = out
    return out
