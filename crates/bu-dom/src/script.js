(() => {
    'use strict';

    const INTERACTIVE_TAGS = new Set([
        'a', 'button', 'input', 'select', 'textarea', 'label',
        'option', 'summary', 'details'
    ]);

    const INTERACTIVE_ROLES = new Set([
        'button', 'link', 'checkbox', 'radio', 'menuitem', 'menuitemcheckbox',
        'menuitemradio', 'option', 'switch', 'tab', 'treeitem', 'combobox',
        'searchbox', 'textbox', 'slider', 'spinbutton'
    ]);

    const KEEP_ATTRS = [
        'id', 'name', 'type', 'placeholder', 'href', 'value', 'src', 'alt',
        'title', 'role', 'aria-label', 'aria-labelledby', 'aria-expanded',
        'aria-checked', 'aria-selected', 'data-testid'
    ];

    const isInteractive = (el, style) => {
        const tag = el.tagName.toLowerCase();
        if (INTERACTIVE_TAGS.has(tag)) return true;
        const role = el.getAttribute('role');
        if (role && INTERACTIVE_ROLES.has(role.toLowerCase())) return true;
        if (el.hasAttribute('onclick') || el.hasAttribute('tabindex')) return true;
        if (el.isContentEditable) return true;
        if (style.cursor === 'pointer') return true;
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
            const v = el.getAttribute(k);
            if (v != null && v !== '') out[k] = String(v).slice(0, 200);
        }
        return out;
    };

    const elements = [];
    let idx = 1;
    const all = document.querySelectorAll('*');
    for (const el of all) {
        const style = getComputedStyle(el);
        if (!isInteractive(el, style)) continue;
        if (!isVisible(el, style)) continue;
        const r = el.getBoundingClientRect();
        if (!isTopAtCenter(el, r)) continue;
        elements.push({
            index: idx++,
            tag: el.tagName.toLowerCase(),
            text: collectText(el),
            attrs: collectAttrs(el),
            bbox: { x: r.x, y: r.y, w: r.width, h: r.height }
        });
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
