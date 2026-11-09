"""jgmDeb battery management (own work).

The Nokia 8110 4G battery is a 1500 mAh, 4.4 V high-voltage Li-ion cell. The kernel's fuel gauge (qpnp-vm-bms) needs a user-space
part that Debian does not have, so `capacity` is always 0 and `current_now` is always 0. This module estimates the charge from the
cell voltage instead:

  * open-circuit-voltage (OCV) -> % through a discharge curve for a 4.4 V cell (piecewise linear);
  * while charging the terminal voltage is raised by I*R (~0.1 V at 500 mA), so that is subtracted; while the screen/CPU draw
    current the voltage sags, so the estimate is smoothed and only allowed to move in the direction of the current flow;
  * the last value is remembered across reboots (/data/jgm/battery.json) so the number does not jump after a restart.

It also provides low/critical warnings, a safe shutdown threshold, a charge limit (writes battery/charging_enabled), the charging LED
and CPU governor switching. update() is cheap (a few sysfs reads) and is called every few seconds by the UI shell.
"""
import os, time, json

PS = '/sys/class/power_supply/'
# open-circuit voltage (V) -> percent for a 4.4 V cell
CURVE = [(3.40, 0), (3.55, 2), (3.65, 5), (3.70, 10), (3.75, 18), (3.80, 30), (3.85, 45), (3.90, 55), (3.98, 65), (4.07, 75), (4.17, 85), (4.25, 92), (4.32, 97), (4.37, 100)]
R_INT = 0.21            # ohm, the BMS reports ~206 mOhm
CHARGE_I = 0.5          # A, USB 500 mA

def rd(path, default=''):
    try: return open(path).read().strip()
    except Exception: return default
def wr(path, v):
    try: open(path, 'w').write(str(v)); return True
    except Exception: return False
def ocv_to_pct(v):
    if v <= CURVE[0][0]: return 0.0
    if v >= CURVE[-1][0]: return 100.0
    for (v0, p0), (v1, p1) in zip(CURVE, CURVE[1:]):
        if v0 <= v <= v1: return p0 + (p1 - p0) * (v - v0) / (v1 - v0)
    return 0.0

class BatteryManager:
    LOW, CRITICAL, SHUTDOWN = 15, 5, 2
    def __init__(self, datadir):
        self.state_file = os.path.join(datadir, 'battery.json'); self.log_file = os.path.join(datadir, 'battery.log')
        st = {}
        try: st = json.load(open(self.state_file))
        except Exception: pass
        self.pct = float(st.get('pct', -1)); self.limit = int(st.get('limit', 100)); self.full_seen = False
        self.v = 0.0; self.status = ''; self.charging = False; self.online = False; self.temp = 0.0
        self.events = []; self.warned = set(); self.last_save = 0; self.last_log = 0; self.charging_enabled = True; self.shutdown_at = None
        self.update(first=True)

    def read_voltage(self):
        # the BMS's own OCV reading is only refreshed at boot; the terminal voltage is the live value
        return int(rd(PS + 'battery/voltage_now', '0') or 0) / 1e6
    def update(self, first=False):
        now = time.time(); v = self.read_voltage()
        status = rd(PS + 'battery/status', ''); self.online = rd(PS + 'usb/online', '0') == '1'
        self.status = status; self.v = v; self.charging = status.lower().startswith('charg'); self.temp = int(rd(PS + 'battery/temp', '0') or 0) / 10.0
        if v <= 0: return self.snapshot()
        ocv = v - (CHARGE_I * R_INT if self.charging else 0.0)
        est = ocv_to_pct(ocv)
        if status.lower() == 'full': est = 100.0; self.full_seen = True
        if self.pct < 0: self.pct = est                                              # first ever reading
        else:
            alpha = 0.5 if first else 0.15
            new = self.pct + alpha * (est - self.pct)
            if self.charging and not first: new = max(new, self.pct)                 # charging: never go down
            elif not self.online and not first: new = min(new, self.pct)             # discharging: never go up
            self.pct = new
        self.pct = max(0.0, min(100.0, self.pct))
        self.apply_limit(); self.check_events(now)
        if now - self.last_save > 60: self.save(now)
        if now - self.last_log > 60: self.log(now)
        return self.snapshot()

    def apply_limit(self):
        """charge limit: stop charging at `limit` % (when 100 charging stops by itself), resume 5 % below"""
        if self.limit >= 100:
            if not self.charging_enabled: wr(PS + 'battery/charging_enabled', 1); self.charging_enabled = True
            return
        if self.online and self.charging_enabled and self.pct >= self.limit: wr(PS + 'battery/charging_enabled', 0); self.charging_enabled = False
        elif not self.charging_enabled and (self.pct <= self.limit - 5 or not self.online): wr(PS + 'battery/charging_enabled', 1); self.charging_enabled = True
    def set_limit(self, pct): self.limit = pct; self.apply_limit(); self.save(time.time())

    def check_events(self, now):
        p = self.pct
        if self.online or self.charging:
            self.warned.clear(); self.shutdown_at = None; return
        if p <= self.SHUTDOWN or self.v <= 3.30:
            if self.shutdown_at is None: self.shutdown_at = now + 20; self.events.append(('shutdown', p))
        elif p <= self.CRITICAL and 'crit' not in self.warned: self.warned.add('crit'); self.events.append(('critical', p))
        elif p <= self.LOW and 'low' not in self.warned: self.warned.add('low'); self.events.append(('low', p))
    def pop_events(self): e, self.events = self.events, []; return e
    def shutdown_due(self): return self.shutdown_at is not None and time.time() >= self.shutdown_at and not self.online

    def snapshot(self):
        return {'pct': int(round(self.pct)), 'v': self.v, 'status': self.status, 'charging': self.charging, 'online': self.online, 'temp': self.temp, 'limit': self.limit}
    def save(self, now):
        self.last_save = now
        try: json.dump({'pct': self.pct, 'limit': self.limit, 't': now}, open(self.state_file, 'w'))
        except Exception: pass
    def log(self, now):
        self.last_log = now
        try:
            with open(self.log_file, 'a') as f: f.write('%d %.3f %.1f %s %s\n' % (now, self.v, self.pct, self.status, 'usb' if self.online else 'bat'))
            if os.path.getsize(self.log_file) > 200000: os.replace(self.log_file, self.log_file + '.1')
        except Exception: pass

    # ---- power saving helpers
    def charge_led(self, on):
        wr('/sys/class/leds/red/brightness', 40 if on else 0)
    def governor(self, name):
        for p in ('/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor',):
            avail = rd(os.path.dirname(p) + '/scaling_available_governors')
            if name in avail.split(): wr(p, name)
