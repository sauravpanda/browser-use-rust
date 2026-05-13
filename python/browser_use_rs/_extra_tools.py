"""Extended tool surface for the agent (v0.6.0). Mirrors several upstream
browser_use tools we were missing:

  - extract_structured_data : LLM-powered query extraction over the page
  - read_file / write_file / replace_file_str : sandboxed file system
  - search_page             : grep page text without an LLM call
  - find_elements           : CSS-selector query, attribute extraction
  - find_text               : scroll a visible substring into view
  - get_dropdown_options    : list <select>/role=listbox options
  - select_dropdown         : select an option by visible text or value
  - send_keys               : keyboard events (Tab, Enter, etc.)
  - go_back                 : history.back()
  - evaluate_js             : escape hatch — run arbitrary JS

Tools that need access to the agent's LLM or file-system root can't be
plain @tool functions (which only get `session`). They're constructed
in `make_extra_tools(agent)` so they capture `agent` as a closure.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from typing import Any

from browser_use_rs.tools import tool


# ---------------------------------------------------------------------------
# Stateless tools (don't need agent reference) — module-level @tool
# ---------------------------------------------------------------------------

@tool
async def search_page(session, pattern: str, max_results: int = 10) -> str:
    """Search the current page text for a regex pattern. Returns matching
    lines with their character offsets. Zero LLM calls.

    Use this for "is X on this page" / "find the section about Y" without
    paying for a full page_text + reasoning round.

    Args:
        pattern: Regex (Python flavor). Use simple substrings if unsure.
        max_results: Cap on returned matches. Default 10.
    """
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as e:
        return f"invalid regex: {e}"
    text = await session.page_text(50000)
    if not text:
        return "(empty page)"
    matches: list[str] = []
    for m in rx.finditer(text):
        if len(matches) >= max_results:
            break
        start = max(0, m.start() - 60)
        end = min(len(text), m.end() + 60)
        excerpt = text[start:end].replace("\n", " ")
        matches.append(f"@{m.start()}: …{excerpt}…")
    if not matches:
        return f"(no matches for {pattern!r})"
    return "\n".join(matches)


@tool
async def find_elements(
    session, selector: str, attributes: str = "", limit: int = 20
) -> str:
    """Query the page by CSS selector. Returns matched elements as
    `<tag attr1="v1" ...>text</tag>` lines.

    Use to enumerate things the indexed snapshot doesn't show — e.g.
    `find_elements("article h2")` for headlines, or
    `find_elements(".price", "data-product-id")` for prices with their
    product IDs.

    Args:
        selector: CSS selector string (`.price`, `article h2`, `[data-x]`).
        attributes: Comma-separated attribute names to extract per
            element (e.g. `"href,title"`). Empty = just text.
        limit: Cap on results. Default 20.
    """
    attrs_list = [a.strip() for a in attributes.split(",") if a.strip()]
    js = (
        "(() => {"
        f" const sel = {json.dumps(selector)};"
        f" const attrs = {json.dumps(attrs_list)};"
        f" const lim = {int(limit)};"
        " const out = [];"
        " try {"
        "   const els = document.querySelectorAll(sel);"
        "   for (let i = 0; i < els.length && out.length < lim; i++) {"
        "     const el = els[i];"
        "     const r = el.getBoundingClientRect();"
        "     if (r.width < 1 || r.height < 1) continue;"
        "     const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200);"
        "     const a = {};"
        "     for (const k of attrs) { const v = el.getAttribute(k); if (v != null) a[k] = String(v).slice(0, 200); }"
        "     out.push({tag: el.tagName.toLowerCase(), text, attrs: a});"
        "   }"
        " } catch(e) { return JSON.stringify({error: String(e)}); }"
        " return JSON.stringify({matches: out});"
        "})()"
    )
    raw = await session.evaluate(js)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return f"(unparseable result: {raw[:200]})"
    if "error" in data:
        return f"(query error: {data['error']})"
    matches = data.get("matches", [])
    if not matches:
        return f"(no elements match {selector!r})"
    lines = []
    for m in matches:
        attr_str = " ".join(
            f'{k}="{v}"' for k, v in (m.get("attrs") or {}).items()
        )
        prefix = f"<{m['tag']}"
        if attr_str:
            prefix += " " + attr_str
        text = m.get("text") or ""
        lines.append(f"{prefix}>{text}</{m['tag']}>")
    return "\n".join(lines)


@tool
async def find_text(session, text: str) -> str:
    """Scroll the page so a visible occurrence of `text` is in view.
    Returns the new scroll position or "(text not found)" if not present.

    Use when you know a word or phrase appears on the page but it's
    above/below the fold and you need to read its surroundings.

    Args:
        text: Substring to locate (case-insensitive).
    """
    needle = text.lower()
    js = (
        "(() => {"
        f" const needle = {json.dumps(needle)};"
        " const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);"
        " let node;"
        " while ((node = walker.nextNode())) {"
        "   const v = (node.nodeValue || '').toLowerCase();"
        "   if (v.includes(needle)) {"
        "     const p = node.parentElement;"
        "     if (p) { p.scrollIntoView({block: 'center'}); return JSON.stringify({found: true, y: window.scrollY}); }"
        "   }"
        " }"
        " return JSON.stringify({found: false});"
        "})()"
    )
    raw = await session.evaluate(js)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return f"(unparseable: {raw[:120]})"
    if data.get("found"):
        return f"scrolled to {text!r} at y={data.get('y', '?')}"
    return "(text not found)"


@tool
async def get_dropdown_options(session, index: int) -> str:
    """List the options of a `<select>` or `[role=listbox]` element by
    its [N] index from the most recent dom_snapshot. Returns one
    `value | label` line per option.

    Args:
        index: The [N] index of the dropdown element.
    """
    js = (
        "(() => {"
        f" const idx = {int(index)};"
        " const el = document.querySelector(`[data-bu-idx=\"${idx}\"]`);"
        " if (!el) return JSON.stringify({error: 'no element with index'});"
        " const tag = el.tagName.toLowerCase();"
        " const out = [];"
        " if (tag === 'select') {"
        "   for (const o of el.options) out.push({value: o.value, label: o.text});"
        " } else {"
        "   for (const o of el.querySelectorAll('[role=\"option\"], li, .option')) {"
        "     const t = (o.innerText || o.textContent || '').replace(/\\s+/g, ' ').trim();"
        "     if (t) out.push({value: t, label: t});"
        "     if (out.length >= 100) break;"
        "   }"
        " }"
        " return JSON.stringify({tag, options: out});"
        "})()"
    )
    raw = await session.evaluate(js)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return f"(unparseable: {raw[:120]})"
    if "error" in data:
        return f"(error: {data['error']})"
    options = data.get("options", [])
    if not options:
        return f"(no options on [{index}] — element may not be a dropdown)"
    lines = [f"{o['value']} | {o['label']}" for o in options[:200]]
    return f"<{data.get('tag','?')}> options:\n" + "\n".join(lines)


@tool
async def select_dropdown(
    session,
    index: int,
    value: str = "",
    text: str = "",
) -> str:
    """Select an option in a `<select>` element or an ARIA dropdown
    (combobox / listbox / menu / Semantic UI custom) by its visible
    label or value.

    Args:
        index: The [N] index of the dropdown element.
        value: The visible label (preferred) or the option's value attr.
        text: Alias for `value` matching upstream browser_use's
            `select_dropdown(text=...)` parameter. If both are given,
            `value` wins. Case-insensitive matching.

    v0.8.12: ported the JS from upstream
    `default_action_watchdog.on_SelectDropdownOptionEvent`. Adds:
    case-insensitive matching, focus()-before-set + blur()-after-set
    (critical for React/Vue/Svelte reactive frameworks), selection-
    reverted detection (when the framework re-overrides our value),
    fallback to click-the-option when set fails, ARIA combobox/menu
    parity (matches `[role=combobox|listbox|menu]` + child
    `[role=menuitem|option]` + `data-value` attr matching), Semantic
    UI / `.dropdown.ui` class detection.

    Original v0.6.5 behavior was a thin native-select impl that didn't
    handle reactive frameworks or any non-`[role=option]` ARIA; the
    Lakers nba.com schedule task (combobox-driven) failed where
    upstream succeeded — that gap is what this port closes.
    """
    want = (value or text or "").strip()
    if not want:
        return "(error: must pass `value` or `text` arg)"
    js = (
        "(() => {"
        f" const idx = {int(index)};"
        f" const want = {json.dumps(want)};"
        " const el = document.querySelector(`[data-bu-idx=\"${idx}\"]`);"
        " if (!el) return JSON.stringify({error: 'no element with index'});"
        " const wantLow = want.toLowerCase();"
        " function trySelect(element) {"
        "   const tag = element.tagName.toLowerCase();"
        "   if (tag === 'select') {"
        "     const opts = Array.from(element.options);"
        "     for (const o of opts) {"
        "       const tLow = o.text.trim().toLowerCase();"
        "       const vLow = (o.value || '').toLowerCase();"
        "       if (tLow === wantLow || vLow === wantLow) {"
        "         element.focus();"
        "         element.value = o.value;"
        "         o.selected = true;"
        "         element.selectedIndex = o.index;"
        "         element.dispatchEvent(new Event('input', {bubbles: true, cancelable: true}));"
        "         element.dispatchEvent(new Event('change', {bubbles: true, cancelable: true}));"
        "         element.blur();"
        "         if (element.value !== o.value) {"
        "           return {success: false, reverted: true, error: 'selection reverted by framework — try clicking the option', tried: o.text.trim()};"
        "         }"
        "         return {success: true, selected: o.text.trim(), value: o.value};"
        "       }"
        "     }"
        "     return {success: false, error: 'no <select> option matching ' + want, available: opts.slice(0,30).map(o => o.text.trim())};"
        "   }"
        "   const role = (element.getAttribute('role') || '').toLowerCase();"
        "   if (role === 'menu' || role === 'listbox' || role === 'combobox') {"
        "     const items = element.querySelectorAll('[role=\"menuitem\"], [role=\"option\"]');"
        "     for (const item of items) {"
        "       const txt = ((item.innerText || item.textContent) || '').trim();"
        "       const dv = (item.getAttribute('data-value') || '').toLowerCase();"
        "       if (txt.toLowerCase() === wantLow || dv === wantLow) {"
        "         items.forEach(mi => { mi.setAttribute('aria-selected', 'false'); mi.classList.remove('selected'); });"
        "         item.setAttribute('aria-selected', 'true');"
        "         item.classList.add('selected');"
        "         item.click();"
        "         item.dispatchEvent(new MouseEvent('click', {view: window, bubbles: true, cancelable: true}));"
        "         return {success: true, selected: txt};"
        "       }"
        "     }"
        "     return {success: false, error: 'no ARIA item matching ' + want, available: Array.from(items).slice(0,30).map(i => ((i.innerText||i.textContent)||'').trim())};"
        "   }"
        "   if (element.classList.contains('dropdown') || element.classList.contains('ui')) {"
        "     const items = element.querySelectorAll('.item, .option, [data-value]');"
        "     for (const item of items) {"
        "       const txt = ((item.innerText || item.textContent) || '').trim();"
        "       const dv = (item.getAttribute('data-value') || '').toLowerCase();"
        "       if (txt.toLowerCase() === wantLow || dv === wantLow) {"
        "         item.click();"
        "         return {success: true, selected: txt};"
        "       }"
        "     }"
        "     return {success: false, error: 'no Semantic UI dropdown item matching ' + want};"
        "   }"
        "   return {success: false, error: 'element [' + idx + '] is not a recognized dropdown (tag=' + tag + ', role=' + role + ')'};"
        " }"
        " const result = trySelect(el);"
        " return JSON.stringify(result);"
        "})()"
    )
    raw = await session.evaluate(js)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return f"(unparseable: {raw[:120]})"
    if data.get("success"):
        return f"selected: {data.get('selected', want)}"
    err = data.get("error") or "unknown error"
    if data.get("available"):
        avail = ", ".join(repr(a)[:40] for a in data["available"][:15])
        return f"(error: {err}; available: {avail})"
    return f"(error: {err})"


# Canonical names accepted by the Rust dispatch_key impl. The map
# normalizes common LLM input variations (case, "Esc" vs "Escape", etc.)
# to the exact tokens Rust expects.
_CDP_KEY_ALIASES = {
    "enter": "Enter", "return": "Enter", "ret": "Enter",
    "tab": "Tab",
    "escape": "Escape", "esc": "Escape",
    "backspace": "Backspace", "bksp": "Backspace",
    "delete": "Delete", "del": "Delete",
    "space": "Space", "spacebar": "Space",
    "arrowup": "ArrowUp", "up": "ArrowUp",
    "arrowdown": "ArrowDown", "down": "ArrowDown",
    "arrowleft": "ArrowLeft", "left": "ArrowLeft",
    "arrowright": "ArrowRight", "right": "ArrowRight",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp", "pgup": "PageUp",
    "pagedown": "PageDown", "pgdn": "PageDown",
}


@tool
async def send_keys(session, keys: str) -> str:
    """Send a real CDP keyboard event for a special key (Enter, Tab,
    Escape, Backspace, Delete, Space, ArrowUp/Down/Left/Right, Home,
    End, PageUp, PageDown).

    v0.8.19 routes through Rust `session.dispatch_key()` →
    CDP `Input.dispatchKeyEvent`. This issues a "trusted" event that
    triggers default browser behavior (form submit on Enter, focus
    move on Tab, etc.). The previous JS `KeyboardEvent` dispatch was
    "untrusted" per the WHATWG spec and silently no-op'd default
    actions on most sites — that's why "type then Enter to submit"
    flows often hung and burned step budget.

    Modifier combos like "Ctrl+a" are NOT supported here yet (would
    need CDP modifier-bit support). For typing literal text, use
    type_text instead.

    Args:
        keys: A single key name. Case-insensitive, common aliases
            supported (Enter/Return, Esc/Escape, Up/ArrowUp, etc.).
    """
    canonical = _CDP_KEY_ALIASES.get(keys.strip().lower())
    if not canonical:
        return (
            f"(unsupported key {keys!r} — use one of: "
            "Enter, Tab, Escape, Backspace, Delete, Space, "
            "ArrowUp, ArrowDown, ArrowLeft, ArrowRight, "
            "Home, End, PageUp, PageDown)"
        )
    try:
        await session.dispatch_key(canonical)
        return f"sent {canonical} (trusted CDP key event)"
    except Exception as e:
        return f"(dispatch_key failed: {type(e).__name__}: {e})"


@tool
async def go_back(session) -> str:
    """Navigate the browser history back one step. Equivalent to the
    browser's back button."""
    await session.evaluate("(() => { history.back(); return ''; })()")
    return "navigated back"


