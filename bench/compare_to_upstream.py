"""Side-by-side per-task comparison: ours (v0.8.7 or v0.8.8) vs upstream.

Match tasks by literal task text. Bucket into:
  - both_done_same_answer  (likely both pass judge)
  - both_done_diff_answer  (one of us wrong — interesting)
  - we_done_they_failed    (we claim, they admit failure — possible our wins)
  - they_done_we_failed    (they claim, we admit failure — losses for us)
  - both_failed            (hard tasks)

Then dump the most informative divergences.
"""
import json
import os
import sys
from pathlib import Path

CACHE = Path('bench/.trace_cache')

if len(sys.argv) > 1 and sys.argv[1] == 'v0.8.8':
    OURS_LABEL = 'v0.8.8-100'
else:
    OURS_LABEL = 'v0.8.7-100'

THEIRS_LABEL = 'upstream-bu'

ours = json.load(open(CACHE / f"{OURS_LABEL}.json"))
theirs = json.load(open(CACHE / f"{THEIRS_LABEL}.json"))

ours_by = {d['task']: d for d in ours['per_task']}
theirs_by = {d['task']: d for d in theirs['per_task']}

# Find shared task set; tasks may have minor text diffs across runs
shared = []
ours_only_tasks = set(ours_by.keys()) - set(theirs_by.keys())
theirs_only_tasks = set(theirs_by.keys()) - set(ours_by.keys())
for k, o in ours_by.items():
    t = theirs_by.get(k)
    if not t:
        # try prefix
        for tk, tv in theirs_by.items():
            if tk.startswith(k[:80]):
                t = tv; break
    if t:
        shared.append((k, o, t))

print(f"comparing {OURS_LABEL} vs {THEIRS_LABEL}")
print(f"  total ours: {len(ours_by)}, total theirs: {len(theirs_by)}")
print(f"  matched: {len(shared)}")

# Bucket
buckets = {
    'both_done_same': [],
    'both_done_diff': [],
    'we_done_they_failed': [],
    'they_done_we_failed': [],
    'both_failed': [],
}

def norm_answer(s):
    if not s: return ''
    return ' '.join(s.lower().split())[:300]

for task, o, t in shared:
    osr = o.get('selfReportSuccess')
    tsr = t.get('selfReportSuccess')
    oa = o.get('finalAnswer','') or ''
    ta = t.get('finalAnswer','') or ''
    if osr and tsr:
        if norm_answer(oa) == norm_answer(ta):
            buckets['both_done_same'].append((task, o, t))
        else:
            buckets['both_done_diff'].append((task, o, t))
    elif osr and not tsr:
        buckets['we_done_they_failed'].append((task, o, t))
    elif not osr and tsr:
        buckets['they_done_we_failed'].append((task, o, t))
    else:
        buckets['both_failed'].append((task, o, t))

print()
print("=" * 60)
print("BUCKETS")
print("=" * 60)
for name, items in buckets.items():
    print(f"  {name:24} {len(items)}")

# Dump samples
def show_sample(label, items, n=10):
    print()
    print("=" * 80)
    print(f"{label} (showing {min(n,len(items))} of {len(items)})")
    print("=" * 80)
    for task, o, t in items[:n]:
        print()
        print(f"TASK: {task[:200]}")
        print(f"  ours [self={o.get('selfReportSuccess')}]: {(o.get('finalAnswer','') or '')[:300]}")
        print(f"  thrs [self={t.get('selfReportSuccess')}]: {(t.get('finalAnswer','') or '')[:300]}")

show_sample("THEY DONE / WE FAILED — losses we should learn from", buckets['they_done_we_failed'], n=10)
show_sample("BOTH DONE BUT ANSWERS DIFFER — likely judge divergence cases", buckets['both_done_diff'], n=10)
show_sample("WE DONE / THEY FAILED — wins where we may be ahead", buckets['we_done_they_failed'], n=5)
