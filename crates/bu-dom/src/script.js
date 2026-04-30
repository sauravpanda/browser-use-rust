(() => {
    'use strict';

    const INTRINSIC_TAGS = new Set([
        'a', 'button', 'input', 'select', 'textarea'
    ]);

    // Static text containers — non-interactive but carry the page's
    // actual content. Without these, the agent literally can't see
    // headlines / paragraphs / list items / table cells, which is the
    // primary failure mode on extract-style WebBench tasks. Mirrors
    // upstream browser_use's serializer which emits text nodes
    // interleaved with interactive elements (see
    // browser_use/dom/serializer/serializer.py:1049+). v0.5.7.
    const STATIC_TEXT_TAGS = new Set([
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'p', 'li', 'td', 'th', 'dt', 'dd',
        'blockquote', 'figcaption', 'time', 'label',
        'span', 'div',  // catch-all for sites that put content in generic containers
    ]);

    // Don't bloat the snapshot with the big block-level containers
    // (article/main/section/div) — their text gets covered by the
    // narrower descendants (h1/p/li/...) and including them too would
    // duplicate every paragraph at multiple depths.
    const STATIC_TEXT_BLOCK_THRESHOLD = 2;
    const STATIC_TEXT_MAX_LEN = 280;

    const INTERACTIVE_ROLES = new Set([
        'button', 'link', 'checkbox', 'radio', 'menuitem', 'menuitemcheckbox',
        'menuitemradio', 'option', 'switch', 'tab', 'treeitem', 'combobox',
        'searchbox', 'textbox', 'slider', 'spinbutton'
    ]);

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
        return out;
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

    // Walk a document collecting interactive elements. `(offsetX, offsetY)`
    // shifts bbox coordinates from the document's local viewport into the
    // top window's viewport, so click_index can dispatch at absolute coords.
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

            el.setAttribute('data-bu-idx', String(idx));
            seen.add(el);
            elements.push({
                index: idx,
                tag: el.tagName.toLowerCase(),
                text: text,
                attrs: attrs,
                selector: elementSelector(el, text),
                bbox: { x: r.x + offsetX, y: r.y + offsetY, w: r.width, h: r.height }
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
    };

    collect(document, 0, 0);

    return JSON.stringify({
        url: document.URL,
        title: document.title,
        viewport: {
            width: window.innerWidth,
            height: window.innerHeight,
            device_pixel_ratio: window.devicePixelRatio
        },
        elements: elements
    });
})()