@tool
async def web_search(session, query: str, engine: str = "duckduckgo") -> str:
    """Open a search-engine results page for `query`. Use when the
    requested information isn't on a known site and you need to find
    it. Subsequent click/scroll/extract calls operate on the results
    page.

    Mirrors upstream browser_use's web_search action (v0.6.5).

    Args:
        query: Search terms.
        engine: 'duckduckgo' (default), 'google', or 'bing'.
    """
    eng = engine.lower().strip()
    base = {
        "duckduckgo": "https://duckduckgo.com/?q=",
        "google": "https://www.google.com/search?q=",
        "bing": "https://www.bing.com/search?q=",
    }.get(eng, "https://duckduckgo.com/?q=")
    from urllib.parse import quote
    url = base + quote(query)
    await session.navigate(url)
    if os.getenv("BROWSER_USE_RS_WEB_SEARCH_SNIPPETS", "").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return f"opened {eng} results for: {query}"
    try:
        await asyncio.sleep(0.8)
        snippets = await session.evaluate(
            r"""
(() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const unwrapUrl = (raw) => {
    try {
      const u = new URL(raw, location.href);
      const host = u.hostname.replace(/^www\./, '');
      if (host.endsWith('duckduckgo.com') && u.searchParams.get('uddg')) {
        return decodeURIComponent(u.searchParams.get('uddg'));
      }
      if (host.endsWith('google.com') && u.pathname === '/url' && u.searchParams.get('q')) {
        return u.searchParams.get('q');
      }
      if (host.endsWith('bing.com') && u.searchParams.get('u')) {
        return u.searchParams.get('u');
      }
      return u.href;
    } catch (_) {
      return raw || '';
    }
  };
  const currentHost = location.hostname.replace(/^www\./, '');
  const searchHosts = [
    'duckduckgo.com', 'google.com', 'bing.com', 'yahoo.com',
    'startpage.com', 'search.brave.com',
  ];
  const skipText = /^(images|videos|news|maps|shopping|books|flights|search|settings|tools|more|next|previous|feedback|cached|similar)$/i;
  const results = [];
  const seen = new Set();
  for (const a of Array.from(document.querySelectorAll('a[href]'))) {
    const title = clean(a.innerText || a.textContent);
    if (title.length < 3 || title.length > 180 || skipText.test(title)) continue;
    const href = unwrapUrl(a.getAttribute('href') || a.href);
    let parsed;
    try { parsed = new URL(href, location.href); } catch (_) { continue; }
    const host = parsed.hostname.replace(/^www\./, '');
    if (!/^https?:$/.test(parsed.protocol)) continue;
    if (searchHosts.some((h) => host === h || host.endsWith('.' + h))) continue;
    if (host === currentHost) continue;
    const key = parsed.href.replace(/[#?].*$/, '') + '|' + title.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);

    let container = a;
    for (let i = 0; i < 5 && container.parentElement; i++) {
      const parentText = clean(container.parentElement.innerText || container.parentElement.textContent);
      if (parentText.length > title.length + 40 && parentText.length < 1400) {
        container = container.parentElement;
        break;
      }
      container = container.parentElement;
    }
    let snippet = clean(container.innerText || container.textContent);
    if (snippet.startsWith(title)) snippet = clean(snippet.slice(title.length));
    snippet = snippet
      .replace(/^https?:\/\/\S+\s*/i, '')
      .replace(/\b(Cached|Similar|Translate this page)\b/gi, '')
      .slice(0, 420);
    results.push({ title, url: parsed.href, snippet });
    if (results.length >= 8) break;
  }
  if (!results.length) return '';
  return results.map((r, i) => {
    const snippet = r.snippet ? `\n   ${r.snippet}` : '';
    return `${i + 1}. ${r.title}\n   ${r.url}${snippet}`;
  }).join('\n');
})()
"""
        )
    except Exception:
        snippets = ""
    if snippets and snippets.strip():
        return f"opened {eng} results for: {query}\n\nTop visible results:\n{snippets.strip()}"
    return (
        f"opened {eng} results for: {query}\n"
        f"(no result snippets were extractable from the search page; use "
        f"extract_structured_data or page_text if the results are visible)"
    )


