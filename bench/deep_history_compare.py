"""Pull completeHistory for a hand-picked set of divergent tasks
(both ours v0.8.7 and upstream) and surface concrete behavioral
differences: tool sequences, navigation patterns, step counts,
how they handle the same page.

Strategy:
  1. Pick 8-12 tasks from entity_mismatch / number_mismatch buckets
     where ours and upstream went different ways
  2. For each, fetch completeHistory from both runs (the heavy field
     I stripped from earlier cache)
  3. Walk both step-by-step, summarizing tool calls + URLs visited
  4. Print side-by-side
"""
import json
import sys
import urllib.request
import ssl
import concurrent.futures
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from env_file import require_env

URL = require_env('EVALUATION_TOOL_URL').rstrip('/')
KEY = require_env('EVALUATION_TOOL_SECRET_KEY')
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
CTX = ssl.create_default_context()

CACHE = Path('bench/.trace_cache')

def http_post(path, payload):
    req = urllib.request.Request(
        f"{URL}{path}",
        data=json.dumps(payload).encode(),
        headers=HDR,
        method="POST",
    )
    with urllib.request.urlopen(req, context=CTX, timeout=120) as r:
        return json.loads(r.read())

ours = json.load(open(CACHE / "v0.8.7-100.json"))
theirs = json.load(open(CACHE / "upstream-bu.json"))

ours_by = {d['task']: d for d in ours['per_task']}
theirs_by = {d['task']: d for d in theirs['per_task']}

# Pick tasks that look like entity / number mismatches but both claimed done.
PICKS = [
    "Check the \"About NASA\" section for a list of collaborating institutions",
    "Browse the Opinion section and list three article titles",
    "Find the latest NBA game schedule for the Los Angeles Lakers",
    "Navigate to the Companies section and summarize the key points from the latest article about an Indian conglomerate",
    "Filter stories by the genre \"Teen Fiction\"",
    "Navigate to the Samsung Galaxy S23 product page",
    "Navigate to the CDC health topics page, search for \"flu prevention\"",
    "Find and list the steps required to apply for VA disability compensation",
]

picks = []
for prefix in PICKS:
    for k, o in ours_by.items():
        if k.startswith(prefix[:60]):
            t = theirs_by.get(k)
            if not t:
                for tk, tv in theirs_by.items():
                    if tk.startswith(k[:80]):
                        t = tv; break
            if t:
                picks.append((k, o, t))
            break

print(f"picked {len(picks)} tasks for deep-history comparison")

def fetch_full(task_result_id):
    return http_post("/api/getTaskDetailsForJudging", {"taskResultId": task_result_id})

def summarize_history(history):
    """Walk completeHistory, return per-step summary lines.

    Each step typically has:
      - state: { url, title, ... }
      - model_output: { action: [...] } or thinking text
      - result: [{ extracted_content, error, is_done, success, ... }]
    """
    if not history: return []
    out = []
    for i, step in enumerate(history):
        state = step.get('state') or {}
        url = (state.get('url') or '')[:80]
        mo = step.get('model_output') or {}
        actions = mo.get('action') or []
        # Extract action names + key args succinctly
        action_summary = []
        for a in actions:
            if isinstance(a, dict):
                for k, v in a.items():
                    args = ''
                    if isinstance(v, dict):
                        args_parts = []
                        for ak in ('index', 'url', 'text', 'query', 'amount', 'direction'):
                            if ak in v: args_parts.append(f"{ak}={str(v[ak])[:40]}")
                        args = ' '.join(args_parts)
                    action_summary.append(f"{k}({args})")
        result = step.get('result') or []
        # Truncate first non-error extracted_content for context
        result_brief = []
        for r in result:
            if r is None: continue
            if r.get('error'):
                result_brief.append(f"ERR: {str(r['error'])[:60]}")
            elif r.get('is_done'):
                ec = (r.get('extracted_content') or '')[:80]
                result_brief.append(f"DONE(success={r.get('success')}): {ec}")
            elif r.get('extracted_content'):
                ec = str(r['extracted_content'])[:60]
                result_brief.append(f"ok: {ec}")
        out.append(f"  s{i:02d} [{url}]  {' | '.join(action_summary)}  →  {' ; '.join(result_brief)}")
    return out

print()
for k, o, t in picks:
    print("=" * 100)
    print(f"TASK: {k[:200]}")
    print()

    o_full = fetch_full(o['taskResultId'])
    t_full = fetch_full(t['taskResultId'])

    o_hist = o_full.get('completeHistory') or []
    t_hist = t_full.get('completeHistory') or []

    print(f"OURS  ({len(o_hist)} steps):")
    for line in summarize_history(o_hist):
        print(line)

    print()
    print(f"THEIRS ({len(t_hist)} steps):")
    for line in summarize_history(t_hist):
        print(line)

    print()
    print(f"OURS final answer:   {(o.get('finalAnswer','') or '')[:200]}")
    print(f"THEIRS final answer: {(t.get('finalAnswer','') or '')[:200]}")
    print()
