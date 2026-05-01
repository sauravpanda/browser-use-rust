//! DOM snapshot, indexed clickable extraction, and LLM-facing serialization.
//!
//! v1 strategy: inject a JS payload via `Runtime.evaluate` that walks the
//! document, filters interactive + visible + on-top-at-center elements, and
//! returns a JSON blob we deserialize into [`DomState`]. The JS bypasses the
//! complexity of multiple CDP round-trips and lets us iterate on heuristics
//! at JS-edit speed instead of Rust-recompile speed. Cross-origin iframes
//! and shadow DOM are deliberately out of scope until something needs them.

use std::collections::BTreeMap;

use bu_cdp::{CdpError, Connection};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use thiserror::Error;

const SNAPSHOT_SCRIPT: &str = include_str!("script.js");

#[derive(Debug, Error)]
pub enum DomError {
    #[error("cdp: {0}")]
    Cdp(#[from] CdpError),
    #[error("serde: {0}")]
    Serde(#[from] serde_json::Error),
    #[error("snapshot script returned no value")]
    NoValue,
    #[error("unknown element index {0}")]
    UnknownIndex(u32),
}

pub type Result<T> = std::result::Result<T, DomError>;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Bbox {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

impl Bbox {
    pub fn center(&self) -> (f64, f64) {
        (self.x + self.w / 2.0, self.y + self.h / 2.0)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Viewport {
    pub width: u32,
    pub height: u32,
    pub device_pixel_ratio: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DomElement {
    pub index: u32,
    pub tag: String,
    pub text: String,
    #[serde(default)]
    pub attrs: BTreeMap<String, String>,
    /// Stable, human-readable selector — `#id`, `button "Sign In"`,
    /// `[data-testid='x']`, etc. Computed by the JS walker (see
    /// `elementSelector` in script.js) so it's based on the live DOM
    /// state, not Rust-side string surgery. Intended for cross-turn
    /// references in agent_history rendering — when the LLM looks back
    /// at "what did I click on step 5", we want a description that
    /// still resolves on a re-rendered page, not a `[N]` index gone
    /// stale.
    #[serde(default)]
    pub selector: String,
    pub bbox: Bbox,
    /// Tree depth — count of interactive ancestors above this element.
    /// Rendered as leading tabs in to_llm_string so the LLM can see
    /// hierarchy (which item belongs to which list/article/table).
    /// Defaults to 0 for snapshots produced before v0.7.0.
    #[serde(default)]
    pub depth: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct PageInfo {
    pub pages_above: f64,
    pub pages_below: f64,
    pub scroll_y: f64,
    pub doc_height: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DomState {
    pub url: String,
    pub title: String,
    pub viewport: Viewport,
    pub elements: Vec<DomElement>,
    /// Scroll position relative to the document. Renders as "X pages
    /// above, Y pages below" in the LLM string. v0.6.3.
    #[serde(default)]
    pub page_info: PageInfo,
}

impl DomState {
    pub fn get(&self, index: u32) -> Result<&DomElement> {
        self.elements
            .iter()
            .find(|e| e.index == index)
            .ok_or(DomError::UnknownIndex(index))
    }

    /// Render the snapshot as the LLM-facing string:
    /// `[1]<button id="x">Sign in</button>` per line for interactive
    /// elements, plus `<h2> "Today's News"` style lines for static text
    /// content (index == 0 sentinel; not clickable). Header with
    /// url/title and a `<page_info>` summary of scroll position.
    /// `|scroll|` prefix on elements that are scrollable containers.
    /// v0.6.3.
    pub fn to_llm_string(&self) -> String {
        let mut out = String::with_capacity(64 * self.elements.len());
        out.push_str(&format!("URL: {}\nTITLE: {}\n", self.url, self.title));
        out.push_str(&format!(
            "VIEWPORT: {}x{}\n",
            self.viewport.width, self.viewport.height
        ));
        // page_info — scroll context. Mirrors upstream's <page_info>
        // block. Tells the agent how much content sits above/below the
        // viewport so it knows whether to scroll vs assume nothing more
        // is there. v0.6.3.
        let pi = &self.page_info;
        if pi.doc_height > 0.0 {
            out.push_str(&format!(
                "PAGE_INFO: {:.1} pages above, {:.1} pages below",
                pi.pages_above, pi.pages_below,
            ));
            if pi.pages_below > 0.2 {
                out.push_str(" — scroll down to reveal more");
            } else if pi.pages_above < 0.1 && pi.pages_below < 0.1 {
                out.push_str(" — entire page visible");
            }
            out.push('\n');
        }
        out.push_str("\nELEMENTS:\n");
        for el in &self.elements {
            if el.index == 0 {
                // Static text content — not clickable. Rendered without
                // a [N] prefix so the LLM doesn't accidentally try to
                // click it. The agent uses this for extraction context.
                let escaped = el.text.replace('"', "\\\"");
                out.push_str(&format!("<{}> \"{}\"\n", el.tag, escaped));
                continue;
            }
            // Scrollable-container marker (v0.6.3): rendered before the
            // [N] prefix so the agent sees `|scroll|[5]<div>` and knows
            // it can scroll inside element 5 (vs scrolling the page).
            let scroll_prefix = if el
                .attrs
                .get("scrollable")
                .map(|v| v == "true")
                .unwrap_or(false)
            {
                "|scroll|"
            } else {
                ""
            };
            // Indent by tree depth (cap at 6 levels to bound bloat).
            let indent_n = el.depth.min(6) as usize;
            for _ in 0..indent_n {
                out.push('\t');
            }
            out.push_str(&format!("{scroll_prefix}[{}]<{}", el.index, el.tag));
            for (k, v) in &el.attrs {
                if k == "scrollable" {
                    continue; // already shown via the prefix
                }
                let escaped = v.replace('"', "\\\"");
                out.push_str(&format!(" {k}=\"{escaped}\""));
            }
            if el.text.is_empty() {
                out.push_str(" />\n");
            } else {
                out.push_str(&format!(">{}</{}>\n", el.text, el.tag));
            }
        }
        out
    }
}

/// Run the snapshot script in the page and return the parsed DomState.
pub async fn snapshot(conn: &Connection, session_id: &str) -> Result<DomState> {
    let r = conn
        .send(
            "Runtime.evaluate",
            json!({
                "expression": SNAPSHOT_SCRIPT,
                "returnByValue": true,
                "awaitPromise": false,
            }),
            Some(session_id),
        )
        .await?;
    let json_str = r
        .get("result")
        .and_then(|x| x.get("value"))
        .and_then(Value::as_str)
        .ok_or(DomError::NoValue)?;
    Ok(serde_json::from_str(json_str)?)
}