@tool
async def extract_links(session, limit: int = 50) -> str:
    """Extract all visible links from the current page as
    `text -> href` lines, sorted by appearance. Capped at `limit`.

    Use when you need a list of clickable destinations to choose from
    (e.g. listing article URLs, finding the right product page).

    Args:
        limit: Max number of links to return. Default 50.
    """
    raw_links = await session.get_links()
    if not raw_links:
        return "(no visible links)"
    out = []
    # v0.8.17: Rust session.get_links() returns Vec<(String, String)>
    # where the first element is href and the second is text (per
    # crates/bu-browser/src/lib.rs:1287). Previously we unpacked as
    # `for text, href in raw_links`, which swapped them: URLs ended up
    # as labels and labels ended up as hrefs. Tasks asking for "the URL
    # of the X link" got confidently-wrong results back. Same bug at
    # _extra_tools.py:717 in the extract_structured_data link
    # augmentation path, fixed with the same swap.
    for href, text in raw_links[:limit]:
        text = text.strip().replace("\n", " ")[:80] or "(no text)"
        out.append(f"{text} -> {href}")
    if len(raw_links) > limit:
        out.append(f"(... {len(raw_links) - limit} more links truncated)")
    return "\n".join(out)


@tool
async def extract_images(session, limit: int = 30) -> str:
    """Extract all visible <img> elements from the page as
    `alt -> src` lines. Useful for tasks that need to identify
    pictures by caption / alt text.

    Args:
        limit: Max images to return. Default 30.
    """
    js = (
        "(() => {"
        f" const lim = {int(limit)};"
        " const out = [];"
        " for (const img of document.querySelectorAll('img')) {"
        "   if (out.length >= lim) break;"
        "   const r = img.getBoundingClientRect();"
        "   if (r.width < 32 || r.height < 32) continue;"
        "   const src = img.src || img.getAttribute('src') || '';"
        "   if (src.startsWith('data:') || !src) continue;"
        "   const alt = (img.alt || img.getAttribute('alt') || img.getAttribute('title') || '').trim();"
        "   out.push({alt, src: src.length > 200 ? src.slice(0,200) + '…' : src});"
        " }"
        " return JSON.stringify(out);"
        "})()"
    )
    raw = await session.evaluate(js)
    try:
        data = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return f"(unparseable: {raw[:120]})"
    if not data:
        return "(no images)"
    return "\n".join(f"{(d.get('alt') or '(no alt)')!r} -> {d.get('src')}" for d in data)


