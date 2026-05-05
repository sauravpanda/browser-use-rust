(() => {
    'use strict';

    const INTRINSIC_TAGS = new Set([
        'a', 'button', 'input', 'select', 'textarea'
    ]);

    // Static text containers — non-interactive but carry the page's
    // actual content. Walked path:
    //   v0.5.7 (full set incl. span/div/label): -2pp vs v0.5.6 — too
    //     much chrome/footer noise.
    //   v0.5.8 (narrowed: h1-h6/p/li/td/...): -2pp vs v0.5.6 too.
    //   v0.6.0 KEPT v0.5.8's narrowed set + added extract/file tools:
    //     +3pp net vs v0.5.6 (53% -> 56%).
    //   v0.6.1 reverted static text on the theory it was the drag:
    //     -2pp vs v0.6.0 (56% -> 54%) — WRONG. Static text was
    //     helping in combination with the extract tools, not hurting.
    // v0.6.4 restores the v0.5.8/v0.6.0 narrowed set. Keeps
    // h1-h6, p, li, td, th, dt, dd, blockquote, figcaption, time —
    // all genuinely-content tags. Drops span/div/label which were
    // chrome/nav junk in v0.5.7.
    const STATIC_TEXT_TAGS = new Set([
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'p', 'li', 'td', 'th', 'dt', 'dd',
        'blockquote', 'figcaption', 'time',
    ]);
    const STATIC_TEXT_BLOCK_THRESHOLD = 2;
    const STATIC_TEXT_MAX_LEN = 280;

    const INTERACTIVE_ROLES = new Set([
        'button', 'link', 'checkbox', 'radio', 'menuitem', 'menuitemcheckbox',
        'menuitemradio', 'option', 'switch', 'tab', 'treeitem', 'combobox',
        'searchbox', 'textbox', 'slider', 'spinbutton'
    ]);

    // v0.8.21: reverted v0.8.17's KEEP_ATTRS expansion. The 22-attr
    // expansion (min/max/step/pattern/autocomplete/list/multiple/
    // aria-*/data-value/for) materially bloated the per-element DOM
    // serialization on dense pages — measured judge regression of
    // -3pp v0.8.16 → v0.8.17 (64.65% → 61.62%) with no offsetting
    // win on the long-tail filter tasks the expansion was meant to
    // help. Hypothesis: extra attrs added more noise than constraint
    // signal; LLM weighted them against primary content. Restoring
    // the v0.8.16 list. The link href/text reversal fix and
    // _valid_indices regex fix from v0.8.17 are KEPT — both pure
    // correctness bug fixes with no plausible regression vector.
    //
    // v0.8.29 (codex-recommended): narrow re-add of just min/max/step.
    // REVERTED in v0.8.30 — empirically caused -9 judge tasks vs the
    // v0.8.27 baseline (143 → 134) and ZERO of the 5 targeted filter
    // tasks recovered (2049 Orlando rentals, 97 LA rent filter, 1134
    // electronics avg price, 2570 porch swing, 1496 Harvard renewable
    // energy). +13 "Incorrect Result" failures and +32 action errors
    // confirm the v0.8.21 lesson: KEEP_ATTRS additions of any size
    // hurt this substrate. Even input-only attributes are weighed
    // against primary content by the LLM. Filter UIs need a different
    // intervention (e.g. dedicated filter tool, structured output
    // mode), not raw DOM attribute exposure.
    const KEEP_ATTRS = [
        'id', 'name', 'type', 'placeholder', 'href', 'value', 'alt',
        'title', 'role', 'aria-label', 'aria-labelledby', 'aria-expanded',
        'aria-checked', 'aria-selected', 'data-testid'
    ];

    const isInsideSvg = (el) => {
        let p = el.parentElement;
        while (p) {
            if (p.tagName === 'svg' || p.tagName === 'SVG') return true;
            p = p.parentElement;
        }
        return false;
    };

    const isIntrinsic = (el) => {
        const tag = el.tagName.toLowerCase();
        if (INTRINSIC_TAGS.has(tag)) return true;
        const role = el.getAttribute('role');
        if (role && INTERACTIVE_ROLES.has(role.toLowerCase())) return true;
        if (el.hasAttribute('onclick')) return true;
        const tabindex = el.getAttribute('tabindex');
        if (tabindex !== null && tabindex !== '-1') return true;
        if (el.isContentEditable) return true;
        return false;
    };

    const hasIntrinsicAncestor = (el) => {
        let p = el.parentElement;
        const root = el.ownerDocument.body;
        while (p && p !== root) {
            if (isIntrinsic(p)) return true;
            p = p.parentElement;
        }
        return false;
    };

    const isVisibleInOwnerWindow = (el, style, doc) => {
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        if (parseFloat(style.opacity) === 0) return false;
        const r = el.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) return false;
        const win = doc.defaultView || window;
        if (r.bottom < 0 || r.top > win.innerHeight) return false;
        if (r.right < 0 || r.left > win.innerWidth) return false;
        return true;
    };

    const isTopAtCenter = (el, r, doc) => {
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        const top = doc.elementFromPoint(cx, cy);
        if (!top) return false;
        return top === el || el.contains(top) || top.contains(el);
    };

    const collectText = (el) => {
        const tag = el.tagName.toLowerCase();
        if (tag === 'input' || tag === 'textarea') {
            return (el.value || el.placeholder || el.getAttribute('aria-label') || '').trim();
        }
        if (tag === 'select') {
            const opt = el.options[el.selectedIndex];
            return opt ? opt.text.trim() : '';
        }
        const txt = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
        if (txt) return txt.length > 200 ? txt.slice(0, 200) + '…' : txt;
        return (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
    };

    // ISO formats for HTML5 date/time inputs. Browser DISPLAYS in
    // locale format but the .value attribute always uses these. Mirrors
    // upstream browser_use's serializer which adds a 'format' attr to
    // date inputs so the LLM can't get it wrong. v0.6.3.
    const DATE_INPUT_FORMATS = {
        'date': 'YYYY-MM-DD',
        'time': 'HH:MM',
        'datetime-local': 'YYYY-MM-DDTHH:MM',
        'month': 'YYYY-MM',
        'week': 'YYYY-Www',
    };

    const collectAttrs = (el) => {
        const out = {};
        for (const k of KEEP_ATTRS) {
            let v = el.getAttribute(k);
            if (v == null || v === '') continue;
            v = String(v);
            if ((k === 'href' || k === 'src') && v.startsWith('data:')) {
                v = v.slice(0, 32) + '…';
            }
            out[k] = v.slice(0, 200);
        }
        const src = el.getAttribute('src');
        if (src && !src.startsWith('data:') && src.length <= 200) {
            out['src'] = src;
        }
        // Date/time format hint (v0.6.3): browser-use upstream adds
        // this so the LLM can't guess the wrong format and fail
        // silently when typing into date inputs.
        const tag = el.tagName.toLowerCase();
        if (tag === 'input') {
            const t = (el.getAttribute('type') || '').toLowerCase();
            if (t in DATE_INPUT_FORMATS) {
                out['format'] = DATE_INPUT_FORMATS[t];
            }
        }
        // Select options preview (v0.6.3): list the first 4 option
        // labels inline so the LLM can tell what to pick without a
        // separate get_dropdown_options call. Mirrors upstream's
        // compound_children info on selects.
        if (tag === 'select') {
            try {
                const opts = [];
                for (let i = 0; i < el.options.length && opts.length < 4; i++) {
                    const o = el.options[i];
                    const t = (o.text || '').replace(/\s+/g, ' ').trim().slice(0, 40);
                    if (t) opts.push(t);
                }
                if (opts.length) {
                    out['options'] = opts.join('|');
                    if (el.options.length > opts.length) {
                        out['option_count'] = String(el.options.length);
                    }
                }
            } catch (e) { /* defensive */ }
        }
        return out;
    };

    // Detect a genuinely-scrollable container: overflow allows scroll AND
    // there's actually content beyond the visible bounds. Mirrors upstream's
    // is_actually_scrollable check. Used to tag elements with a `|scroll|`
    // marker so the agent knows it can scroll *inside* this container, not
    // just the page. v0.6.3.
    const isScrollContainer = (el, style) => {
        const oy = style.overflowY;
        const ox = style.overflowX;
        const scrollable_y = (oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 4;
        const scrollable_x = (ox === 'auto' || ox === 'scroll') && el.scrollWidth > el.clientWidth + 4;
        return scrollable_y || scrollable_x;
    };

    // Clear stale data-bu-idx everywhere it might live (top doc + same-origin iframes).
    const clearStale = (doc) => {
        for (const old of doc.querySelectorAll('[data-bu-idx]')) {
            old.removeAttribute('data-bu-idx');
        }
        for (const iframe of doc.querySelectorAll('iframe')) {
            try {
                const sub = iframe.contentDocument;
                if (sub) clearStale(sub);
            } catch (e) { /* cross-origin */ }
        }
    };
    clearStale(document);

    // Build a stable, human-readable selector for cross-turn references.
    // Format priorities (most stable → least):
    //   1. id          → `#hero-search`
    //   2. data-testid → `[data-testid='login-btn']`
    //   3. role + name → `button "Sign In"`  (aria-label or visible text)
    //   4. tag + name  → `a "More information..."`  (visible text)
    //   5. tag + nth   → `button:nth-of-type(3)`  (last resort)
    // Result is a short string the LLM can read in history without
    // re-snapshotting. The same element on a re-rendered page should
    // hash to the same selector even if its `[N]` index has shifted.
    const elementSelector = (el, text) => {
        const id = el.getAttribute('id');
        if (id && /^[a-zA-Z][\w-]{0,32}$/.test(id)) return `#${id}`;

        const testid = el.getAttribute('data-testid');
        if (testid) return `[data-testid='${testid}']`;

        const role = el.getAttribute('role') || el.tagName.toLowerCase();
        const aria = el.getAttribute('aria-label');
        const name = aria || (text ? text.trim().slice(0, 40) : '');
        if (name) return `${role} "${name.replace(/"/g, '\\"')}"`;

        // Same-tag siblings. nth is brittle but better than nothing.
        const parent = el.parentElement;
        if (parent) {
            const sib = Array.from(parent.children).filter(c => c.tagName === el.tagName);
            const n = sib.indexOf(el) + 1;
            if (sib.length > 1) return `${el.tagName.toLowerCase()}:nth-of-type(${n})`;
        }
        return el.tagName.toLowerCase();
    };

    // Direct text content of an element, NOT including descendants.
    // Used for static text emission so we don't duplicate text from
    // child <h1> when their parent <article> is also walked.
    const directText = (el) => {
        let s = '';
        for (const n of el.childNodes) {
            if (n.nodeType === 3) s += n.nodeValue;  // TEXT_NODE
        }
        return s.replace(/\s+/g, ' ').trim();
    };

    const elements = [];
    let idx = 1;
    // Track elements we've already emitted (interactive OR static) so
    // we don't double-count when their containers are walked later.
    const seen = new WeakSet();

    // Compute interactive-ancestor depth — how many emitted elements
    // are above this one in the DOM tree. Used to render the snapshot
    // with indentation so the LLM sees the tree structure (which item
    // belongs to which list/article/table). v0.7.0.
    const interactiveDepth = (el) => {
        let d = 0;
        let p = el.parentElement;
        const root = el.ownerDocument && el.ownerDocument.body;
        while (p && p !== root) {
            if (p.hasAttribute && p.hasAttribute('data-bu-idx')) d++;
            p = p.parentElement;
        }
        return d;
    };

    // Walk a document collecting interactive elements. `(offsetX, offsetY)`
    // shifts bbox coordinates from the document's local viewport into the
    // top window's viewport, so click_index can dispatch at absolute coords.
    // Also descends into shadow roots for proper shadow-DOM coverage. v0.7.0.
    const collect = (doc, offsetX, offsetY) => {
        const all = doc.querySelectorAll('*');
        for (const el of all) {
            if (isInsideSvg(el)) continue;
            if (el.tagName === 'svg' || el.tagName === 'SVG') {
                if (!isIntrinsic(el) && !el.getAttribute('aria-label')) continue;
            }

            const style = getComputedStyle(el);
            const intrinsic = isIntrinsic(el);
            const cursorOnly = !intrinsic && style.cursor === 'pointer';
            const tag = el.tagName.toLowerCase();
            const isStaticText = STATIC_TEXT_TAGS.has(tag);

            if (!intrinsic && !cursorOnly && !isStaticText) continue;
            if (cursorOnly && hasIntrinsicAncestor(el)) continue;
            if (!isVisibleInOwnerWindow(el, style, doc)) continue;

            // Static text path — emit non-interactive content so the
            // agent can extract from it (headlines, prices, table cells)
            // without needing a separate page_text call.
            if (!intrinsic && !cursorOnly && isStaticText) {
                if (seen.has(el)) continue;
                // Skip if inside an interactive element (already shown).
                if (hasIntrinsicAncestor(el)) continue;
                // Use direct text content only, not descendants — avoids
                // every <p> in an <article> showing the article's full
                // text repeatedly.
                const txt = directText(el);
                if (!txt || txt.length < STATIC_TEXT_BLOCK_THRESHOLD) continue;
                const trimmed = txt.length > STATIC_TEXT_MAX_LEN
                    ? txt.slice(0, STATIC_TEXT_MAX_LEN) + '…'
                    : txt;
                seen.add(el);
                const r = el.getBoundingClientRect();
                elements.push({
                    index: 0,  // sentinel: non-interactive
                    tag: tag,
                    text: trimmed,
                    attrs: {},
                    selector: '',
                    bbox: { x: r.x + offsetX, y: r.y + offsetY, w: r.width, h: r.height }
                });
                continue;
            }

            const r = el.getBoundingClientRect();
            if (!isTopAtCenter(el, r, doc)) continue;

            const text = collectText(el);
            const attrs = collectAttrs(el);
            if (!text && Object.keys(attrs).length === 0) continue;
            // Tag scrollable containers so the agent knows to scroll
            // INSIDE them (e.g. infinite-scroll product grids, comment
            // threads in modals) rather than scrolling the whole page.
            // v0.6.3.
            if (isScrollContainer(el, style)) {
                attrs['scrollable'] = 'true';
            }

            el.setAttribute('data-bu-idx', String(idx));
            seen.add(el);
            elements.push({
                index: idx,
                tag: el.tagName.toLowerCase(),
                text: text,
                attrs: attrs,
                selector: elementSelector(el, text),
                bbox: { x: r.x + offsetX, y: r.y + offsetY, w: r.width, h: r.height },
                depth: interactiveDepth(el),
            });
            idx++;
        }

        // Recurse into same-origin iframes. Cross-origin frames throw on
        // contentDocument access — catch and skip; they need a separate
        // CDP target attach to walk, which is deferred.
        for (const iframe of doc.querySelectorAll('iframe')) {
            let sub = null;
            try { sub = iframe.contentDocument; } catch (e) { continue; }
            if (!sub) continue;
            const fr = iframe.getBoundingClientRect();
            collect(sub, offsetX + fr.x, offsetY + fr.y);
        }

        // Shadow DOM walk (v0.7.0). Many web-component-heavy sites
        // (Salesforce, custom UI kits, modern e-commerce) put real
        // content inside open shadow roots. querySelectorAll('*')
        // skips them. Walk each shadow root the same way.
        for (const host of doc.querySelectorAll('*')) {
            try {
                const root = host.shadowRoot;
                if (root) collect(root, offsetX, offsetY);
            } catch (e) { /* closed root or cross-origin */ }
        }
    };

    collect(document, 0, 0);

    // Page scroll context (v0.6.3): tells the agent how much content
    // is above/below the visible viewport so it can decide whether
    // scrolling is worth it. Mirrors upstream's <page_info> block:
    // 'X.X pages above, Y.Y pages below — scroll down to reveal more'.
    const scrollY = window.scrollY || document.documentElement.scrollTop || 0;
    const docH = Math.max(
        document.body ? document.body.scrollHeight : 0,
        document.documentElement ? document.documentElement.scrollHeight : 0,
    );
    const viewH = window.innerHeight || 1;
    const pagesAbove = scrollY / viewH;
    const pagesBelow = Math.max(0, (docH - scrollY - viewH) / viewH);

    return JSON.stringify({
        url: document.URL,
        title: document.title,
        viewport: {
            width: window.innerWidth,
            height: window.innerHeight,
            device_pixel_ratio: window.devicePixelRatio
        },
        elements: elements,
        page_info: {
            pages_above: Math.round(pagesAbove * 10) / 10,
            pages_below: Math.round(pagesBelow * 10) / 10,
            scroll_y: scrollY,
            doc_height: docH,
        }
    });
})()
