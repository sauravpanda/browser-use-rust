//! Domain-pattern URL matching for the navigation allow/deny policy.
//! Extracted verbatim from `lib.rs`, tests included.

/// True for URLs that should always be navigable regardless of policy
/// (new-tab placeholders, browser-internal pages used during startup).
pub(crate) fn is_new_tab_url(url: &str) -> bool {
    let u = url.trim().to_ascii_lowercase();
    u.is_empty()
        || u == "about:blank"
        || u.starts_with("chrome://new-tab-page")
        || u.starts_with("chrome://newtab")
}

/// Match a URL against a `browser_use`-style domain pattern. Mirrors
/// `browser_use.utils.match_url_with_domain_pattern` semantics:
///
/// - Bare hostname (`example.com`): exact hostname match, default `https`.
/// - `*.example.com`: matches `example.com` AND any subdomain.
/// - `*google.com`: prefix glob — matches `google.com`, `agoogle.com`,
///   `www.google.com`.
/// - `chrome-extension://*`: scheme + any host.
/// - `http*://example.com`: scheme glob (matches `http`, `https`, etc.).
///
/// Rejects unsafe patterns: multiple wildcards, wildcard TLDs (`example.*`),
/// or wildcards in non-leading positions other than `*.`-prefix.
pub(crate) fn match_url_with_domain_pattern(url: &str, pattern: &str) -> bool {
    if is_new_tab_url(url) {
        return false;
    }
    let parsed = match url::Url::parse(url) {
        Ok(u) => u,
        Err(_) => return false,
    };
    let scheme = parsed.scheme().to_ascii_lowercase();
    let host = match parsed.host_str() {
        Some(h) => h.to_ascii_lowercase(),
        None => return false,
    };
    if scheme.is_empty() {
        return false;
    }

    let lower = pattern.to_ascii_lowercase();
    let (pattern_scheme, mut pattern_domain) = match lower.split_once("://") {
        Some((s, d)) => (s.to_string(), d.to_string()),
        None => ("https".to_string(), lower.clone()),
    };
    // Strip any port from pattern domain.
    if let Some(idx) = pattern_domain.find(':') {
        if idx > 0 {
            pattern_domain.truncate(idx);
        }
    }

    if !glob_match(&pattern_scheme, &scheme) {
        return false;
    }

    // Exact match or full wildcard.
    if pattern_domain == "*" || pattern_domain == host {
        return true;
    }

    if !pattern_domain.contains('*') {
        return false;
    }

    // Reject unsafe globs.
    let star_dot_count = pattern_domain.matches("*.").count();
    let dot_star_count = pattern_domain.matches(".*").count();
    if star_dot_count > 1 || dot_star_count > 1 {
        return false;
    }
    if pattern_domain.ends_with(".*") {
        return false; // wildcard TLD
    }
    let bare = pattern_domain.replace("*.", "");
    if bare.contains('*') && !pattern_domain.starts_with('*') {
        return false; // embedded wildcard outside leading position
    }

    if let Some(rest) = pattern_domain.strip_prefix("*.") {
        // *.example.com matches example.com (root) AND *.example.com (subs).
        return host == rest || host.ends_with(&format!(".{rest}"));
    }
    glob_match(&pattern_domain, &host)
}

/// Tiny glob matcher supporting `*` (any sequence). Used for domain patterns
/// like `*google.com` and scheme patterns like `http*`.
fn glob_match(pattern: &str, value: &str) -> bool {
    if !pattern.contains('*') {
        return pattern == value;
    }
    let parts: Vec<&str> = pattern.split('*').collect();
    let mut idx = 0usize;
    // Anchor first part if pattern doesn't start with *.
    if let Some(first) = parts.first() {
        if !first.is_empty() {
            if !value[idx..].starts_with(first) {
                return false;
            }
            idx += first.len();
        }
    }
    // Middle parts: each must appear in order.
    let last_idx = parts.len() - 1;
    for part in parts.iter().take(last_idx).skip(1) {
        if part.is_empty() {
            continue;
        }
        match value[idx..].find(part) {
            Some(p) => idx += p + part.len(),
            None => return false,
        }
    }
    // Anchor last part if pattern doesn't end with *.
    if let Some(last) = parts.last() {
        if !last.is_empty() {
            return value[idx..].ends_with(last);
        }
    }
    true
}

#[cfg(test)]
mod url_match_tests {
    use super::*;

    #[test]
    fn exact_hostname_https_only() {
        assert!(match_url_with_domain_pattern("https://example.com/", "example.com"));
        // Bare pattern defaults to https — http should NOT match.
        assert!(!match_url_with_domain_pattern("http://example.com/", "example.com"));
    }

    #[test]
    fn star_dot_matches_root_and_subs() {
        assert!(match_url_with_domain_pattern("https://example.com/", "*.example.com"));
        assert!(match_url_with_domain_pattern("https://sub.example.com/", "*.example.com"));
        assert!(!match_url_with_domain_pattern("https://evil.com/", "*.example.com"));
        assert!(!match_url_with_domain_pattern("https://exampleXcom/", "*.example.com"));
    }

    #[test]
    fn prefix_star() {
        assert!(match_url_with_domain_pattern("https://google.com/", "*google.com"));
        assert!(match_url_with_domain_pattern("https://www.google.com/", "*google.com"));
        assert!(match_url_with_domain_pattern("https://agoogle.com/", "*google.com"));
        assert!(!match_url_with_domain_pattern("https://googleX.com/", "*google.com"));
    }

    #[test]
    fn scheme_glob() {
        assert!(match_url_with_domain_pattern("http://example.com/", "http*://example.com"));
        assert!(match_url_with_domain_pattern("https://example.com/", "http*://example.com"));
    }

    #[test]
    fn extension_wildcard() {
        assert!(match_url_with_domain_pattern("chrome-extension://abc/", "chrome-extension://*"));
        assert!(!match_url_with_domain_pattern("https://abc/", "chrome-extension://*"));
    }

    #[test]
    fn rejects_wildcard_tld() {
        assert!(!match_url_with_domain_pattern("https://example.com/", "example.*"));
    }

    #[test]
    fn rejects_multiple_wildcards() {
        assert!(!match_url_with_domain_pattern("https://a.b.example.com/", "*.*.example.com"));
    }

    #[test]
    fn new_tab_never_matches() {
        assert!(!match_url_with_domain_pattern("about:blank", "*"));
    }
}
