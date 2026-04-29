# browser-use-rs

A lean Rust runtime for [browser-use](https://github.com/browser-use/browser-use), with Python bindings.

The browser, CDP, and DOM layers are written in Rust. The agent loop, LLM
providers, and tool registry stay in Python. The result is **3.7× faster
and 56% cheaper than Python `browser-use` on the same tasks with the same
LLM**, with **23× fewer output tokens per step** — see [bench/](bench/)
for the suite and the raw numbers.

The Python surface is API-compatible with `browser_use` for the eval-runner
path: pure import swaps, no call-site or history-read changes. See [Drop-in
for `browser_use`](#drop-in-for-browser_use) below.

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
- **Native tool calling** — Anthropic `tool_use`, Gemini
  `function_call`, OpenAI `function`. Not JSON-mode unions. The model
  emits a structured call directly; no preamble. This is where most of
  the speed and cost win comes from.

Five providers (Anthropic, Google, OpenAI, Azure OpenAI, Groq) — lazy
imports, install only what you use. No daemon, no tunnel, no sandbox,
no telemetry.

## Install

Requires Rust 1.80+, Python 3.10+, and a Chromium binary (Chrome,
Chromium, or Chrome for Testing).

```sh
git clone https://github.com/sauravpanda/browser-use-rust.git
cd browser-use-rust

python3 -m venv .venv
.venv/bin/pip install --upgrade pip maturin anthropic 'google-genai>=1.0' \
    'openai>=1.50' pydantic
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

## Hello, agent

```python
import asyncio
from browser_use_rs import Agent
from browser_use_rs.llm import ChatGoogle  # or ChatAnthropic, ChatOpenAI, ...

async def main():
    agent = Agent(
        task="Go to https://news.ycombinator.com and tell me the top story.",
        llm=ChatGoogle(model="gemini-2.5-flash"),
        max_steps=8,
    )
    history = await agent.run()
    print(history.final_result())

asyncio.run(main())
```

`export GEMINI_API_KEY=...` first (or `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GROQ_API_KEY`, `AZURE_OPENAI_API_KEY`). See
`python/examples/agent_demo.py` (Anthropic) and
`python/examples/agent_demo_gemini.py` (Gemini).

## Drop-in for `browser_use`

The Python surface mirrors `browser_use.Agent` so the eval-runner path
swaps in with import changes only — no call-site rewrites:

```python
# Before
from browser_use import Agent, BrowserSession, BrowserProfile, Controller
from browser_use.agent.views import AgentHistoryList
from browser_use.llm.anthropic.chat import ChatAnthropic
from browser_use.llm.google.chat import ChatGoogle

# After
from browser_use_rs import Agent, BrowserSession, BrowserProfile, Controller
from browser_use_rs.agent.views import AgentHistoryList
from browser_use_rs.llm import ChatAnthropic, ChatGoogle
```

What works as-is: `Agent(task=, llm=, browser_session=, controller=,
use_vision=, max_actions_per_step=, use_thinking=, flash_mode=,
sensitive_data=, source=, override_system_message=, initial_actions=,
register_new_step_callback=, register_done_callback=,
register_should_stop_callback=, injected_agent_state=)`,
`agent.run(max_steps=, on_step_start=, on_step_end=)`,
`agent.add_new_task(...)`, `agent.message_manager.last_input_messages`,
`history.final_result()`, `history.is_done()`,
`history.history[i].state.get_screenshot()`,
`history.history[i].result[j].extracted_content/.is_done/.success/.error`,
`history.history[i].metadata['input_tokens']`,
`history.history[i].model_output.model_dump()`,
`history.usage.model_dump()`, `BrowserSession(cdp_url=...)` for
Anchor/Browserbase/Daytona/BrightData, `BrowserProfile(headless=,
window_size=, ...)`, `Controller()` with
`@controller.registry.action(description, param_model=PydanticModel)`.

Compat-only kwargs (`use_thinking`, `flash_mode`, `images_per_step`,
`use_judge`, `judge_llm`, `ground_truth`, `calculate_cost`, `stealth`,
`highlight_elements`, `keep_alive`, `allowed_domains`, ...) are accepted
silently — they don't change behavior yet, but importing code doesn't break.

## Architecture

```
crates/
  bu-cdp/       Async WebSocket CDP transport (~140 LOC)
  bu-browser/   BrowserSession, navigate, click, snapshot, tabs, downloads, cookies, ...
  bu-dom/       DOM snapshot + serializer + iframe traversal
  bu-py/        PyO3 + pyo3-async-runtimes bindings (the only crate Python knows about)

python/browser_use_rs/
  agent/__init__.py  Provider-agnostic Agent loop (returns AgentHistoryList)
  agent/views.py     Compat re-exports for browser_use.agent.views path
  views.py           AgentHistoryList, AgentHistory, ActionResult, AgentState, ...
  browser.py         BrowserSession + BrowserProfile Python wrappers
  controller.py      Controller + @registry.action decorator
  llm/base.py        BaseChatModel + ChatInvokeCompletion/Usage + Message types
  llm/anthropic.py   ChatAnthropic (tool_use, adaptive thinking, prompt caching)
  llm/google.py      ChatGoogle (function_call, thought_signature replay)
  llm/openai.py      ChatOpenAI (function tool calling)
  llm/azure.py       ChatAzureOpenAI (Azure deployment routing)
  llm/groq.py        ChatGroq (OpenAI-compat over Groq endpoint)
  llm/messages.py    Compat re-exports for browser_use.llm.messages path
  tools.py           @tool decorator that derives JSON Schema from type hints
  _browser_tools.py  25 built-in tools wrapping the Rust BrowserSession
  agent_gemini.py    Deprecated thin wrapper — kept for back-compat
```

The agent loop is a **manual native-tool-call loop**: each provider's
`ainvoke` returns structured `tool_calls`; the agent executes them in
parallel via `asyncio.gather`, sends results back as `tool_result` /
`function_response` / `tool` messages. Image-in-tool-result works
natively for Anthropic; for Gemini and OpenAI the screenshot is split
into the next user-message `Part` automatically inside their providers.

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
| Total wall time | 59.2 s | 219.7 s | **3.7× faster** |
| Total cost | $0.051 | $0.116 | **56% cheaper** |
| Total steps | 48 | 30 | theirs fewer |
| Total input tokens | 143k | 218k | ours 35% lighter |
| Total output tokens | 730 | 17,053 | **23× lighter** |

The output-token gap is the explanation. Theirs averages **~568 output
tokens per step**; ours averages **~15**. Native function calling skips
the prose preamble that JSON-mode unions force. Each of their steps takes
~7.3s vs our ~1.2s.

The hardest task in the suite (`multitab` — open two pages, return both
headlines) is **3.7× faster** for us at $0.006 vs theirs $0.020.

Reproduce with:

```sh
export GEMINI_API_KEY=...
.venv/bin/python bench/bench.py
```

Raw results: [bench/results.json](bench/results.json).

## What's deliberately not in scope

- **A LiteLLM-style provider abstraction.** Five direct adapters cover
  the providers eval/cloud consumers actually use; new ones land when
  asked for, not preemptively.
- **CLI / daemon / cloudflare tunnel.** That's a separate product surface.
- **Sandbox / Skills system.** Niche features that drove most of the
  bloat in the source project.
- **MCP server.** MCP client tools could be added; running our own MCP
  server is its own product.
- **Sync wrapper.** `asyncio.run()` is one line.

## Honest gaps vs. Python `browser-use`

- **`file_system_path` / `available_file_paths`** — eval consumers pass
  these; currently swallowed.
- **Shadow DOM traversal** — our snapshot doesn't walk shadow DOM trees.
  Many modern web apps (Salesforce, custom-elements UIs) hide content
  there.
- **Streaming agent events** — the agent returns at end, not per-step.
  Needed for any UI integration. Step callbacks already fire per-step
  in-process.
- **Link-click navigation guard** — `allowed_domains` / `prohibited_domains`
  enforcement runs on `navigate()` and `new_tab()` (where the model
  explicitly chooses a URL). Navigation triggered by clicking a link
  isn't intercepted at the CDP level yet — needs `Network.setRequestInterception`
  or `Fetch.enable` to catch all forms.
- **Built-in `search` action** — we let the model construct search URLs.
  Cloud consumers register their own DDG-Lite action via Controller.
- **`generate_gif`** — no per-step screenshot stitching.

## Recently landed

- **`allowed_domains` / `prohibited_domains` enforcement** — full
  `browser_use` pattern syntax (`*.example.com`, `*google.com`,
  `chrome-extension://*`, `http*://example.com`). Enforced on
  `navigate()` + `new_tab()`. 0.4.4.
- **Stealth Chrome flags** — `BrowserSession(stealth=True)` adds
  `--disable-blink-features=AutomationControlled` etc. Helps against
  Google/DDG headless detection. 0.4.4.
- **`observe()` / `observe_debug()` no-op stubs** at
  `browser_use_rs.observability` — drops in for consumers that import
  these from `browser_use.observability`. 0.4.4.

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
│   │   ├── _native.pyi       type stubs for the Rust extension
│   │   ├── agent/            unified Agent loop + agent.views compat path
│   │   ├── llm/              base + 5 provider adapters + messages compat path
│   │   ├── views.py          AgentHistoryList / AgentHistory / ActionResult / ...
│   │   ├── browser.py        BrowserSession + BrowserProfile wrappers
│   │   ├── controller.py     Controller + @registry.action decorator
│   │   ├── tools.py          @tool decorator
│   │   ├── _browser_tools.py built-in tool set
│   │   └── agent_gemini.py   deprecated back-compat shim
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
