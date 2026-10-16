#!/usr/bin/env python3
"""Lightweight resource logger for the Jetson (stdlib only, reads /proc and /sys).

Record (runs on the host, sees docker processes too):
    python3 jetson_sysmon.py record -i 5 -o ~/sysmon
    nohup python3 jetson_sysmon.py record -i 5 -o ~/sysmon >/dev/null 2>&1 &

Report (on the Jetson or after copying the directory to a laptop):
    python3 jetson_sysmon.py report ~/sysmon
    python3 jetson_sysmon.py report ~/sysmon --last 30m

Files:
    system.csv  one row per sample: CPU/RAM/swap/GPU/load/temps/power/net
    procs.csv   per sample: top-K by CPU, top-K by RSS and anything >= --min-cpu
Process CPU% is like top: 100 = one full core.
"""
import argparse
import csv
import glob
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime

CLK_TCK = os.sysconf('SC_CLK_TCK')
PAGE_MB = os.sysconf('SC_PAGE_SIZE') / 1048576
NCPU = os.cpu_count() or 1

GPU_LOAD_GLOBS = [
    '/sys/devices/platform/bus@0/*.gpu/load',   # Orin (JetPack 6)
    '/sys/devices/platform/*.gpu/load',
    '/sys/devices/gpu.0/load',                   # older JetPacks
]
INTERPRETERS = {'python', 'python3', 'python3.10', 'python3.8', 'bash', 'sh',
                'node', 'java', 'ruby', 'perl'}


def read(path, default=''):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def read_int(path):
    try:
        return int(read(path).strip())
    except ValueError:
        return None


# ---------------------------------------------------------------- system ---

def cpu_times():
    """{'cpu': (busy, total), 'cpu0': ...} in jiffies."""
    out = {}
    for line in read('/proc/stat').splitlines():
        if not line.startswith('cpu'):
            break
        name, *vals = line.split()
        vals = [int(v) for v in vals[:8]]
        idle = vals[3] + vals[4]  # idle + iowait
        out[name] = (sum(vals) - idle, sum(vals), vals[4])
    return out


def meminfo():
    m = {}
    for line in read('/proc/meminfo').splitlines():
        k, v = line.split(':', 1)
        m[k] = int(v.split()[0])  # kB
    return m


def net_bytes():
    rx = tx = 0
    for line in read('/proc/net/dev').splitlines()[2:]:
        iface, data = line.split(':', 1)
        iface = iface.strip()
        if iface == 'lo' or iface.startswith(('docker', 'veth', 'br-')):
            continue
        f = data.split()
        rx += int(f[0])
        tx += int(f[8])
    return rx, tx


def find_gpu_load():
    for pattern in GPU_LOAD_GLOBS:
        for p in glob.glob(pattern):
            if read_int(p) is not None:
                return p
    return None


def thermal_zones():
    zones = []
    for z in sorted(glob.glob('/sys/class/thermal/thermal_zone*')):
        t = read(z + '/type').strip()
        if t and read_int(z + '/temp') is not None:
            zones.append((t.replace('-thermal', ''), z + '/temp'))
    return zones


def power_rails():
    """INA3221 rails on Jetson dev kits: [(label, volt_path, curr_path)]."""
    rails = []
    for hw in glob.glob('/sys/bus/i2c/drivers/ina3221/*/hwmon/hwmon*'):
        for lbl in sorted(glob.glob(hw + '/in*_label')):
            idx = lbl.split('/in')[-1].split('_')[0]
            name = read(lbl).strip()
            volt, curr = f'{hw}/in{idx}_input', f'{hw}/curr{idx}_input'
            if name and not name.startswith('sum') and os.path.exists(curr):
                rails.append((name, volt, curr))
    return rails


# ------------------------------------------------------------- processes ---

def proc_name(pid, comm):
    """comm, but more useful for interpreters and ROS nodes."""
    args = read(f'/proc/{pid}/cmdline').split('\0')
    for a in args:
        if a.startswith('__node:='):
            return f'{comm}[{a[8:]}]'
    base = os.path.basename(args[0]) if args and args[0] else comm
    if base in INTERPRETERS or comm in INTERPRETERS:
        for a in args[1:]:
            if a and not a.startswith('-'):
                return f'{base}:{os.path.basename(a)}'
    return comm


def proc_snapshot():
    """{pid: (name, cpu_jiffies, rss_mb)}"""
    snap = {}
    for d in os.listdir('/proc'):
        if not d.isdigit():
            continue
        stat = read(f'/proc/{d}/stat')
        if not stat:
            continue
        # comm can contain spaces/parens -> split on the last ')'
        lp, rp = stat.find('('), stat.rfind(')')
        comm = stat[lp + 1:rp]
        f = stat[rp + 2:].split()
        jiff = int(f[11]) + int(f[12])  # utime + stime
        rss = int(f[21]) * PAGE_MB
        snap[int(d)] = (comm, jiff, rss)
    return snap


