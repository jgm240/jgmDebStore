"""JWatch BLE protocol (same UUIDs and byte layouts as the JWLink Android/iOS/KaiOS apps)."""
import struct, datetime, unicodedata, time

U = {
    'HEART_RATE_MEASUREMENT': '00002a37-0000-1000-8000-00805f9b34fb', 'BATTERY_LEVEL': '00002a19-0000-1000-8000-00805f9b34fb',
    'FIRMWARE_REVISION': '00002a26-0000-1000-8000-00805f9b34fb', 'CURRENT_TIME': '00002a2b-0000-1000-8000-00805f9b34fb',
    'LOCAL_TIME_INFO': '00002a0f-0000-1000-8000-00805f9b34fb', 'ALERT_LEVEL': '00002a06-0000-1000-8000-00805f9b34fb',
    'STEP_COUNT': '00030001-78fc-48fe-8e23-433b3a1942d0', 'WATCH_NAME': '36680df1-1e57-4dde-a2bd-b1e65eb24552',
    'DND': '9214f6b5-01ea-4c6b-8f1e-6a3a3e5f6c10', 'FACE': '9214f6b6-01ea-4c6b-8f1e-6a3a3e5f6c10', 'PHONE_BATTERY': '9214f6b3-01ea-4c6b-8f1e-6a3a3e5f6c10',
    'HEALTH_SUMMARY': '4d8a2f11-6b1e-4c3a-9a57-2f1d8c6e0b11', 'HEALTH_WORKOUT': '4d8a2f12-6b1e-4c3a-9a57-2f1d8c6e0b11'}
FACE_NAMES = ['Digital', 'Analog', 'PineTimeStyle', 'Terminal', 'Infineat', 'Casio G7710', 'Pride flag', 'Modular', 'Simple', 'Astronomy']
DND_NAMES = ['On', 'Off', 'Sleep']      # value written: 0 on, 1 off, 2 sleep

def current_time(now=None):
    n = now or datetime.datetime.now()
    return struct.pack('<HBBBBBBBB', n.year, n.month, n.day, n.hour, n.minute, n.second, n.isoweekday(), 0, 0)
def local_time_info():
    off = -time.timezone if not time.localtime().tm_isdst else -time.altzone
    return struct.pack('<bb', round(off / 900), 0)
def ascii_bytes(text, maxlen):
    t = unicodedata.normalize('NFD', text); return bytes(ord(c) for c in t if 0x20 <= ord(c) < 0x7f)[:maxlen]
FIND_WATCH = bytes([2])
def dnd_value(mode): return bytes([mode])
def phone_battery(percent, charging): return bytes([max(0, min(100, round(percent))), 1 if charging else 0])
def parse_battery(b): return b[0]
def parse_firmware(b): return b.split(b'\0')[0].decode('ascii', 'replace')
def parse_heart_rate(b):
    if len(b) < 2: return None
    return (b[1] | (b[2] << 8)) if (b[0] & 1) and len(b) >= 3 else b[1]
def parse_steps(b): return struct.unpack('<I', b[:4])[0] if len(b) >= 4 else None
def parse_health_summary(b):
    if len(b) < 6 or b[0] != 1: return None
    return {'light_h': b[1] / 2, 'deep_h': b[2] / 2, 'still_h': b[3] / 2, 'bed_end_hour': b[4], 'stand_hours': b[5]}
def parse_workout(b):
    if len(b) < 16 or b[0] != 1 or b[1] == 0: return None
    start, dur = struct.unpack('<II', b[2:10])
    return {'start': datetime.datetime.fromtimestamp(start), 'minutes': round(dur / 60), 'avg_hr': b[10], 'max_hr': b[11], 'steps': struct.unpack('<H', b[12:14])[0], 'kcal': struct.unpack('<H', b[14:16])[0]}
def face_buffer(cfg): return bytes([1] + [int(cfg[i]) if i < len(cfg) else 0 for i in range(1, 14)])
def parse_face(b): return list(b[:14]) if len(b) >= 14 else None
