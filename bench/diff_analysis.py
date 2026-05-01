"""Deeper analysis of the 184 'both done, answers differ' cases.

Categorizes by:
  - blocked_we_pretend: ours claims done but answer admits inability/blocking
  - entity_mismatch: both cite specific entities but they differ
  - count_mismatch: both list N items, but Ns differ
  - wording_only: same numbers/entities, different sentences
  - cant_classify: needs human read
"""
import json
import re
from pathlib import Path

CACHE = Path('bench/.trace_cache')
ours = json.load(open(CACHE / "v0.8.7-100.json"))
theirs = json.load(open(CACHE / "upstream-bu.json"))

ours_by = {d['task']: d for d in ours['per_task']}
theirs_by = {d['task']: d for d in theirs['per_task']}

# Heuristic: if ours mentions blocking phrases, it's a hidden failure.
BLOCK_PHRASES = (
    "i am unable",
    "i was unable",
    "could not access",
    "could not retrieve",
    "blocked access",
    "403 forbidden",
    "forbidden",
    "sign-in",
    "sign in / register",
    "must log in",
    "captcha",
    "verification required",
    "robot detection",
    "bot protection",
    "access denied",
)

def has_blocking(s):
    s = (s or '').lower()
    return any(p in s for p in BLOCK_PHRASES)

# Extract numeric mentions
NUM_RE = re.compile(r'\b\d+(?:[.,]\d+)?(?:%|k|m|b)?\b', re.I)
# Extract proper-noun-ish tokens (capitalized words 4+ chars)
ENTITY_RE = re.compile(r'\b[A-Z][a-zA-Z]{3,}(?:\s+[A-Z][a-zA-Z]{3,}){0,2}\b')

def extract_signals(s):
    s = s or ''
    nums = set(re.findall(NUM_RE, s))
    # Filter out trivial nums
    nums = {n for n in nums if n.lower() not in ('0','1','2','3','100','1000')}
    ents = set(re.findall(ENTITY_RE, s))
    # Filter out generic/common
    ents = {e for e in ents if e.lower() not in (
        'the', 'a', 'an', 'and', 'or', 'but', 'for', 'with',
        'definition', 'definitions', 'usage', 'examples', 'example',
        'positive', 'negative', 'points', 'note', 'notes',
    )}
    return nums, ents

shared_done_diff = []
for task, o in ours_by.items():
    t = theirs_by.get(task)
    if not t:
        for tk, tv in theirs_by.items():
            if tk.startswith(task[:80]):
                t = tv; break
    if not t: continue
    if not (o.get('selfReportSuccess') and t.get('selfReportSuccess')):
        continue
    oa = o.get('finalAnswer','') or ''
    ta = t.get('finalAnswer','') or ''
    if oa.strip().lower() == ta.strip().lower(): continue
    shared_done_diff.append((task, oa, ta))

print(f"Both-done-diff cases: {len(shared_done_diff)}")

# Category counts
cats = {
    'we_pretend_blocked':   [],   # we say done but text indicates inability
    'they_pretend_blocked': [],   # rare — they say done but text indicates inability
    'both_blocked':          [],
    'entity_mismatch':       [],   # capitalized entities differ a lot
    'number_mismatch':       [],   # numbers differ a lot
    'wording_only':          [],   # numbers + entities mostly overlap
    'short_ours':            [],   # ours much shorter than theirs (truncated?)
    'short_theirs':          [],
    'other':                 [],
}

for task, oa, ta in shared_done_diff:
    ob = has_blocking(oa)
    tb = has_blocking(ta)
    if ob and tb: cats['both_blocked'].append((task, oa, ta))
    elif ob: cats['we_pretend_blocked'].append((task, oa, ta))
    elif tb: cats['they_pretend_blocked'].append((task, oa, ta))
    else:
        ol, tl = len(oa), len(ta)
        if ol < tl * 0.4: cats['short_ours'].append((task, oa, ta))
        elif tl < ol * 0.4: cats['short_theirs'].append((task, oa, ta))
        else:
            o_nums, o_ents = extract_signals(oa)
            t_nums, t_ents = extract_signals(ta)
            num_overlap = (len(o_nums & t_nums) / max(1, len(o_nums | t_nums))) if (o_nums or t_nums) else 1.0
            ent_overlap = (len(o_ents & t_ents) / max(1, len(o_ents | t_ents))) if (o_ents or t_ents) else 1.0
            # Heuristic threshold
            if num_overlap < 0.3 and (o_nums or t_nums):
                cats['number_mismatch'].append((task, oa, ta, num_overlap))
            elif ent_overlap < 0.3 and (o_ents or t_ents):
                cats['entity_mismatch'].append((task, oa, ta, ent_overlap))
            else:
                cats['wording_only'].append((task, oa, ta))

print()
print("CATEGORY COUNTS")
print("=" * 60)
for k, v in cats.items():
    print(f"  {k:24} {len(v)}")
print()

def show(label, items, n=5):
    print()
    print("=" * 80)
    print(f"{label}  ({len(items)} total, showing {min(n,len(items))})")
    print("=" * 80)
    for it in items[:n]:
        task = it[0]; oa = it[1]; ta = it[2]
        print()
        print(f"TASK: {task[:200]}")
        print(f"  OURS  : {(oa or '')[:280]}")
        print(f"  THEIRS: {(ta or '')[:280]}")

show("WE PRETEND DONE ON BLOCKED — easy calibration win", cats['we_pretend_blocked'], n=8)
show("ENTITY MISMATCH — likely wrong-entity selected", cats['entity_mismatch'], n=5)
show("NUMBER MISMATCH — different facts extracted", cats['number_mismatch'], n=5)
show("OURS MUCH SHORTER — possibly under-extracted", cats['short_ours'], n=4)
show("THEIRS MUCH SHORTER — possibly we over-elaborate", cats['short_theirs'], n=4)
show("WORDING ONLY — same facts, different phrasing", cats['wording_only'], n=3)