# ---------------------------------------------------------------- record ---

def record(args):
    os.makedirs(args.out, exist_ok=True)
    gpu = find_gpu_load()
    zones = thermal_zones()
    rails = power_rails()
    cores = sorted((k for k in cpu_times() if k != 'cpu'), key=lambda c: int(c[3:]))

    sys_cols = (['time', 'cpu_pct', 'iowait_pct', 'cpu_max_core_pct', 'load1',
                 'mem_used_mb', 'mem_pct', 'swap_used_mb', 'gpu_pct',
                 'net_rx_kbs', 'net_tx_kbs', 'nprocs']
                + [f'core{c[3:]}_pct' for c in cores]
                + [f't_{n}' for n, _ in zones]
                + [f'p_{n}_mw' for n, _, _ in rails])
    sys_path = os.path.join(args.out, 'system.csv')
    proc_path = os.path.join(args.out, 'procs.csv')
    sys_new = not os.path.exists(sys_path)
    if not sys_new:
        with open(sys_path) as f:
            if f.readline().strip().split(',') != sys_cols:
                sys.exit(f'{sys_path} has a different header; use another -o dir')
    proc_new = not os.path.exists(proc_path)
    fs = open(sys_path, 'a', newline='')
    fp = open(proc_path, 'a', newline='')
    ws, wp = csv.writer(fs), csv.writer(fp)
    if sys_new:
        ws.writerow(sys_cols)
    if proc_new:
        wp.writerow(['time', 'pid', 'name', 'cpu_pct', 'rss_mb'])

    print(f'sysmon: every {args.interval}s -> {args.out} '
          f'(gpu={"yes" if gpu else "no"}, {len(zones)} temps, {len(rails)} rails)',
          file=sys.stderr)

    prev_cpu, prev_net = cpu_times(), net_bytes()
    prev_procs, names = proc_snapshot(), {}
    prev_t = time.monotonic()
    try:
        while True:
            time.sleep(args.interval)
            now = time.monotonic()
            dt = now - prev_t
            ts = datetime.now().isoformat(timespec='seconds')

            cur_cpu = cpu_times()

            def pct(k):
                b0, t0, w0 = prev_cpu[k]
                b1, t1, w1 = cur_cpu.get(k, prev_cpu[k])
                dtot = (t1 - t0) or 1
                return 100 * (b1 - b0) / dtot, 100 * (w1 - w0) / dtot

            total, iowait = pct('cpu')
            per_core = [pct(c)[0] for c in cores]

            m = meminfo()
            mem_used = (m['MemTotal'] - m.get('MemAvailable', m['MemFree'])) / 1024
            swap_used = (m.get('SwapTotal', 0) - m.get('SwapFree', 0)) / 1024
            rx, tx = net_bytes()
            gpu_pct = read_int(gpu) / 10 if gpu else ''
            temps = [(read_int(p) or 0) / 1000 for _, p in zones]
            power = []
            for _, vp, cp in rails:
                v, c = read_int(vp), read_int(cp)
                power.append(round(v * c / 1000) if v is not None and c is not None else '')

            procs = proc_snapshot()
            rows = []
            for pid, (comm, jiff, rss) in procs.items():
                old = prev_procs.get(pid)
                cpu = 100 * (jiff - old[1]) / CLK_TCK / dt if old and old[0] == comm else 0.0
                rows.append((pid, comm, cpu, rss))

            ws.writerow([ts, f'{total:.1f}', f'{iowait:.1f}',
                         f'{max(per_core, default=0):.1f}',
                         read('/proc/loadavg').split()[0],
                         f'{mem_used:.0f}', f'{100 * mem_used * 1024 / m["MemTotal"]:.1f}',
                         f'{swap_used:.0f}', gpu_pct,
                         f'{(rx - prev_net[0]) / 1024 / dt:.1f}',
                         f'{(tx - prev_net[1]) / 1024 / dt:.1f}', len(procs)]
                        + [f'{c:.0f}' for c in per_core]
                        + [f'{t:.1f}' for t in temps] + power)

            by_cpu = sorted(rows, key=lambda r: -r[2])
            keep = {r[0] for r in by_cpu[:args.top]}
            keep |= {r[0] for r in sorted(rows, key=lambda r: -r[3])[:args.top]}
            keep |= {r[0] for r in rows if r[2] >= args.min_cpu}
            for pid, comm, cpu, rss in by_cpu:
                if pid in keep:
                    key = (pid, comm)
                    if key not in names:
                        names[key] = proc_name(pid, comm)
                    wp.writerow([ts, pid, names[key], f'{cpu:.1f}', f'{rss:.0f}'])
            names = {k: v for k, v in names.items() if k[0] in procs}

            for f in (fs, fp):
                f.flush()
                os.fsync(f.fileno())  # survive a power cut / hard hang
            prev_cpu, prev_net, prev_procs, prev_t = cur_cpu, (rx, tx), procs, now
    except KeyboardInterrupt:
        pass
    finally:
        fs.close()
        fp.close()


