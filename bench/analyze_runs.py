"""Pull metadata + per-task traces for every known run, then summarize.

Usage:
    EVALUATION_TOOL_URL=... EVALUATION_TOOL_SECRET_KEY=... python3 bench/analyze_runs.py
"""
import json
import statistics
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
CACHE.mkdir(exist_ok=True, parents=True)

# (label, run_id)
RUNS = [
    ("v0.7.3-50",    "kh72n8mx9pmw6drn580qceshyh85w1yk"),
    ("v0.7.3-100",   "kh7c0c8k9njs41z49zjh3bhmtx85xj8y"),
    ("v0.8.2-100",   "kh7ab8rbccp6p3aqkbmdbqzfsd85webt"),
    ("v0.8.4-100",   "kh7cezjde2zwpdsh0ykrk1gk4n85x0e0"),
    ("v0.8.7-100",   "kh745113qw09x90zc4m25n00ds85x3j0"),
    ("upstream-bu",  "kh747qm0255wtfn44dpeyb00b585rha1"),
]

PROMPT_METADATA_KEYS = (
    "prompt_agent_history_bytes",
    "prompt_read_state_bytes",
    "prompt_history_items",
    "prompt_history_collapsed_items",
    "prompt_n_messages",
)

def http_get(path):
    req = urllib.request.Request(f"{URL}{path}", headers=HDR, method="GET")
    with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
        return json.loads(r.read())

def http_post(path, payload):
    req = urllib.request.Request(
        f"{URL}{path}",
        data=json.dumps(payload).encode(),
        headers=HDR,
        method="POST",
    )
    with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
        return json.loads(r.read())