@tool
async def evaluate_js(session, expression: str) -> str:
    """Execute an arbitrary JavaScript expression in the page context.
    Returns the JSON-stringified result.

    Use sparingly — prefer `find_elements` / `search_page` /
    `get_dropdown_options` when those fit. This is the escape hatch for
    custom DOM queries (shadow DOM traversal, computed style reads,
    custom widgets) that the structured tools can't reach.

    Args:
        expression: A JS expression. Can be wrapped in `(() => {...})()`
            for multi-statement bodies.
    """
    # v0.11.11: reverted v0.11.10's try/catch wrap. Wrapping changed
    # return-value serialization (raw return → String/JSON-stringify)
    # and correlated with a -7pp accuracy hit in the 198-task eval.
    # Bisecting v0.11.10's three fixes by reverting only this one;
    # keeping scroll_down/up real-tool fix and CDP -32001 retry which
    # were independently sound and contributed to the -40% action
    # error reduction.
    raw = await session.evaluate(expression)
    if not raw:
        return "(no result)"
    return raw[:5000] + ("…" if len(raw) > 5000 else "")


# ---------------------------------------------------------------------------
# Stateful tools — capture the agent in a closure (extract uses LLM,
# file tools share a sandboxed dir per-agent run).
# ---------------------------------------------------------------------------