# ---------------------------------------------------------------- report ---

def parse_since(last):
    if not last:
        return None
    mult = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    secs = float(last[:-1]) * mult[last[-1]] if last[-1] in mult else float(last)
    return datetime.fromtimestamp(time.time() - secs).isoformat(timespec='seconds')


def pctl(vals, q):
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))]


def report(args):
    since = parse_since(args.last)
    with open(os.path.join(args.dir, 'system.csv')) as f:
        rows = [r for r in csv.DictReader(f) if not since or r['time'] >= since]
    if not rows:
        sys.exit('no samples in range')
    times = [r['time'] for r in rows]
    print(f'{len(rows)} samples, {times[0]} .. {times[-1]}\n')

    print(f'{"metric":<22}{"avg":>9}{"p50":>9}{"p95":>9}{"max":>9}{"min":>9}')
    for col in rows[0]:
        if col == 'time':
            continue
        vals = [float(r[col]) for r in rows if r[col] not in ('', None)]
        if not vals:
            continue
        print(f'{col:<22}{statistics.fmean(vals):>9.1f}{pctl(vals, .5):>9.1f}'
              f'{pctl(vals, .95):>9.1f}{max(vals):>9.1f}{min(vals):>9.1f}')

    n = len(rows)
    cpu_sum = defaultdict(float)     # summed over samples (missing sample = 0)
    cpu_max = defaultdict(float)
    rss_per_sample = defaultdict(lambda: defaultdict(float))
    seen = defaultdict(set)
    with open(os.path.join(args.dir, 'procs.csv')) as f:
        for r in csv.DictReader(f):
            if since and r['time'] < since:
                continue
            key = r['name'] if args.by_name else f'{r["name"]} ({r["pid"]})'
            cpu, rss = float(r['cpu_pct']), float(r['rss_mb'])
            cpu_sum[key] += cpu
            rss_per_sample[key][r['time']] += rss
            seen[key].add(r['time'])
            # per-sample CPU of all pids with this name
            cpu_max[(key, r['time'])] += cpu
    peak = defaultdict(float)
    for (key, _), v in cpu_max.items():
        peak[key] = max(peak[key], v)

    def table(title, order):
        print(f'\n{title}')
        print(f'{"process":<48}{"cpu avg":>8}{"cpu max":>8}{"rss avg":>8}'
              f'{"rss max":>8}{"seen":>6}')
        for key in order[:args.top]:
            rss = list(rss_per_sample[key].values())
            print(f'{key[:47]:<48}{cpu_sum[key] / n:>8.1f}{peak[key]:>8.1f}'
                  f'{statistics.fmean(rss):>8.0f}{max(rss):>8.0f}'
                  f'{100 * len(seen[key]) / n:>5.0f}%')

    keys = list(cpu_sum)
    table('Top by average CPU (% of one core; cpu avg counts unlogged samples as 0)',
          sorted(keys, key=lambda k: -cpu_sum[k]))
    table('Top by peak RSS (MB)',
          sorted(keys, key=lambda k: -max(rss_per_sample[k].values())))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('record', help='sample every N seconds into CSV files')
    r.add_argument('-i', '--interval', type=float, default=5.0)
    r.add_argument('-o', '--out', default=os.path.expanduser('~/sysmon'))
    r.add_argument('-k', '--top', type=int, default=10,
                   help='log top-K processes by CPU and top-K by RSS')
    r.add_argument('--min-cpu', type=float, default=1.0,
                   help='also log any process at or above this CPU%%')
    p = sub.add_parser('report', help='summarise a recorded directory')
    p.add_argument('dir', nargs='?', default=os.path.expanduser('~/sysmon'))
    p.add_argument('--last', help='only the last 30m / 2h / 1d ...')
    p.add_argument('-k', '--top', type=int, default=15)
    p.add_argument('--by-name', action='store_true',
                   help='merge processes with the same name (e.g. restarts, workers)')
    args = ap.parse_args()
    record(args) if args.cmd == 'record' else report(args)


if __name__ == '__main__':
    main()
