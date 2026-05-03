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
import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any

from browser_use_rs.tools import tool


# ---------------------------------------------------------------------------
# v0.9.0 — structure-aware markdown chunking for extract_structured_data.
# Module-level so tests can hit it without spinning up an Agent. Codex-
# reviewed design; targets the dominant Top-N failure pattern (items 6-10
# of a long list landing in chunk 2 without context).
# ---------------------------------------------------------------------------


@dataclass
class _MdBlock:
    """One semantic block in the cleaned markdown stream."""
    kind: str   # 'heading' | 'list' | 'table' | 'paragraph' | 'fence' | 'rule'
    text: str   # raw block text (no surrounding blank lines)
    level: int = 0  # heading level (1..6) or 0 for non-headings
    src_start: int = 0  # byte offset of block start in original source md
    src_end: int = 0    # byte offset of block end (exclusive)


@dataclass
class _MdChunk:
    """A bounded slice of the markdown plus the context-prefix to render."""
    content: str
    overlap_prefix: str
    char_offset_start: int
    char_offset_end: int
    chunk_index: int
    total_chunks: int
    has_more: bool


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:-]+\|[\s:|-]+\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _parse_md_blocks(md: str) -> list[_MdBlock]:
    """Split cleaned markdown into semantic blocks.

    Greedy paragraph-style parsing — each block is contiguous lines of
    a single kind, with blank lines as separators. Headings are always
    standalone (one block per heading line). Lists and tables consume
    consecutive lines of the same kind.

    Each block records its byte offsets in the *source* markdown
    (src_start/src_end) so start_from_char pagination indexes into
    real source text rather than a reconstructed approximation.
    """
    if not md:
        return []
    lines = md.splitlines(keepends=True)  # keep '\n' so cumulative offsets match source
    line_starts: list[int] = []
    pos = 0
    for line in lines:
        line_starts.append(pos)
        pos += len(line)
    line_starts.append(pos)  # sentinel for end-of-source

    blocks: list[_MdBlock] = []
    i = 0
    n = len(lines)

    def _block_text(start_idx: int, end_idx: int) -> str:
        # Strip trailing newline on each line — the renderer joins with
        # '\n' so we don't want double-line endings inside block.text.
        return "\n".join(lines[k].rstrip("\n") for k in range(start_idx, end_idx))

    def _src_range(start_idx: int, end_idx: int) -> tuple[int, int]:
        # End offset = start of the line AFTER the last block line, but
        # WITHOUT the trailing newline (so consecutive blocks don't overlap
        # on source-newline characters). Trim trailing whitespace.
        s = line_starts[start_idx]
        e = line_starts[end_idx]
        # Walk back over trailing newlines so end is exclusive of separator.
        while e > s and md[e - 1] in "\n\r":
            e -= 1
        return s, e

    while i < n:
        line = lines[i].rstrip("\n")
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        # Code fence — capture until closing fence (or EOF as fallback).
        if _FENCE_RE.match(line):
            start = i
            i += 1
            while i < n and not _FENCE_RE.match(lines[i].rstrip("\n")):
                i += 1
            if i < n:
                i += 1  # consume closing fence
            s, e = _src_range(start, i)
            blocks.append(_MdBlock(kind="fence", text=_block_text(start, i), src_start=s, src_end=e))
            continue
        # Heading.
        m = _HEADING_RE.match(line)
        if m:
            s, e = _src_range(i, i + 1)
            blocks.append(
                _MdBlock(kind="heading", text=line.rstrip(), level=len(m.group(1)),
                         src_start=s, src_end=e)
            )
            i += 1
            continue
        # Horizontal rule (--- / *** / ___).
        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            s, e = _src_range(i, i + 1)
            blocks.append(_MdBlock(kind="rule", text=line.rstrip(), src_start=s, src_end=e))
            i += 1
            continue
        # Table — header line then separator then body rows.
        if _TABLE_LINE_RE.match(line):
            start = i
            while i < n and _TABLE_LINE_RE.match(lines[i].rstrip("\n")):
                i += 1
            s, e = _src_range(start, i)
            blocks.append(_MdBlock(kind="table", text=_block_text(start, i), src_start=s, src_end=e))
            continue
        # List — consecutive list lines (allow nested indentation).
        if _LIST_LINE_RE.match(line):
            start = i
            while i < n:
                cur = lines[i].rstrip("\n")
                if _LIST_LINE_RE.match(cur):
                    i += 1
                elif cur.startswith((" ", "\t")) and cur.strip():
                    # continuation of a list item
                    i += 1
                else:
                    break
            s, e = _src_range(start, i)
            blocks.append(_MdBlock(kind="list", text=_block_text(start, i), src_start=s, src_end=e))
            continue
        # Paragraph — non-empty lines until a blank line or block-start.
        start = i
        while i < n:
            cur = lines[i].rstrip("\n")
            if not cur.strip():
                break
            if (
                _HEADING_RE.match(cur)
                or _LIST_LINE_RE.match(cur)
                or _TABLE_LINE_RE.match(cur)
                or _FENCE_RE.match(cur)
            ):
                break
            i += 1
        s, e = _src_range(start, i)
        blocks.append(_MdBlock(kind="paragraph", text=_block_text(start, i), src_start=s, src_end=e))
    return blocks


