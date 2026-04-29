# browser-use-rs

A lean Rust runtime for [browser-use](https://github.com/browser-use/browser-use), with Python bindings.

The browser, CDP, and DOM layers are written in Rust. The agent loop, LLM
providers, and tool registry stay in Python. The result is **3.4× faster
and 48% cheaper than Python `browser-use` on the same tasks with the same
LLM** — see [bench/](bench/) for the suite and the raw numbers.

## Why

The Python `browser-use` does many things well. It also accumulated bloat:
13 LLM provider adapters, a full CLI/daemon, two competing browser-action
APIs, sandbox layers, telemetry. The agent loop itself emits **JSON-mode
output** — every step writes `evaluation_previous_goal / memory /
next_goal / action` prose before the action — because the schema demands it.

This project takes a different bet:

- **Rust for the deterministic plumbing** — CDP transport, browser
  lifecycle, DOM snapshot, click/type dispatch. Predictable, fast,
  cheap to run at scale.
- **Python for the parts that need iteration** — agent loop, LLM
  providers, prompts, tool registry. The Python ecosystem owns this.
- **Native tool calling** — Anthropic `tool_use` and Gemini
  `function_call`, not JSON-mode unions. The model emits a structured
  call directly; no preamble. This is where most of the speed and cost
  win comes from.

Two providers (Anthropic + Gemini) instead of thirteen. No daemon, no
tunnel, no sandbox, no telemetry. ~5,500 lines of code total vs
~64,000.

## Install

Requires Rust 1.80+, Python 3.10+, and a Chromium binary (Chrome,
Chromium, or Chrome for Testing).

```sh
git clone https://github.com/sauravpanda/browser-use-rust.git
cd browser-use-rust

python3 -m venv .venv
.venv/bin/pip install --upgrade pip maturin anthropic 'google-genai>=1.0'
.venv/bin/maturin develop
```

The Rust extension builds to an `abi3` wheel — one wheel works for any
Python ≥3.10.

## Hello, browser

```python
import asyncio
from browser_use_rs import BrowserSession

async def main():
    session = BrowserSession(headless=True, viewport=(1280, 900))
    await session.start()
    await session.navigate("https://example.com")
    snap = await session.dom_snapshot()
    print(snap.to_llm_string())  # numbered list of clickable elements
    png = await session.screenshot()
    open("page.png", "wb").write(png)
    await session.stop()

asyncio.run(main())
```

## Hello, agent (Gemini)

```python
import asyncio
from browser_use_rs._browser_tools import BROWSER_TOOLS
from browser_use_rs.agent_gemini import GeminiAgent

async def main():
    agent = GeminiAgent(
        task="Go to https://news.ycombinator.com and tell me the top story.",
        tools=BROWSER_TOOLS,
        max_steps=8,
    )
    print(await agent.run())

asyncio.run(main())
```

`export GEMINI_API_KEY=...` first. The same shape works with `Agent`
(Anthropic) — see `python/examples/agent_demo.py` and
`python/examples/agent_demo_gemini.py`.

## Architecture

```
crates/
  bu-cdp/       Async WebSocket CDP transport (~140 LOC)
  bu-browser/   BrowserSession, navigate, click, snapshot, tabs, downloads, cookies, ...
  bu-dom/       DOM snapshot + serializer + iframe traversal
  bu-py/        PyO3 + pyo3-async-runtimes bindings (the only crate Python knows about)

python/browser_use_rs/
  agent.py        Agent loop driving Anthropic Claude (default: opus-4-7)
  agent_gemini.py Agent loop driving Google Gemini (default: 2.5-flash / 3-flash)
  tools.py        @tool decorator that derives JSON Schema from type hints
  _browser_tools.py  25 built-in tools wrapping the Rust BrowserSession
```

The agent loop is the **manual variant of Anthropic's tool-use loop**, not
the SDK's `tool_runner`. We need the manual loop so `screenshot` can return
image content blocks the model literally sees as part of the
`tool_result` — the runner only surfaces strings. Same trick is harder
to get right with Gemini (its `function_response` doesn't allow image
content), so the Gemini path attaches the PNG as a separate user-message
`Part` right after the function response.

## Tools available to the agent

Navigation: `navigate`, `wait_for_navigation`, `current_url` (internal)
DOM: `dom_snapshot`, `wait_for_selector`, `get_text`, `page_text`,
  `get_links`
Action: `click`, `type_text`, `upload_file`, `scroll`, `scroll_to`,
  `scroll_to_top`, `scroll_to_bottom`, `sleep`
Tabs: `list_tabs`, `switch_tab`, `new_tab`, `close_tab` — also expose
  cross-origin iframes as switchable contexts
Persistence: `screenshot`, `save_pdf`, `list_downloads`, `get_cookies`,
  `clear_cookies`

25 in total. Define your own with `@tool`.

## Benchmarks

10 tasks, both systems use `gemini-3-flash-preview`. All 10/10 succeeded
for both with correct answers.

| | browser-use-rs | Python browser-use | ratio |
|---|---:|---:|---:|
| Total wall time | 57.0 s | 193.7 s | **3.4× faster** |
| Total cost | $0.048 | $0.093 | **48% cheaper** |
| Total steps | 46 | 26 | theirs fewer |
| Total input tokens | 133k | 184k | ours 27% lighter |
| Total output tokens | 713 | 12,385 | **17× lighter** |

The output-token gap is the explanation. Theirs averages **476 output
tokens per step**; ours averages **15.5**. Native function calling skips
the prose preamble that JSON-mode unions force. Each of their steps takes
~7.5s vs our ~1.2s.

Reproduce with:

```sh
export GEMINI_API_KEY=...
.venv/bin/python bench/bench.py
```

Raw results: [bench/results.json](bench/results.json).

## What's deliberately not in scope

- **More LLM providers beyond Anthropic + Gemini.** Direct adapters land
  when there's user demand, not preemptively. OpenAI is the next likely
  one. No LiteLLM-style provider abstraction.
- **CLI / daemon / cloudflare tunnel.** That's a separate product surface.
- **Sandbox / Skills system.** Niche features that drove most of the
  bloat in the source project.
- **MCP server.** MCP client tools could be added; running our own MCP
  server is its own product.
- **Sync wrapper.** `asyncio.run()` is one line.

## Honest gaps vs. Python `browser-use`

- **Shadow DOM traversal** — our snapshot doesn't walk shadow DOM trees.
  Many modern web apps (Salesforce, custom-elements UIs) hide content
  there.
- **Streaming agent events** — the agent returns at end, not per-step.
  Needed for any UI integration.
- **Full AgentHistory persistence** — we record `usage_log` and
  `error_log`; theirs records full screenshots + action + result per step
  for replay.
- **Built-in `search` action** — we let the model construct search URLs.
- **OpenAI provider** — planned.
- **Anti-bot launch flags** — DDG and Google detect headless Chrome and
  serve degraded pages. Worth adding `--disable-blink-features=
  AutomationControlled` etc. as a flag.

See `python/examples/agent_demo*.py` for what works today.

## Project layout

```
.
├── Cargo.toml              workspace
├── pyproject.toml          maturin → builds crates/bu-py
├── crates/
│   ├── bu-cdp/             CDP WebSocket transport
│   ├── bu-browser/         browser session, navigate, click, snapshot
│   ├── bu-dom/             DOM snapshot + serializer + iframe walk
│   └── bu-py/              PyO3 bindings
├── python/
│   ├── browser_use_rs/
│   │   ├── _native.pyi     type stubs for the Rust extension
│   │   ├── agent.py        Anthropic agent loop
│   │   ├── agent_gemini.py Gemini agent loop
│   │   ├── tools.py        @tool decorator
│   │   └── _browser_tools.py  built-in tool set
│   └── examples/
│       ├── browser.py
│       ├── click.py
│       ├── agent_demo.py
│       └── agent_demo_gemini.py
└── bench/
    ├── bench.py            10-task comparison vs Python browser-use
    ├── run_ours.py
    ├── run_theirs.py
    └── results.json
```

## Development

```sh
cargo check --workspace          # type-check all crates
cargo test --workspace           # run Rust tests (sparse for now)
.venv/bin/maturin develop        # rebuild + install Python extension
.venv/bin/python python/examples/click.py   # smoke test
```

The Rust crates live under `crates/`. After any change to a crate, run
`maturin develop` to rebuild the Python extension. Pure Python changes
(under `python/browser_use_rs/`) take effect on the next interpreter
start without rebuilding.

## License

MIT — see [LICENSE](LICENSE).
