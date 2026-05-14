"""Pull completeHistory for v0.8.10 blowup tasks (vs v0.8.9 baseline) and
surface what the [INDEX_DEAD] nudge actually did. We want to see:
  - did the nudge fire?
  - did the LLM respond by re-orienting (good) or by abandoning a
    working flow (bad)?
  - is the regression from the nudge text or from something else?
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

def http_post(p, b):
    req = urllib.request.Request(
        f"{URL}{p}", data=json.dumps(b).encode(),
        headers=HDR, method="POST",
    )
    return json.loads(urllib.request.urlopen(req, context=CTX, timeout=120).read())

v9 = json.load(open(CACHE / "v0.8.9-100.json"))
v10 = json.load(open(CACHE / "v0.8.10-100.json"))

v9_by = {d['task']: d for d in v9['per_task']}
v10_by = {d['task']: d for d in v10['per_task']}

# Top 4 blowups (v0.8.10 burned >>v0.8.9 steps)
PICKS = [
    "Return the names of 4 people who work as analysts or associates",
    "Filter property listings in Los Angeles with a maximum rent of $3000",
    "Determine which movie release this weekend had the highest box office",
    "Navigate to Bulgaria and go to Visa section and list the required",
]

def summarize_step(step, idx):
    state = step.get('state') or {}
    url = (state.get('url') or '')[:60]
    mo = step.get('model_output') or {}
    actions = mo.get('action') or []
    act_summary = []
    for a in actions:
        if not isinstance(a, dict): continue
        for k, v in a.items():
            args = ''
            if isinstance(v, dict):
                parts = [f"{ak}={str(v[ak])[:30]}" for ak in ('index','url','text','query','value','amount','direction') if ak in v]
                args = ' '.join(parts)
            act_summary.append(f"{k}({args})")
    result = step.get('result') or []
    res_brief = []
    for r in result:
        if r is None: continue
        if r.get('error'):
            res_brief.append(f"ERR: {str(r['error'])[:80]}")
        elif r.get('is_done'):
            ec = (r.get('extracted_content') or '')[:60]
            res_brief.append(f"DONE(s={r.get('success')}): {ec}")
        elif r.get('extracted_content'):
            ec = str(r['extracted_content'])[:80]
            # Highlight the dead-index nudge marker if present
            if 'INDEX_DEAD' in ec or 'index_dead' in ec.lower():
                res_brief.append(f"[NUDGE] {ec}")
            elif 'index' in ec.lower() and ('not available' in ec.lower() or 'no longer present' in ec.lower() or 'not in the current' in ec.lower()):
                res_brief.append(f"[STALE] {ec}")
            else:
                res_brief.append(f"ok: {ec}")
    return f"  s{idx:02d} [{url}]  {' | '.join(act_summary)[:120]}  →  {' ; '.join(res_brief)}"

def find_user_messages_with_index_dead(history):
    """Look through the model_output for [INDEX_DEAD] nudges or
    UserMessage injections we made. They show as the next-turn input
    that the LLM would see, not as a step's own result. We can't
    directly see them, but we can see the LLM's behavior change after
    a stale-index step — proxied by sudden tool changes."""
    pass  # not directly observable in completeHistory

for prefix in PICKS:
    t9 = next((t for t in v9_by if t.startswith(prefix[:50])), None)
    if not t9: continue
    d9 = v9_by[t9]
    d10 = v10_by.get(t9)
    if not d10:
        for k, v in v10_by.items():
            if k.startswith(t9[:80]): d10 = v; break
    if not d10: continue

    print("=" * 100)
    print(f"TASK: {t9[:140]}")
    print(f"  v0.8.9:  {d9.get('steps','?')}st, self={d9.get('selfReportSuccess')}")
    print(f"  v0.8.10: {d10.get('steps','?')}st, self={d10.get('selfReportSuccess')}")
    print()

    print(f"--- v0.8.9 (worked) ---")
    h9 = http_post("/api/getTaskDetailsForJudging", {"taskResultId": d9['taskResultId']}).get('completeHistory') or []
    for i, s in enumerate(h9[:25]):
        print(summarize_step(s, i))
    if len(h9) > 25: print(f"  ... ({len(h9)-25} more steps)")

    print()
    print(f"--- v0.8.10 (blew up) ---")
    h10 = http_post("/api/getTaskDetailsForJudging", {"taskResultId": d10['taskResultId']}).get('completeHistory') or []
    # Show first 8 + last 6 to see start and end of the run
    for i, s in enumerate(h10[:8]):
        print(summarize_step(s, i))
    if len(h10) > 14:
        print(f"  ... ({len(h10) - 14} middle steps elided) ...")
        for i, s in enumerate(h10[-6:], start=len(h10)-6):
            print(summarize_step(s, i))
    elif len(h10) > 8:
        for i, s in enumerate(h10[8:], start=8):
            print(summarize_step(s, i))

    # Final answers
    print()
    print(f"v0.8.9  final answer: {d9.get('finalAnswer','')[:200]}")
    print(f"v0.8.10 final answer: {d10.get('finalAnswer','')[:200]}")
    print()