def _table_header(table_text: str) -> str | None:
    """Return 'header_row + separator_row' from a table block, or None."""
    lines = table_text.splitlines()
    if len(lines) < 2:
        return None
    if _TABLE_SEP_RE.match(lines[1]):
        return "\n".join(lines[:2])
    return None


def _hard_cut_fragments(b: _MdBlock, max_chars: int) -> list[_MdBlock]:
    """Last-resort: char-cut a block whose text > max_chars into
    pieces of exactly max_chars. Source offsets are derived from the
    parent block (best-effort: we project sub-piece char-positions
    proportionally onto the parent's src_start..src_end range).
    """
    if len(b.text) <= max_chars:
        return [b]
    out: list[_MdBlock] = []
    parent_span = max(1, b.src_end - b.src_start)
    text_len = max(1, len(b.text))
    for i in range(0, len(b.text), max_chars):
        chunk_text = b.text[i : i + max_chars]
        # Project text-relative offsets onto source-relative offsets.
        sub_start = b.src_start + (i * parent_span) // text_len
        sub_end = b.src_start + ((i + len(chunk_text)) * parent_span) // text_len
        out.append(
            _MdBlock(
                kind=b.kind, text=chunk_text, level=b.level,
                src_start=sub_start, src_end=sub_end,
            )
        )
    return out


def _split_oversized_block(b: _MdBlock, max_chars: int) -> list[_MdBlock]:
    """Sub-split a block that exceeds max_chars into smaller blocks.

    Lists are split at item boundaries (each `- ` / `1.` line begins a
    new fragment), preserving list-kind. Tables are split at row
    boundaries with the header+separator carried into every fragment.
    Paragraphs and fences fall back to line-grouping. Headings/rules
    are tiny by definition; left untouched.

    All paths feed any still-oversized fragment through
    ``_hard_cut_fragments`` as a last resort, so every returned block
    is bounded by max_chars (codex must-fix #4: list items longer
    than max_chars used to escape this guarantee).
    """
    if len(b.text) <= max_chars:
        return [b]
    if b.kind in ("heading", "rule"):
        return [b]
    lines = b.text.splitlines()
    parent_span = max(1, b.src_end - b.src_start)
    text_len = max(1, len(b.text))

    def _make(kind: str, text: str, text_offset_in_parent: int) -> _MdBlock:
        # Derive source range from the position of `text` inside the
        # parent block. text_offset_in_parent is the char-offset of
        # this fragment's first char within b.text.
        sub_start = b.src_start + (text_offset_in_parent * parent_span) // text_len
        sub_end = b.src_start + ((text_offset_in_parent + len(text)) * parent_span) // text_len
        return _MdBlock(kind=kind, text=text, src_start=sub_start, src_end=sub_end)

    if b.kind == "table":
        hdr_lines = lines[:2] if len(lines) >= 2 and _TABLE_SEP_RE.match(lines[1]) else []
        body = lines[len(hdr_lines):]
        out: list[_MdBlock] = []
        cur: list[str] = list(hdr_lines)
        cur_chars = sum(len(x) + 1 for x in cur)
        cur_text_offset = 0  # offset of first body row in parent
        body_offset_in_parent = sum(len(x) + 1 for x in hdr_lines)
        offset_cursor = body_offset_in_parent
        for row in body:
            row_size = len(row) + 1
            if cur_chars + row_size > max_chars and cur != hdr_lines:
                frag_text = "\n".join(cur)
                out.append(_make("table", frag_text, cur_text_offset))
                cur_text_offset = offset_cursor
                cur = list(hdr_lines) + [row]
                cur_chars = sum(len(x) + 1 for x in cur)
            else:
                cur.append(row)
                cur_chars += row_size
            offset_cursor += row_size
        if cur and cur != hdr_lines:
            frag_text = "\n".join(cur)
            out.append(_make("table", frag_text, cur_text_offset))
        # Apply hard-cut fallback to any fragment that's still too big
        # (e.g. a single body row with a massive cell).
        final: list[_MdBlock] = []
        for f in out:
            final.extend(_hard_cut_fragments(f, max_chars))
        return final or [b]

    if b.kind == "list":
        out = []
        cur = []
        cur_chars = 0
        cur_text_offset = 0
        offset_cursor = 0
        for line in lines:
            line_size = len(line) + 1
            is_item_start = bool(_LIST_LINE_RE.match(line))
            if cur and is_item_start and cur_chars + line_size > max_chars:
                frag_text = "\n".join(cur)
                out.append(_make("list", frag_text, cur_text_offset))
                cur_text_offset = offset_cursor
                cur = [line]
                cur_chars = line_size
            else:
                cur.append(line)
                cur_chars += line_size
            offset_cursor += line_size
        if cur:
            frag_text = "\n".join(cur)
            out.append(_make("list", frag_text, cur_text_offset))
        # Hard-cut any list fragment that's still > max_chars (one big
        # item with continuation lines). Codex must-fix #4.
        final: list[_MdBlock] = []
        for f in out:
            final.extend(_hard_cut_fragments(f, max_chars))
        return final or [b]

    # paragraph / fence / fallback — line-group with hard char cap.
    out = []
    cur = []
    cur_chars = 0
    cur_text_offset = 0
    offset_cursor = 0
    for line in lines:
        line_size = len(line) + 1
        if cur and cur_chars + line_size > max_chars:
            frag_text = "\n".join(cur)
            out.append(_make(b.kind, frag_text, cur_text_offset))
            cur_text_offset = offset_cursor
            cur = [line]
            cur_chars = line_size
        else:
            cur.append(line)
            cur_chars += line_size
        offset_cursor += line_size
    if cur:
        frag_text = "\n".join(cur)
        out.append(_make(b.kind, frag_text, cur_text_offset))
    final: list[_MdBlock] = []
    for f in out:
        final.extend(_hard_cut_fragments(f, max_chars))
    return final or [b]