def make_extra_tools(agent: Any) -> list:
    """Build agent-aware tools that need the LLM or a file-system root.
    Returns a list of @tool-decorated callables to merge with BROWSER_TOOLS.
    """

    # Per-agent sandbox directory — agent file tools may only read/write
    # inside this dir, never outside it. UUID prevents collisions when
    # multiple agents run concurrently.
    if not hasattr(agent, "_file_sandbox") or not agent._file_sandbox:
        sandbox = os.path.join(
            tempfile.gettempdir(),
            "browser-use-rs-files",
            uuid.uuid4().hex[:12],
        )
        os.makedirs(sandbox, exist_ok=True)
        agent._file_sandbox = sandbox
    sandbox = agent._file_sandbox

    def _resolve(path: str) -> str | None:
        # Disallow escape via .. or absolute path. Always relative to
        # sandbox.
        clean = path.lstrip("/").replace("..", "").strip()
        if not clean:
            return None
        full = os.path.join(sandbox, clean)
        # Defense in depth — make sure the resolved path stays inside.
        if not os.path.realpath(full).startswith(os.path.realpath(sandbox)):
            return None
        return full

    @tool
    async def read_file(
        session, path: str, offset: int = 0, max_chars: int = 50_000,
    ) -> str:
        """Read a file from the agent's sandbox directory. Use to recall
        notes, partial extractions, todo items, or large tool results
        (under `results/`) spilled by the read-state lifecycle.

        Args:
            path: Relative path in the agent sandbox (e.g. "notes.md"
                or "results/page_text_abc123.txt").
            offset: Character offset to start reading from (default 0).
                Use this to page through files larger than max_chars.
            max_chars: Max chars to return per call (default 50,000;
                hard cap 200,000). Lower for cheaper reads.

        Returns the requested slice plus a "[bytes K..L of N]" tail
        marker so paging is observable. v0.11.2: pagination added so
        the read-state lifecycle's "full result retrievable" promise
        is honored for spilled results > 50k chars.
        """
        full = _resolve(path)
        if not full or not os.path.isfile(full):
            return f"(no such file: {path})"
        try:
            offset = max(0, int(offset or 0))
            cap = max(1, min(int(max_chars or 50_000), 200_000))
            with open(full, "r", encoding="utf-8") as f:
                data = f.read()
            total = len(data)
            slice_ = data[offset : offset + cap]
            end = offset + len(slice_)
            if total > end or offset > 0:
                marker = (
                    f"\n\n[chars {offset:,}..{end:,} of {total:,}; "
                    f"pass offset={end} to continue]"
                    if end < total
                    else f"\n\n[chars {offset:,}..{end:,} of {total:,}; end of file]"
                )
                return slice_ + marker
            return slice_
        except Exception as e:
            return f"(read error: {e})"

    @tool
    async def write_file(session, path: str, content: str) -> str:
        """Write content to a file in the agent's sandbox. Overwrites
        existing files. Use for storing notes, partial extractions
        (e.g. when a long task needs to assemble data from many pages),
        or maintaining a `todo.md` checklist.

        Args:
            path: Relative path (e.g. "notes.md", "extracted/page1.json").
            content: File content (UTF-8 text).
        """
        full = _resolve(path)
        if not full:
            return f"(invalid path: {path})"
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content[:200000])
            return f"wrote {len(content)} chars to {path}"
        except Exception as e:
            return f"(write error: {e})"

    @tool
    async def replace_file_str(
        session, path: str, old: str, new: str
    ) -> str:
        """Replace all occurrences of `old` with `new` in a sandboxed
        file. Use to update a `todo.md` checklist (e.g. swap `[ ]` →
        `[x]` on completed items) without rewriting the whole file.

        Args:
            path: Relative path of the file to modify.
            old: Substring to find (literal, not regex).
            new: Replacement string.
        """
        full = _resolve(path)
        if not full or not os.path.isfile(full):
            return f"(no such file: {path})"
        try:
            with open(full, "r", encoding="utf-8") as f:
                data = f.read()
            count = data.count(old)
            if count == 0:
                return f"(no occurrences of {old!r} in {path})"
            data = data.replace(old, new)
            with open(full, "w", encoding="utf-8") as f:
                f.write(data)
            return f"replaced {count} occurrence(s) in {path}"
        except Exception as e:
            return f"(replace error: {e})"

    @tool
    async def list_files(session) -> str:
        """List all files currently in the agent's sandbox directory."""
        out: list[str] = []
        for root, _dirs, files in os.walk(sandbox):
            rel_root = os.path.relpath(root, sandbox)
            for f in files:
                p = f if rel_root == "." else os.path.join(rel_root, f)
                out.append(p)
        if not out:
            return "(sandbox empty)"
        return "\n".join(sorted(out))

    # Per-task dedup memory for extract_structured_data. Keys are
    # (query, page_offset_bucket); values are the answer strings we
    # already returned. Saves redundant LLM calls when the agent
    # re-extracts the same thing it just got. v0.7.0.
    extract_cache: dict = {}

    @tool
    async def extract_structured_data(
        session,
        query: str,
        max_chars: int = 30000,
        start_from_char: int = 0,
        output_schema_hint: str = "",
        extract_links: bool = False,
        extract_images: bool = False,
        already_collected: str = "",
    ) -> str:
        """Extract specific information from the current page using an
        LLM-powered query.

        This is the flagship READ tool. The agent's own LLM is asked to
        find the answer to `query` inside the page's text — much more
        reliable than dumping `page_text` and reasoning over it manually.
        Use for: extracting specific values (prices, names, dates),
        listing items matching a criterion ("top 3 articles"), summarizing
        a section, or answering a question about the page.

        Pagination: when a page is longer than max_chars, call again
        with start_from_char=max_chars to read the next chunk
        (start_from_char=30000 starts the second slice). Repeat until
        you find the answer or the chunk is empty.

        Structured output: pass output_schema_hint with a JSON-like
        sketch of the format you want. The extractor will try to match
        that shape. Example:
          output_schema_hint='{"products": [{"name": str, "price": str}]}'

        Args:
            query: What to extract. Be specific.
            max_chars: Max page text per chunk. Default 30000.
            start_from_char: Offset to start reading from (for paged
                long pages). Default 0.
            output_schema_hint: Optional JSON-like template the answer
                should follow. Default empty (free-form text answer).
        """
        # Dedup memory (v0.7.0). If the agent calls extract with the
        # exact same query+offset on the same URL we just answered, return
        # the cached answer instead of re-running the LLM. Saves ~$0.005
        # per duplicate call.
        try:
            url = await session.current_url()
        except Exception:
            url = ""
        cache_key = (url, query.strip(), start_from_char // 5000)
        if cache_key in extract_cache:
            return f"(cached) {extract_cache[cache_key]}"

        # Markdown extraction (v0.7.1): pull the page DOM and convert
        # to a cleaned markdown-style text. Drops scripts/styles/nav/
        # footer/aside (the chrome that pollutes raw page_text) and
        # converts headings, links, and lists into markdown markers
        # the LLM is well-trained on. Falls back to raw page_text if
        # the markdown extractor errors.
        markdown_js = r"""
            (() => {
                try {
                    const root = document.body.cloneNode(true);
                    for (const sel of ['script','style','noscript','nav','footer','aside','iframe']) {
                        for (const el of root.querySelectorAll(sel)) el.remove();
                    }
                    for (const h of root.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
                        const lvl = parseInt(h.tagName[1]);
                        h.innerHTML = '\n' + '#'.repeat(lvl) + ' ' + h.textContent.trim() + '\n';
                    }
                    for (const a of root.querySelectorAll('a[href]')) {
                        const t = (a.textContent || '').trim();
                        const href = a.getAttribute('href');
                        if (t && href) a.innerHTML = `[${t}](${href})`;
                    }
                    for (const li of root.querySelectorAll('li')) {
                        li.innerHTML = '\n- ' + li.textContent.trim();
                    }
                    for (const tag of ['p','div','section','article']) {
                        for (const el of root.querySelectorAll(tag)) {
                            el.innerHTML = '\n' + el.innerHTML + '\n';
                        }
                    }
                    let txt = root.innerText || root.textContent || '';
                    txt = txt.replace(/\n{3,}/g, '\n\n').replace(/[ \t]+/g, ' ').trim();
                    return txt;
                } catch (e) { return ''; }
            })()
        """
        page = ""
        try:
            page = await session.evaluate(markdown_js)
        except Exception:
            page = ""
        if not page:
            page = await session.page_text(max_chars + start_from_char + 1000)
        if not page or not page.strip():
            return "(page is empty — cannot extract)"
        if start_from_char > 0:
            if start_from_char >= len(page):
                return f"(start_from_char={start_from_char} is past end of page text len={len(page)})"
            page = page[start_from_char : start_from_char + max_chars]
        elif len(page) > max_chars:
            page = page[:max_chars]

        schema_clause = ""
        if output_schema_hint and output_schema_hint.strip():
            schema_clause = (
                f"\n\nRETURN FORMAT: respond with a JSON object/array "
                f"matching this shape exactly (use real values from the "
                f"page, do not echo the placeholder types):\n"
                f"{output_schema_hint.strip()}"
            )

        # Optional augmentation (v0.7.2): pull links/images from the
        # actual DOM and append. Cheaper than a separate
        # extract_links/extract_images call when the agent needs the
        # answer to incorporate them. Mirrors upstream's
        # extract_links/extract_images flags.
        extras = ""
        if extract_links:
            try:
                links = await session.get_links()
                if links:
                    # v0.8.17: same href/text swap fix as extract_links above.
                    extras += "\n\nLINKS:\n" + "\n".join(
                        f"- {(t or '').strip()[:80]} -> {h}"
                        for h, t in links[:80]
                    )
            except Exception:
                pass
        if extract_images:
            try:
                imgs_js = (
                    "(() => { const out=[]; for (const img of document.querySelectorAll('img')){"
                    " const r=img.getBoundingClientRect(); if(r.width<32||r.height<32) continue;"
                    " const src=img.src||''; if(src.startsWith('data:')||!src) continue;"
                    " out.push({alt:(img.alt||img.title||'').trim(), src:src.slice(0,200)}); if(out.length>40) break; }"
                    " return JSON.stringify(out); })()"
                )
                imgs_raw = await session.evaluate(imgs_js)
                imgs_data = json.loads(imgs_raw) if imgs_raw else []
                if imgs_data:
                    extras += "\n\nIMAGES:\n" + "\n".join(
                        f"- {(d.get('alt') or '(no alt)')[:60]} -> {d['src']}"
                        for d in imgs_data
                    )
            except Exception:
                pass

        already_clause = ""
        if already_collected and already_collected.strip():
            already_clause = (
                f"\n\nITEMS ALREADY COLLECTED (do NOT repeat in your "
                f"answer; find NEW ones):\n{already_collected.strip()[:2000]}"
            )

        # v0.8.12: split into proper SystemMessage + UserMessage. Was
        # passing the whole instruction block as a single user message
        # with `system=None`. SystemMessage is treated with much higher
        # authority by every provider — that change alone tends to make
        # the extractor obey "don't hallucinate, list ALL items" much
        # more reliably. System prompt mirrors upstream's free-text
        # extraction prompt at tools/service.py:1196-1216 with our
        # additions (schema hint + already-collected) appended.
        extraction_system = (
            "You are an expert at extracting data from the markdown of "
            "a webpage.\n\n"
            "<input>\n"
            "You will be given a query and the markdown of a webpage "
            "that has been filtered to remove noise and advertising "
            "content.\n"
            "</input>\n\n"
            "<instructions>\n"
            "- Extract ONLY information available in the webpage that "
            "is relevant to the query. Do NOT make up information or "
            "guess from your own knowledge.\n"
            "- If the relevant information is not available in the "
            "page, your response should mention that — reply exactly "
            "NOT FOUND.\n"
            "- If the query asks for ALL items, products, etc., make "
            "sure to directly list ALL of them — do not summarize or "
            "pick just a few unless the query specifies a count.\n"
            "- If the content was truncated and you need more, note "
            "that the user can use start_from_char to continue from "
            "where truncation occurred.\n"
            "- If <already_collected> items are provided, exclude any "
            "results whose name/title/URL matches those — do not "
            "include duplicates.\n"
            "</instructions>\n\n"
            "<output>\n"
            "- Present ALL information relevant to the query in a "
            "concise way.\n"
            "- Do NOT answer in conversational format. Directly output "
            "the relevant information, or NOT FOUND if unavailable.\n"
            "- No preamble, no explanation, no markdown formatting "
            "unless the answer requires structure.\n"
            "</output>"
        )
        extraction_user = (
            f"<query>\n{query}\n</query>\n\n"
            f"<page_url>\n{url}\n</page_url>\n\n"
            f"<page_offset>{start_from_char}</page_offset>"
            + schema_clause + already_clause + "\n\n"
            f"<webpage_content>\n{page}{extras}\n</webpage_content>"
        )

        try:
            from browser_use_rs.llm.base import UserMessage
            messages = [UserMessage(content=extraction_user)]
            # Use page_extraction_llm if the consumer set one (mirrors
            # upstream's separate cheap-extraction-LLM pattern). Falls
            # back to the agent's main LLM. v0.7.0.
            extract_llm = getattr(agent, "page_extraction_llm", None) or agent.llm
            completion = await asyncio.wait_for(
                extract_llm.ainvoke(messages, [], system=extraction_system),
                timeout=getattr(agent, "tool_timeout", 60.0),
            )
            # v0.8.15: account for the extractor LLM call. Without this,
            # 5-15K input tokens × N extracts/task were silently missing
            # from usage_log → step_metadata.input_tokens → eval framework's
            # tokensUsed → dashboard total_cost. The eval framework reads
            # the SUM across step_metadata, so adding to history.usage
            # also flows through. Wrapped in try/except so a missing
            # _record_usage attr (older Agent shape) doesn't break extract.
            try:
                if completion.usage is not None:
                    agent._record_usage(agent.state.n_steps, completion.usage)
            except Exception:
                pass
            text = (completion.text or "").strip()
            if not text:
                return "(extractor returned empty response)"
            # v0.9.7 — file-system overflow for long extracts.
            # Mirrors upstream's tools/service.py:1175-1182 pattern.
            # Long extract results bloat the agent's context turn-by-
            # turn (read-tool turns stay native indefinitely in our
            # collapse policy). Spilling >10k-char results to a sandbox
            # file and returning a one-line reference cuts ~5-30k
            # tokens of cumulative context per long-list / long-doc
            # task. The agent reads the file via read_file when it
            # needs the full content, otherwise just keeps the
            # reference in history. Targets the cost gap vs upstream
            # (their extractor has the same overflow).
            EXTRACT_OVERFLOW_THRESHOLD = 10000
            if len(text) > EXTRACT_OVERFLOW_THRESHOLD:
                try:
                    # Sandbox path — agent has read access via read_file
                    extracts_dir = os.path.join(sandbox, "extracts")
                    os.makedirs(extracts_dir, exist_ok=True)
                    # Stable name per (query, fingerprint) so re-asking the
                    # same question doesn't pile up files.
                    name_hash = hashlib.md5(
                        f"{query}|{page_fingerprint}|{start_from_char}".encode()
                    ).hexdigest()[:10]
                    fname = f"extract_{name_hash}.txt"
                    fpath = os.path.join(extracts_dir, fname)
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(text)
                    preview = text[:1500].rstrip()
                    overflow_msg = (
                        f"{preview}\n\n"
                        f"[EXTRACT OVERFLOW — {len(text):,} chars total, "
                        f"first 1500 shown above. Full content saved to "
                        f"`extracts/{fname}` — call `read_file(\"extracts/"
                        f"{fname}\")` if you need the rest.]"
                    )
                    extract_cache[cache_key] = overflow_msg
                    return overflow_msg
                except Exception as e:
                    # Sandbox write failed — degrade to returning the
                    # full text (pre-v0.9.7 behaviour).
                    logger_inner = __import__("logging").getLogger(__name__)
                    logger_inner.info(
                        "extract: file overflow write failed (%s), "
                        "falling back to full inline return", e,
                    )
            extract_cache[cache_key] = text  # cache for dedup
            return text
        except asyncio.TimeoutError:
            return "(extractor timed out — try a narrower query)"
        except Exception as e:
            return f"(extractor failed: {type(e).__name__}: {e})"

    # ---------- v0.8.27: always-available done(text, success) tool ----
    # Codex-recommended addition. The plain "no tool calls = done"
    # heuristic worked but was implicit; this gives the LLM an explicit
    # finalize action AND lets us wedge a Top-N count-check guard
    # between the LLM saying "done" and the runtime committing the
    # answer. Targets the dominant failure pattern (17 tasks where the
    # agent over-claimed completion with too few list items).
    #
    # Skip registration if a Controller already added `done`
    # (structured-output mode) — that one carries a payload schema and
    # the loop has the corresponding parser. Adding our duplicate would
    # confuse the LLM about which to call.

    @tool
    async def done(session, text: str, success: bool = True) -> str:
        """Commit your final answer. Call this when the task is complete
        (or unrecoverable) — your `text` becomes the final answer the
        judge sees, and the agent loop terminates.

        Args:
            text: The final answer in plain text. For list/Top-N tasks
                ("list top 3 headlines"), include EXACTLY N distinct
                items in the requested order. If fewer than N matching
                items were available on the page, include only those
                and state explicitly that the page showed only M items.
            success: True if you completed the task and your answer is
                correct based on observed page evidence. False if you
                were blocked, the data wasn't available, or you're
                unsure.
        """
        # Top-N count-check guard. Fires AT MOST ONCE per task so we
        # don't spin if the LLM genuinely cannot find more items —
        # codex's design: nudge once, then trust the agent.
        already_fired = getattr(agent, "_done_count_check_fired", False)
        if success and not already_fired:
            n_required = _parse_required_count(agent.task or "")
            if n_required is not None and n_required >= 2:
                n_found = _count_items_in_answer(text)
                # Only nudge when SIGNIFICANTLY short — < ceil(N/2). A
                # 3-of-5 partial is plausible; 1-of-5 is suspicious. Also
                # skip if the agent already explicitly acknowledged
                # partial coverage in the text (avoids double-prompting
                # honest "only M available" answers).
                acknowledges_partial = bool(
                    re.search(
                        r"\b(only|just|fewer than|less than|less|partial)\b.*"
                        r"\b(item|result|article|headline|entry|game|review|"
                        r"deal|product|listing|video|press release|"
                        r"available|matching|found)\b",
                        text,
                        re.IGNORECASE,
                    )
                ) or bool(
                    re.search(
                        r"\b(showed|returned|displayed|had|contained)\b\s+"
                        r"only\s+\d+",
                        text,
                        re.IGNORECASE,
                    )
                )
                if (
                    n_found is not None
                    and n_found < (n_required + 1) // 2
                    and not acknowledges_partial
                ):
                    agent._done_count_check_fired = True
                    return (
                        f"[DONE_COUNT_CHECK] The task asks for "
                        f"{n_required} items but your answer appears "
                        f"to contain only {n_found} list item(s). "
                        f"Either:\n"
                        f"  (a) extract more items from the page (call "
                        f"extract_structured_data or scroll to reveal "
                        f"more), OR\n"
                        f"  (b) call done() again, including in your "
                        f"text the explicit phrase 'the page showed "
                        f"only X matching items' so the judge knows "
                        f"this is a verified-partial answer, not an "
                        f"oversight.\n"
                        f"This guard fires once — your next done() "
                        f"call will commit whatever you submit."
                    )
        # Encode for the agent loop's existing __DONE__ parser
        # (agent/__init__.py: ~line 1369). The success flag must be 0
        # or 1; payload follows the second colon.
        return f"__DONE__:{int(bool(success))}:{text}"

    return [
        extract_structured_data,
        read_file,
        write_file,
        replace_file_str,
        list_files,
        done,
    ]


# ---------------------------------------------------------------------------
# v0.8.27 Top-N parsing helpers (used by the always-available done tool).
# Module-level so unit tests can hit them without spinning up an Agent.
# ---------------------------------------------------------------------------

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

_COUNT_PATTERNS = [
    # "top 3", "top three", "first 5", "first five", "next 3"
    re.compile(
        r"\b(?:top|first|next|last|latest|recent)\s+"
        r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
        re.IGNORECASE,
    ),
    # "list 5 articles", "list five entries"
    re.compile(
        r"\blist\s+"
        r"(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+"
        r"(?:item|article|headline|result|entry|game|review|deal|"
        r"product|listing|video|press release|recipe|paragraph|"
        r"option|topic|tour|community|hashtag|name|definition|fee|"
        r"address|database|paper|recommendation|advisory|step)",
        re.IGNORECASE,
    ),
    # "the 3 most recent", "the five highest"
    re.compile(
        r"\bthe\s+(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+"
        r"(?:most|highest|lowest|featured|recent|latest|top)",
        re.IGNORECASE,
    ),
    # "5 (most recent|featured|highest|...)" without leading "the"
    re.compile(
        r"\b(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+"
        r"(?:most|featured|highest|lowest)\s+\w+",
        re.IGNORECASE,
    ),
]


def _parse_required_count(task: str) -> int | None:
    """Extract N from task text when the task asks for N items.

    Returns None when the task isn't a recognisable Top-N pattern.
    Conservative on purpose — false positives cost the agent a
    spurious nudge turn, so we only fire when the pattern is clearly
    "give me N of something".
    """
    if not task:
        return None
    for pat in _COUNT_PATTERNS:
        m = pat.search(task)
        if m:
            tok = m.group(1).lower()
            if tok.isdigit():
                n = int(tok)
            else:
                n = _NUMBER_WORDS.get(tok)
            # Cap sanity: tasks asking for 50+ items aren't really
            # Top-N, they're "extract everything" — skip the guard.
            if n is not None and 1 <= n <= 20:
                return n
    return None


def _count_items_in_answer(text: str) -> int | None:
    """Heuristic count of distinct list items in a final-answer string.

    Returns None when no recognisable list structure was detected (so
    the count check skips and we don't misfire on prose answers).
    """
    if not text:
        return 0
    # Numbered lines like "1.", "2)", "1:", at start of a line/segment.
    numbered = len(
        re.findall(r"(?:^|\n)\s*\d+[.)\]:](?:\s|\*)", text)
    )
    # Bulleted lines: -, *, •, ·, — followed by space.
    bulleted = len(
        re.findall(r"(?:^|\n)\s*[-*•·—](?:\s|\*)", text)
    )
    n = max(numbered, bulleted)
    if n >= 2:
        return n
    # Fallback: bold-prefixed enumerations like "**Title:**" — common
    # gemini-flash output style for list items.
    bolded = len(re.findall(r"\*\*[^*\n]{2,80}:\*\*", text))
    if bolded >= 2:
        return bolded
    return None  # no recognisable list shape — skip the guard


# Stateless tools as a separate list — agent merges these with the
# stateful ones via make_extra_tools.
EXTRA_STATELESS_TOOLS = [
    search_page,
    find_elements,
    find_text,
    get_dropdown_options,
    select_dropdown,
    send_keys,
    go_back,
    evaluate_js,
    web_search,        # v0.6.5
    extract_links,     # v0.6.5
    extract_images,    # v0.6.5
]
