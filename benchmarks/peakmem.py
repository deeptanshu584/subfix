"""Run a command and report wall time + peak resident memory of it and its children (psutil)."""

import subprocess
import sys
import time

import psutil

proc = subprocess.Popen(sys.argv[1:])
p = psutil.Process(proc.pid)
peak = 0
t0 = time.perf_counter()
while proc.poll() is None:
    try:
        rss = p.memory_info().rss + sum(c.memory_info().rss for c in p.children(recursive=True))
        peak = max(peak, rss)
    except psutil.Error:
        pass
    time.sleep(0.2)
mib = peak / 2**20
print(f"PEAKMEM wall={time.perf_counter() - t0:.1f}s peak_rss={mib:.0f}MiB rc={proc.returncode}")