def _build_overlap_prefix(
    heading_stack: list[tuple[int, str]],
    last_table_header: str | None,
    cap_chars: int,
    chunk_content: str = "",
) -> str:
    """Render the context prefix that precedes a continuation chunk.

    Includes the active heading hierarchy and (when relevant) the
    header row of a table whose body is continuing into this chunk.
    Capped so the prefix can't dominate the chunk.

    Codex must-fix #3: when the chunk's content already starts with
    the table header (because _split_oversized_block carries it into
    every fragment), don't duplicate it as overlap_prefix.
    """
    if not heading_stack and not last_table_header:
        return ""
    parts: list[str] = []
    if heading_stack:
        parts.append(
            "\n".join(f"{'#' * lvl} {text}" for lvl, text in heading_stack)
        )
    # Skip table header in the prefix if the chunk already opens with it.
    if last_table_header and not chunk_content.lstrip().startswith(
        last_table_header.lstrip()
    ):
        parts.append(last_table_header)
    pre = "\n\n".join(parts)
    if len(pre) > cap_chars:
        pre = pre[:cap_chars] + "..."
    return pre


def chunk_markdown_by_structure(
    md: str,
    max_chunk_chars: int,
    start_from_char: int = 0,
) -> list[_MdChunk]:
    """Split markdown into structure-aware chunks.

    Each chunk respects ``max_chunk_chars`` but breaks at block
    boundaries (heading / list / table / paragraph) — never mid-line.
    Continuation chunks get an ``overlap_prefix`` containing the
    parent heading stack and (for table continuations) the table
    header + separator row, so the LLM sees what context items
    belong to.

    ``start_from_char`` indexes into the *source* markdown; the
    returned chunk starts at the first whole block whose source
    range overlaps that offset.
    """
    raw_blocks = _parse_md_blocks(md)
    if not raw_blocks:
        return []

    # Sub-split any block that exceeds max_chunk_chars on its own
    # (long lists, big tables, single huge paragraphs). Without this
    # the chunker would emit oversized chunks because greedy grouping
    # only triggers when ADDING a block would overflow — a block that
    # ALREADY overflows just gets emitted whole. Sub-splitting at
    # natural boundaries (list items, table rows, lines) preserves
    # readability and keeps each output chunk bounded. Source offsets
    # propagate from the parent block.
    blocks: list[_MdBlock] = []
    for rb in raw_blocks:
        blocks.extend(_split_oversized_block(rb, max_chunk_chars))

    # Skip blocks that end before start_from_char (using SOURCE offsets,
    # codex must-fix #2).
    skip_until = 0
    if start_from_char > 0:
        for i, b in enumerate(blocks):
            if b.src_end > start_from_char:
                skip_until = i
                break
        else:
            return []

    # Group blocks into chunks. Track heading stack + last table header
    # as we go so each chunk knows its context-prefix.
    chunks: list[_MdChunk] = []
    cur_blocks: list[_MdBlock] = []
    cur_chars = 0
    cur_start_off = blocks[skip_until].src_start if skip_until < len(blocks) else 0
    heading_stack: list[tuple[int, str]] = []
    last_table_header: str | None = None
    prefix_cap = max(200, min(1500, int(max_chunk_chars * 0.15)))
    # Track whether we're starting from a continuation point (codex
    # must-fix #1). When start_from_char > 0, even the FIRST returned
    # chunk is a continuation and needs its prefix.
    is_continuation_start = start_from_char > 0

    # Pre-walk to find the heading stack as it stood at skip_until.
    for b in blocks[:skip_until]:
        if b.kind == "heading":
            heading_stack = [h for h in heading_stack if h[0] < b.level]
            heading_stack.append((b.level, b.text.lstrip("#").strip()))
        elif b.kind == "table":
            hdr = _table_header(b.text)
            if hdr is not None:
                last_table_header = hdr

    def _flush(end_offset: int) -> None:
        nonlocal cur_blocks, cur_chars, cur_start_off
        if not cur_blocks:
            return
        content = "\n\n".join(b.text for b in cur_blocks)
        # Render prefix if this isn't the document's first chunk OR if
        # we're starting from a pagination continuation point.
        needs_prefix = bool(chunks) or is_continuation_start
        prefix = (
            _build_overlap_prefix(
                heading_stack_snapshot, last_table_header_snapshot,
                prefix_cap, chunk_content=content,
            )
            if needs_prefix
            else ""
        )
        chunks.append(
            _MdChunk(
                content=content,
                overlap_prefix=prefix,
                char_offset_start=cur_start_off,
                char_offset_end=end_offset,
                chunk_index=len(chunks),
                total_chunks=0,  # patched after loop
                has_more=False,  # patched after loop
            )
        )
        cur_blocks = []
        cur_chars = 0
        cur_start_off = end_offset

    # Snapshots captured AT chunk-flush time so the prefix reflects
    # the state when the chunk *starts*, not the state at parse end.
    heading_stack_snapshot: list[tuple[int, str]] = list(heading_stack)
    last_table_header_snapshot: str | None = last_table_header

    for i in range(skip_until, len(blocks)):
        b = blocks[i]
        size = len(b.text)
        sep = 2 if cur_blocks else 0  # "\n\n" between blocks within a chunk
        if cur_chars + sep + size > max_chunk_chars and cur_blocks:
            _flush(end_offset=b.src_start)
            heading_stack_snapshot = list(heading_stack)
            last_table_header_snapshot = last_table_header
            cur_start_off = b.src_start
        cur_blocks.append(b)
        cur_chars += sep + size
        # Update tracking state AFTER appending so the prefix snapshot
        # captures what was active before this chunk grew.
        if b.kind == "heading":
            heading_stack = [h for h in heading_stack if h[0] < b.level]
            heading_stack.append((b.level, b.text.lstrip("#").strip()))
        elif b.kind == "table":
            hdr = _table_header(b.text)
            if hdr is not None:
                last_table_header = hdr

    if cur_blocks:
        _flush(end_offset=blocks[-1].src_end)

    # Post-pass: merge a tiny chunk (just a heading or two, < 120 chars)
    # into the next one when their combined size doesn't exceed
    # max_chunk_chars * 1.25. Avoids "stranded heading" chunks that
    # waste a tool-call slot. The 25% over-budget tolerance is the
    # smaller evil compared to forcing the agent to paginate just to
    # get past 'a heading'.
    if len(chunks) > 1:
        merged: list[_MdChunk] = []
        i = 0
        while i < len(chunks):
            cur = chunks[i]
            if (
                i + 1 < len(chunks)
                and len(cur.content) < 120
                and len(cur.content) + len(chunks[i + 1].content) + 2
                <= int(max_chunk_chars * 1.25)
            ):
                nxt = chunks[i + 1]
                cur = _MdChunk(
                    content=cur.content + "\n\n" + nxt.content,
                    overlap_prefix=cur.overlap_prefix,  # keep the EARLIER prefix
                    char_offset_start=cur.char_offset_start,
                    char_offset_end=nxt.char_offset_end,
                    chunk_index=0,
                    total_chunks=0,
                    has_more=False,
                )
                i += 2
            else:
                i += 1
            merged.append(cur)
        chunks = merged

    total = len(chunks)
    for i, c in enumerate(chunks):
        c.chunk_index = i
        c.total_chunks = total
        c.has_more = i < total - 1
    return chunks


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
    return f"opened {eng} results for: {query}"


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
    async def read_file(session, path: str) -> str:
        """Read a file from the agent's sandbox directory. Use to recall
        notes, partial extractions, or todo items written earlier in the
        run.

        Args:
            path: Relative path in the agent sandbox (e.g. "notes.md").
        """
        full = _resolve(path)
        if not full or not os.path.isfile(full):
            return f"(no such file: {path})"
        try:
            with open(full, "r", encoding="utf-8") as f:
                return f.read()[:50000]
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

        Pagination (v0.9.0 structure-aware): when a page exceeds
        max_chars the extractor splits at heading / list / table /
        paragraph boundaries (NOT mid-line). Each result tells you
        `chunk X of Y` and gives a `next_start_char` to continue with.
        Continuation chunks include the parent heading stack as a
        prefix so items 6-10 of a list don't lose their context.
        For Top-N tasks crossing chunks, pass `already_collected`
        with the items from prior chunks so the extractor doesn't
        repeat them.

        Structured output: pass output_schema_hint with a JSON-like
        sketch of the format you want. The extractor will try to match
        that shape. Example:
          output_schema_hint='{"products": [{"name": str, "price": str}]}'

        Args:
            query: What to extract. Be specific.
            max_chars: Max page text per chunk. Default 30000.
            start_from_char: Source-text offset to start reading from
                (for paged long pages). Default 0. Pass the
                `next_start_char` value from a prior call to continue.
            output_schema_hint: Optional JSON-like template the answer
                should follow. Default empty (free-form text answer).
            already_collected: Comma- or newline-separated identifiers
                (titles/names/URLs) from prior chunks of the same
                page. The extractor will skip duplicates.
        """
        # Per-task dedup cache (v0.9.0): keyed on the actual chunk
        # selection, not a coarse char-bucket. Disable when
        # already_collected is in play — that param is meant to drive
        # different output across calls and the cache would mask it.
        try:
            url = await session.current_url()
        except Exception:
            url = ""
        cache_disabled = bool(already_collected and already_collected.strip())

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

        # v0.9.2 (codex bisection): two paths based on page size.
        #
        # FAST PATH (short page, no pagination): bypass the chunker
        # entirely and use the EXACT pre-v0.9.0 prompt + cache shape.
        # This is the dominant case on this benchmark (most pages are
        # <30k chars, fit in one chunk). v0.9.0 added chunker codepath
        # for ALL pages — even ones that didn't need it — and judge
        # regressed -5 vs v0.8.30 mean despite operational gains.
        # Codex hypothesis: the parser-rejoin or prompt-restructure
        # changed the LLM's input on short pages even when chunker
        # was a no-op. Fast path eliminates that variable.
        #
        # CHUNKER PATH (long page or paginating): full v0.9.0 logic
        # — structure-aware chunks, overlap_prefix, content_stats
        # envelope. Targets the Top-N tasks where chunker is
        # genuinely needed.
        full_md = page  # full markdown text, regardless of path
        use_chunker_path = (
            start_from_char > 0 or len(full_md) > max_chars
        )

        if not use_chunker_path:
            # FAST PATH — exact pre-v0.9.0 behavior on the prompt
            # side (legacy cache key, no content_stats envelope, raw
            # markdown).
            sel_chunk = None
            content_stats: dict[str, Any] = {}
            page_fingerprint = ""
            # Legacy cache key from v0.7.0: (url, query, offset_bucket).
            # offset is always 0 on the fast path so the bucket is 0.
            cache_key = (url, query.strip(), 0)
            # page already == full_md; nothing to slice.
        else:
            # CHUNKER PATH — v0.9.0+ structure-aware
            chunks = chunk_markdown_by_structure(
                full_md, max_chunk_chars=max_chars, start_from_char=start_from_char,
            )
            if not chunks:
                return (
                    f"(start_from_char={start_from_char} past end of page "
                    f"text len={len(full_md)})"
                )
            sel_chunk = chunks[0]
            if sel_chunk.overlap_prefix:
                page = sel_chunk.overlap_prefix + "\n\n" + sel_chunk.content
            else:
                page = sel_chunk.content

            content_stats = {
                "total_markdown_chars": len(full_md),
                "chunk_index": sel_chunk.chunk_index,
                "total_chunks": sel_chunk.total_chunks,
                "chunk_chars": len(sel_chunk.content),
                "char_offset_start": sel_chunk.char_offset_start,
                "char_offset_end": sel_chunk.char_offset_end,
            }
            if sel_chunk.has_more:
                content_stats["next_start_char"] = sel_chunk.char_offset_end
                content_stats["truncated_at_char"] = sel_chunk.char_offset_end

            # v0.9.0 cache key tied to the actual chunk selection.
            page_fingerprint = hashlib.md5(
                full_md[:2000].encode("utf-8")
            ).hexdigest()[:12]
            cache_key = (
                url, page_fingerprint, query.strip(), max_chars,
                sel_chunk.chunk_index, extract_links, extract_images,
            )

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

        # Cache lookup — key was built per-path above.
        if not cache_disabled and cache_key in extract_cache:
            return f"(cached) {extract_cache[cache_key]}"

        already_clause = ""
        if already_collected and already_collected.strip():
            already_clause = (
                f"\n\nITEMS ALREADY COLLECTED (do NOT repeat in your "
                f"answer; find NEW ones):\n{already_collected.strip()[:2000]}"
            )

        # content_stats envelope only on the chunker path AND when
        # total_chunks > 1 (v0.9.1 gating). On the fast path,
        # stats_clause stays empty so the prompt matches pre-v0.9.0.
        if sel_chunk is not None and sel_chunk.total_chunks > 1:
            stats_lines = [
                f"chunk {sel_chunk.chunk_index + 1} of {sel_chunk.total_chunks}",
                f"page total: {content_stats['total_markdown_chars']:,} chars",
                f"this chunk: {content_stats['chunk_chars']:,} chars (source [{content_stats['char_offset_start']:,}..{content_stats['char_offset_end']:,}])",
            ]
            if sel_chunk.has_more:
                stats_lines.append(
                    f"MORE CONTENT BELOW: call extract_structured_data again with start_from_char={content_stats['next_start_char']} to read the next chunk"
                )
            else:
                stats_lines.append("(this is the LAST chunk of the page)")
            stats_clause = (
                "\n\n<content_stats>\n" + "\n".join(stats_lines) + "\n</content_stats>"
            )
        else:
            stats_clause = ""

        # Telemetry for v0.9.2 bisection (codex-requested). Logs the
        # path taken AND content hashes so trace analysis can answer
        # "did this task hit the fast path or chunker path? did the
        # markdown content change between v0.9.x ships?".
        try:
            import logging as _lg
            md_hash = hashlib.md5(full_md.encode("utf-8")).hexdigest()[:8] if full_md else "-"
            _lg.getLogger(__name__).info(
                "extract: url=%s md_len=%d md_hash=%s path=%s chunks=%d sel_idx=%d sel_len=%d has_more=%s start_from=%d query=%r",
                url[:80], len(full_md), md_hash,
                "fast" if not use_chunker_path else "chunker",
                sel_chunk.total_chunks if sel_chunk else 1,
                sel_chunk.chunk_index if sel_chunk else 0,
                len(page),
                sel_chunk.has_more if sel_chunk else False,
                start_from_char, query[:80],
            )
        except Exception:
            pass

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
        # v0.9.2: restore the pre-v0.9.0 `<page_offset>` line in the
        # user prompt. v0.9.0 dropped it in favor of the content_stats
        # envelope; codex bisection flagged that prompt-shape change
        # as a possible cause of the short-page regression. Keeping
        # both lines is harmless (`<page_offset>` was always there
        # pre-v0.9.0; new envelope only appears for multi-chunk).
        extraction_user = (
            f"<query>\n{query}\n</query>\n\n"
            f"<page_url>\n{url}\n</page_url>\n\n"
            f"<page_offset>{start_from_char}</page_offset>"
            + stats_clause + schema_clause + already_clause + "\n\n"
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
            if not cache_disabled:
                extract_cache[cache_key] = text
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
