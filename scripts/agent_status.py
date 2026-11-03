#!/usr/bin/env python3
"""One JSON line: what the background builder is doing. Read by the Claude status mod."""
import json, os, re, subprocess, time
from datetime import datetime
LOG = os.path.expanduser("~/Library/Logs/gear-scout")
def sh(*a):
    try: return subprocess.run(a, capture_output=True, text=True, timeout=20, cwd="/Users/blakekaiser/Documents/gear-scout").stdout
    except Exception: return ""
runs = []
try:
    for line in open(f"{LOG}/accounts_runs.log"):
        m = re.match(r"(\S+ \S+) (\S+) (\S+) exit=(\d+) (\d+)s ?(.*)", line.strip())
        if m: runs.append(dict(at=m[1], job=m[2], acct=m[3], code=int(m[4]), secs=int(m[5]), note=m[6]))
except Exception: pass
ps = sh("ps", "-eo", "etime,command")
running = []
for l in ps.splitlines():
    m = re.search(r"claude_job\.sh (\S+) (\d+)", l)
    if m and "bash scripts" in l and m[1] not in [r["job"] for r in running]:
        running.append(dict(job=m[1], elapsed=l.split()[0]))
# a job counts as running once; a job's wrapper shows twice
plan = []
done = {r["job"] for r in runs if r["code"] == 0 and r["at"][:10] == datetime.now().strftime("%Y-%m-%d")}
cur = {r["job"] for r in running}
try:
    for line in open(f"{LOG}/queue_plan.txt"):
        j, _, label = line.strip().partition("|")
        plan.append(dict(job=j, label=label, state="done" if j in done and j not in cur else "running" if j in cur else "waiting"))
except Exception: pass
props = sh("docker", "exec", "gear-scout-dashboard", "python", "-m", "scrapers.proposals", "list").splitlines()
counts = {}
for p in props:
    parts = p.split()
    if len(parts) > 1: counts[parts[1]] = counts.get(parts[1], 0) + 1
commits = [c for c in sh("git", "log", "--since=6 hours ago", "--format=%s", "-n", "8").splitlines()]
dock = sh("docker", "ps", "--format", "{{.Names}} {{.Status}}")
healthy = "gear-scout Up" in dock and "gear-scout-dashboard Up" in dock
limited = {}
for a in ("plan1", "plan2"):
    try:
        u = int(open(f"{LOG}/{a}_limited_until").read().strip())
        if u > time.time(): limited[a] = datetime.fromtimestamp(u).strftime("%-I:%M %p")
    except Exception: pass
n_done = sum(1 for p in plan if p["state"] == "done"); n = len(plan)
print(json.dumps(dict(plan=plan, done=n_done, total=n, running=[r["job"] for r in running], counts=counts,
                      commits=commits, healthy=healthy, limited=limited, recent=runs[-4:], at=datetime.now().strftime("%-I:%M %p"))))