def fetch_run(label, run_id):
    """Fetch metadata + per-task summaries (lightweight). Cached on disk."""
    cache_path = CACHE / f"{label.replace('/', '_')}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    print(f"  fetching {label} ({run_id})...", flush=True)
    metadata = http_get(f"/api/getRun?runId={run_id}")
    traces = http_post("/api/getRunTracesForJudging", {"runId": run_id})
    print(f"    metadata + {len(traces)} traces ok, fetching per-task...", flush=True)

    def _fetch_task(t):
        try:
            d = http_post("/api/getTaskDetailsForJudging", {"taskResultId": t['taskResultId']})
        except Exception as e:
            d = {"_error": str(e)}
        # Keep ONLY the small fields we need for analysis.
        return {
            'taskResultId': t['taskResultId'],
            'task': t.get('task', '')[:300],
            'finalAnswer': (t.get('finalResultResponse', '') or '')[:500],
            'steps': t.get('steps'),
            'taskDuration': t.get('taskDuration'),
            'usage_str': t.get('usage', ''),
            'selfReportSuccess': d.get('selfReportSuccess'),
            'prompt_metrics': prompt_metric_rollup(d),
            '_error': d.get('_error'),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        per_task = list(ex.map(_fetch_task, traces))

    out = {"label": label, "run_id": run_id, "metadata": metadata, "per_task": per_task}
    cache_path.write_text(json.dumps(out, default=str))
    print(f"    {label}: cached {len(per_task)} task summaries", flush=True)
    return out

def parse_usage(u_str):
    if not u_str: return {}
    try:
        return json.loads(u_str) if isinstance(u_str, str) else u_str
    except Exception:
        return {}

def prompt_metric_rollup(detail):
    """Small per-task rollup from Agent StepMetadata prompt counters."""
    rows = []
    for step in detail.get("completeHistory") or []:
        md = step.get("metadata") or {}
        if isinstance(md, dict):
            rows.append(md)
    out = {}
    for key in PROMPT_METADATA_KEYS:
        vals = [
            md[key]
            for md in rows
            if isinstance(md.get(key), (int, float))
        ]
        if vals:
            out[f"{key}_mean"] = round(statistics.mean(vals), 2)
            out[f"{key}_max"] = max(vals)
    return out

def stats(xs, name=""):
    if not xs: return None
    return {
        'mean': round(statistics.mean(xs), 4),
        'median': round(statistics.median(xs), 4),
        'p90': round(sorted(xs)[int(len(xs)*0.9)], 4) if len(xs) >= 10 else None,
        'max': round(max(xs), 4),
        'sum': round(sum(xs), 4),
    }

def summarize(run):
    md = run['metadata']
    pt = run['per_task']
    n = len(pt)

    judge_passed = md.get('successfulTasks')
    self_pass = sum(1 for d in pt if d.get('selfReportSuccess') is True)
    self_fail = sum(1 for d in pt if d.get('selfReportSuccess') is False)
    self_none = sum(1 for d in pt if d.get('selfReportSuccess') is None)

    costs = []
    in_toks = []
    out_toks = []
    cache_reads = []
    steps = []
    durations = []
    prompt_agent_history_mean = []
    prompt_agent_history_max = []
    prompt_read_state_mean = []
    prompt_read_state_max = []
    prompt_history_items_mean = []
    prompt_history_collapsed_items_mean = []
    for d in pt:
        u = parse_usage(d.get('usage_str'))
        if u.get('total_cost') is not None: costs.append(u['total_cost'])
        if u.get('input') is not None: in_toks.append(u['input'])
        if u.get('output') is not None: out_toks.append(u['output'])
        if u.get('cache_read') is not None: cache_reads.append(u['cache_read'])
        if d.get('steps') is not None: steps.append(d['steps'])
        if d.get('taskDuration') is not None: durations.append(d['taskDuration'])
        pm = d.get('prompt_metrics') or {}
        if pm.get('prompt_agent_history_bytes_mean') is not None:
            prompt_agent_history_mean.append(pm['prompt_agent_history_bytes_mean'])
        if pm.get('prompt_agent_history_bytes_max') is not None:
            prompt_agent_history_max.append(pm['prompt_agent_history_bytes_max'])
        if pm.get('prompt_read_state_bytes_mean') is not None:
            prompt_read_state_mean.append(pm['prompt_read_state_bytes_mean'])
        if pm.get('prompt_read_state_bytes_max') is not None:
            prompt_read_state_max.append(pm['prompt_read_state_bytes_max'])
        if pm.get('prompt_history_items_mean') is not None:
            prompt_history_items_mean.append(pm['prompt_history_items_mean'])
        if pm.get('prompt_history_collapsed_items_mean') is not None:
            prompt_history_collapsed_items_mean.append(
                pm['prompt_history_collapsed_items_mean']
            )

    return {
        'label': run['label'],
        'judge_pct': round((judge_passed / n) * 100, 2) if judge_passed and n else None,
        'self_report_pct': round((self_pass / n) * 100, 2) if n else None,
        'cal_gap_pp': (
            round((self_pass / n - judge_passed / n) * 100, 2)
            if judge_passed and n else None
        ),
        'self_pass': self_pass,
        'self_fail_or_none': self_fail + self_none,
        'judge_passed': judge_passed,
        'access_denied': md.get('accessDeniedCount'),
        'action_errors': md.get('actionErrorCount'),
        'total_steps': md.get('stepsTaken'),
        'total_time_min': round(md.get('timeElapsed', 0) / 60, 1) if md.get('timeElapsed') else None,
        'cost': stats(costs),
        'in_toks': stats(in_toks),
        'out_toks': stats(out_toks),
        'cache_read': stats(cache_reads),
        'steps_per_task': stats(steps),
        'duration_s': stats(durations),
        'prompt_agent_history_bytes_mean': stats(prompt_agent_history_mean),
        'prompt_agent_history_bytes_max': stats(prompt_agent_history_max),
        'prompt_read_state_bytes_mean': stats(prompt_read_state_mean),
        'prompt_read_state_bytes_max': stats(prompt_read_state_max),
        'prompt_history_items_mean': stats(prompt_history_items_mean),
        'prompt_history_collapsed_items_mean': stats(prompt_history_collapsed_items_mean),
    }

def main():
    summaries = []
    for label, run_id in RUNS:
        try:
            run = fetch_run(label, run_id)
            summaries.append(summarize(run))
        except Exception as e:
            print(f"  !! {label} failed: {type(e).__name__}: {e}", flush=True)

    Path('bench/.trace_cache/all_summaries.json').write_text(
        json.dumps(summaries, default=str, indent=2)
    )

    # Compact comparison table
    print()
    print("=" * 130)
    hdr = f"{'label':14} {'judge':>7} {'self':>7} {'gap':>7} {'cost_avg':>10} {'cost_p90':>10} {'steps_avg':>10} {'dur_avg':>9} {'access_d':>9} {'act_err':>8}"
    print(hdr)
    print("=" * 130)
    for s in summaries:
        print(
            f"{s['label']:14} "
            f"{(s['judge_pct'] or 0):>6.2f}% "
            f"{(s['self_report_pct'] or 0):>6.2f}% "
            f"{(s['cal_gap_pp'] or 0):>6.2f}pp "
            f"${(s['cost']['mean'] if s['cost'] else 0):>8.4f}  "
            f"${(s['cost']['p90'] if s['cost'] and s['cost']['p90'] else 0):>8.4f}  "
            f"{(s['steps_per_task']['mean'] if s['steps_per_task'] else 0):>9.1f}  "
            f"{(s['duration_s']['mean'] if s['duration_s'] else 0):>7.1f}s  "
            f"{(s['access_denied'] or 0):>8}  "
            f"{(s['action_errors'] or 0):>7}"
        )

    # Detailed cost / step breakdown
    print()
    print("=" * 70)
    print("Token / cost detail (per task)")
    print("=" * 70)
    print(f"{'label':14} {'in_avg':>9} {'out_avg':>8} {'cache_avg':>10} {'in_p90':>9}")
    for s in summaries:
        print(
            f"{s['label']:14} "
            f"{(s['in_toks']['mean'] if s['in_toks'] else 0):>9.0f}  "
            f"{(s['out_toks']['mean'] if s['out_toks'] else 0):>7.0f}  "
            f"{(s['cache_read']['mean'] if s['cache_read'] else 0):>9.0f}  "
            f"{(s['in_toks']['p90'] if s['in_toks'] and s['in_toks']['p90'] else 0):>8.0f}"
        )

    if any(s.get('prompt_agent_history_bytes_mean') for s in summaries):
        print()
        print("=" * 88)
        print("Prompt section detail (per task; requires v0.12.5+ metadata)")
        print("=" * 88)
        print(
            f"{'label':14} {'hist_avg':>10} {'hist_max':>10} "
            f"{'read_avg':>10} {'read_max':>10} {'items':>8} {'collapsed':>10}"
        )
        for s in summaries:
            hist_mean = s.get('prompt_agent_history_bytes_mean')
            hist_max = s.get('prompt_agent_history_bytes_max')
            read_mean = s.get('prompt_read_state_bytes_mean')
            read_max = s.get('prompt_read_state_bytes_max')
            items = s.get('prompt_history_items_mean')
            collapsed = s.get('prompt_history_collapsed_items_mean')
            print(
                f"{s['label']:14} "
                f"{(hist_mean['mean'] if hist_mean else 0):>10.0f} "
                f"{(hist_max['mean'] if hist_max else 0):>10.0f} "
                f"{(read_mean['mean'] if read_mean else 0):>10.0f} "
                f"{(read_max['mean'] if read_max else 0):>10.0f} "
                f"{(items['mean'] if items else 0):>8.1f} "
                f"{(collapsed['mean'] if collapsed else 0):>10.1f}"
            )

if __name__ == '__main__':
    main()
