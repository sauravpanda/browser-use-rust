(() => {
    'use strict';

    const INTRINSIC_TAGS = new Set([
        'a', 'button', 'input', 'select', 'textarea'
    ]);

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

    const elements = [];
    let idx = 1;

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

            if (!intrinsic && !cursorOnly) continue;
            if (cursorOnly && hasIntrinsicAncestor(el)) continue;
            if (!isVisibleInOwnerWindow(el, style, doc)) continue;

            const r = el.getBoundingClientRect();
            if (!isTopAtCenter(el, r, doc)) continue;

            const text = collectText(el);
            const attrs = collectAttrs(el);
            if (!text && Object.keys(attrs).length === 0) continue;

            el.setAttribute('data-bu-idx', String(idx));
            elements.push({
                index: idx,
                tag: el.tagName.toLowerCase(),
                text: text,
                attrs: attrs,
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
