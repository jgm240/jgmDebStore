"""jgmDeb power saving (own work).

Stages after the screen goes off (each only when the power saver mode allows it):
  1 (after ~8 s)   CPU: low governor, top frequency capped, extra cores offline; deep CPU idle allowed (lpm_levels sleep_disabled=N)
  2 (after ~60 s)  WiFi: powered down (restored when the screen comes on), unless an ssh/adb session is open or the charger is connected
  3 (deep mode)    system suspend (echo mem > /sys/power/state) with a safety wake-up timer (RTC alarm), so the phone can never sleep forever; the next jgmDeb alarm is
                   scheduled as the wake-up. Suspend is skipped during calls, downloads, music, ssh/adb sessions, on the charger, and (by default) while the modem is running,
                   because the modem's incoming-call wake-up has not been proven.
Everything is logged to /data/jgm/power.log. Create /data/jgm/no-powersave to disable all of it.  Modes: 0 off, 1 light (stage 1), 2 standard (+2), 3 deep (+3).
"""
import os, time, subprocess, glob

LOG = '/data/jgm/power.log'; KILL = '/data/jgm/no-powersave'
CPU = '/sys/devices/system/cpu'
def log(m):
    try:
        with open(LOG, 'a') as f: f.write(time.strftime('%m-%d %H:%M:%S ') + m + '\n')
        if os.path.getsize(LOG) > 60000: os.replace(LOG, LOG + '.1')
    except Exception: pass
def rd(p, d=''):
    try: return open(p).read().strip()
    except Exception: return d
def wr(p, v):
    try: open(p, 'w').write(str(v)); return True
    except Exception: return False
def sh(c, t=15):
    try: return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=t).stdout.strip()
    except Exception as e: return str(e)

class Power:
    def __init__(self): self.stage = 0; self.saved = None; self.wifi_was_up = False; self.last_suspend = None; self.suspends = 0; self.suspend_total = 0.0
    def enabled(self): return not os.path.exists(KILL)
    # ---- stage 1: CPU + idle
    def cpu_low(self):
        online = [int(p.split('cpu')[-1]) for p in glob.glob(CPU + '/cpu[0-9]*') if rd(p + '/online', '1') == '1']
        gov = rd(CPU + '/cpu0/cpufreq/scaling_governor'); fmax = rd(CPU + '/cpu0/cpufreq/scaling_max_freq')
        self.saved = {'gov': gov, 'fmax': fmax, 'online': online}
        avail = rd(CPU + '/cpu0/cpufreq/scaling_available_governors').split()
        for g in ('powersave', 'conservative', 'ondemand'):
            if g in avail: wr(CPU + '/cpu0/cpufreq/scaling_governor', g); break
        freqs = sorted(int(x) for x in rd(CPU + '/cpu0/cpufreq/scaling_available_frequencies').split() if x.isdigit())
        if len(freqs) > 2: wr(CPU + '/cpu0/cpufreq/scaling_max_freq', freqs[1])
        for c in online:
            if c != 0: wr(CPU + '/cpu%d/online' % c, 0)
        wr('/sys/module/lpm_levels/parameters/sleep_disabled', 'N')
        log('stage1: cpu low (gov %s->%s, max %s, cores %s->0), deep idle allowed' % (gov, rd(CPU + '/cpu0/cpufreq/scaling_governor'), rd(CPU + '/cpu0/cpufreq/scaling_max_freq'), online))
    def cpu_normal(self):
        s = self.saved
        if not s: return
        for c in s['online']:
            if c != 0: wr(CPU + '/cpu%d/online' % c, 1)
        if s['gov']: wr(CPU + '/cpu0/cpufreq/scaling_governor', s['gov'])
        if s['fmax']: wr(CPU + '/cpu0/cpufreq/scaling_max_freq', s['fmax'])
        self.saved = None; log('cpu restored')
    # ---- stage 2: WiFi
    def wifi_up_now(self): return os.path.exists('/sys/class/net/wlan0') and rd('/sys/class/net/wlan0/operstate') not in ('down',)
    def ssh_active(self):
        """an established TCP connection on port 22 (ssh) or 5555 (adb over WiFi)"""
        for l in rd('/proc/net/tcp').splitlines()[1:]:
            p = l.split()
            if len(p) > 3 and p[3] == '01' and p[1].split(':')[-1] in ('0016', '15B3'): return True
        return False
    def wifi_down(self):
        self.wifi_was_up = self.wifi_up_now()
        if self.wifi_was_up: sh('sh /opt/jgm/wifi-connect.sh stop'); log('stage2: wifi down')
    def wifi_up(self):
        if self.wifi_was_up:
            subprocess.Popen('sh /opt/jgm/wifi-connect.sh', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); log('wifi up'); self.wifi_was_up = False
    # ---- stage 3: suspend
    def can_suspend(self, charging, modem_up, allow_modem, busy):
        if charging or busy or self.ssh_active() or (modem_up and not allow_modem): return False
        return os.path.exists('/sys/power/state') and 'mem' in rd('/sys/power/state')
    def suspend_once(self, seconds_to_alarm=None, safety=900):
        """suspend until a wake source fires or the safety timer expires -> seconds spent asleep"""
        wake = safety if seconds_to_alarm is None else max(30, min(safety, int(seconds_to_alarm)))
        wr('/sys/class/rtc/rtc0/wakealarm', 0); ok = wr('/sys/class/rtc/rtc0/wakealarm', '+%d' % wake)
        if not ok: log('no RTC wake alarm: not suspending'); return 0.0
        t0 = time.time(); m0 = float(rd('/proc/uptime', '0 0').split()[0]); sh('sync', 5)
        wr('/sys/power/state', 'mem')                       # blocks until woken
        dt = time.time() - t0; self.last_suspend = dt; self.suspends += 1; self.suspend_total += dt
        log('suspend: %.1f s (alarm in %d s)' % (dt, wake)); return dt
    # ---- driver for the main loop
    def screen_off_tick(self, el, mode, charging):
        if not self.enabled() or mode == 0: return
        if mode >= 1 and self.stage < 1 and el > 8: self.cpu_low(); self.stage = 1
        if mode >= 2 and self.stage < 2 and el > 60 and not charging and not self.ssh_active(): self.wifi_down(); self.stage = 2
    def screen_on(self):
        if self.stage >= 1: self.cpu_normal()
        if self.stage >= 2: self.wifi_up()
        if self.stage: log('screen on: restored (stage %d)' % self.stage)
        self.stage = 0
    def drain_per_hour(self, path='/data/jgm/battery.log'):
        """average % per hour over the latest stretch on battery, from the battery log; None if not enough data"""
        pts = []
        try:
            for l in open(path).read().splitlines()[-400:]:
                p = l.split()
                if len(p) >= 5 and p[4] == 'bat': pts.append((int(float(p[0])), float(p[2])))
        except Exception: return None
        if len(pts) < 3: return None
        t0, p0 = pts[0]; t1, p1 = pts[-1]
        return None if t1 - t0 < 900 else max(0.0, (p0 - p1) / ((t1 - t0) / 3600.0))
