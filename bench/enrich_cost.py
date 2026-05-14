"""Enrich the cached per-task records with cost/step/duration data
from /api/getRunResults?format=only_judge (which carries the usage
stringified in each task entry).

Cache files are keyed by taskResultId — we just merge in `usage_str`,
`steps`, `taskDuration` from the judge endpoint."""
import json
import sys
import urllib.request
import ssl
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

RUNS = [
    ("v0.7.3-50",    "kh72n8mx9pmw6drn580qceshyh85w1yk"),
    ("v0.7.3-100",   "kh7c0c8k9njs41z49zjh3bhmtx85xj8y"),
    ("v0.8.2-100",   "kh7ab8rbccp6p3aqkbmdbqzfsd85webt"),
    ("v0.8.4-100",   "kh7cezjde2zwpdsh0ykrk1gk4n85x0e0"),
    ("v0.8.7-100",   "kh745113qw09x90zc4m25n00ds85x3j0"),
    ("upstream-bu",  "kh747qm0255wtfn44dpeyb00b585rha1"),
]

for label, run_id in RUNS:
    cache_path = CACHE / f"{label}.json"
    if not cache_path.exists():
        print(f"  skip {label} (no cache)"); continue
    run = json.loads(cache_path.read_text())

    print(f"  enriching {label}...", flush=True)
    req = urllib.request.Request(
        f"{URL}/api/getRunResults?runId={run_id}&format=only_judge",
        headers=HDR,
        method="GET",
    )
    with urllib.request.urlopen(req, context=CTX, timeout=120) as r:
        judge_resp = json.loads(r.read())
    tasks_by_text = {t['task']: t for t in judge_resp.get('judgeData', {}).get('tasks', [])}

    enriched = 0
    for d in run['per_task']:
        # match by task text since judge endpoint doesn't include taskResultId
        m = tasks_by_text.get(d['task']) or tasks_by_text.get(d.get('task','')[:300])
        if not m:
            # try prefix match
            for k, v in tasks_by_text.items():
                if k.startswith(d['task'][:80]):
                    m = v
                    break
        if m:
            d['usage_str'] = m.get('usage', '')
            d['steps'] = m.get('steps')
            d['taskDuration'] = m.get('taskDuration')
            d['tokensUsed'] = m.get('tokensUsed')
            enriched += 1

    print(f"    enriched {enriched}/{len(run['per_task'])}")
    cache_path.write_text(json.dumps(run, default=str))

print("done")
