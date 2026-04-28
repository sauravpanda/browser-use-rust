(() => {
    'use strict';

    // Tags that are *always* their own click target.
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

    // Walk up looking for an intrinsic ancestor. Used to dedupe
    // cursor:pointer descendants of real buttons/links.
    const hasIntrinsicAncestor = (el) => {
        let p = el.parentElement;
        while (p && p !== document.body) {
            if (isIntrinsic(p)) return true;
            p = p.parentElement;
        }
        return false;
    };

    const isVisible = (el, style) => {
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        if (parseFloat(style.opacity) === 0) return false;
        const r = el.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) return false;
        if (r.bottom < 0 || r.top > window.innerHeight) return false;
        if (r.right < 0 || r.left > window.innerWidth) return false;
        return true;
    };

    const isTopAtCenter = (el, r) => {
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        const top = document.elementFromPoint(cx, cy);
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
        // src is omitted from KEEP_ATTRS because data: URIs swamp the LLM
        // view; only include short non-data src values when present.
        const src = el.getAttribute('src');
        if (src && !src.startsWith('data:') && src.length <= 200) {
            out['src'] = src;
        }
        return out;
    };

    // Clear any data-bu-idx left over from a prior snapshot so attribute
    // counts and selectors don't accumulate stale state.
    for (const old of document.querySelectorAll('[data-bu-idx]')) {
        old.removeAttribute('data-bu-idx');
    }

    const elements = [];
    let idx = 1;
    const all = document.querySelectorAll('*');
    for (const el of all) {
        // Never include anything inside an SVG — the SVG (or its parent button)
        // stands in as the click target.
        if (isInsideSvg(el)) continue;
        // The SVG itself is only included when it's explicitly interactive.
        if (el.tagName === 'svg' || el.tagName === 'SVG') {
            if (!isIntrinsic(el) && !el.getAttribute('aria-label')) continue;
        }

        const style = getComputedStyle(el);
        const intrinsic = isIntrinsic(el);
        const cursorOnly = !intrinsic && style.cursor === 'pointer';

        if (!intrinsic && !cursorOnly) continue;

        // cursor:pointer is mostly inherited; only keep when the element is
        // genuinely the click target (no intrinsic ancestor wraps it).
        if (cursorOnly && hasIntrinsicAncestor(el)) continue;

        if (!isVisible(el, style)) continue;
        const r = el.getBoundingClientRect();
        if (!isTopAtCenter(el, r)) continue;

        const text = collectText(el);
        const attrs = collectAttrs(el);
        // Drop residual noise: nothing identifying, nothing to read.
        if (!text && Object.keys(attrs).length === 0) continue;

        // Tag the element with its index so subsequent click/type can find
        // it again even if the DOM reflows between snapshot and action.
        el.setAttribute('data-bu-idx', String(idx));
        elements.push({
            index: idx,
            tag: el.tagName.toLowerCase(),
            text: text,
            attrs: attrs,
            bbox: { x: r.x, y: r.y, w: r.width, h: r.height }
        });
        idx++;
    }

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
