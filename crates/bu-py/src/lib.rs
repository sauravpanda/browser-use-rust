//! PyO3 bindings — the only crate Python knows about.

use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use pyo3_async_runtimes::tokio::future_into_py;
use tokio::sync::Mutex;

fn map_err<E: std::fmt::Display>(e: E) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(e.to_string())
}

// ---------- DOM types ----------

#[pyclass]
#[derive(Clone)]
struct Bbox {
    inner: bu_dom::Bbox,
}

#[pymethods]
impl Bbox {
    #[getter]
    fn x(&self) -> f64 {
        self.inner.x
    }
    #[getter]
    fn y(&self) -> f64 {
        self.inner.y
    }
    #[getter]
    fn w(&self) -> f64 {
        self.inner.w
    }
    #[getter]
    fn h(&self) -> f64 {
        self.inner.h
    }
    fn center(&self) -> (f64, f64) {
        self.inner.center()
    }
    fn __repr__(&self) -> String {
        format!(
            "Bbox(x={:.1}, y={:.1}, w={:.1}, h={:.1})",
            self.inner.x, self.inner.y, self.inner.w, self.inner.h
        )
    }
}

#[pyclass]
#[derive(Clone)]
struct DomElement {
    inner: bu_dom::DomElement,
}

#[pymethods]
impl DomElement {
    #[getter]
    fn index(&self) -> u32 {
        self.inner.index
    }
    #[getter]
    fn tag(&self) -> String {
        self.inner.tag.clone()
    }
    #[getter]
    fn text(&self) -> String {
        self.inner.text.clone()
    }
    #[getter]
    fn attrs<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        for (k, v) in &self.inner.attrs {
            d.set_item(k, v)?;
        }
        Ok(d)
    }
    #[getter]
    fn bbox(&self) -> Bbox {
        Bbox {
            inner: self.inner.bbox.clone(),
        }
    }
    fn __repr__(&self) -> String {
        let mut s = format!("[{}]<{}", self.inner.index, self.inner.tag);
        for (k, v) in &self.inner.attrs {
            s.push_str(&format!(" {k}={v:?}"));
        }
        if self.inner.text.is_empty() {
            s.push_str(" />");
        } else {
            s.push_str(&format!(">{}</{}>", self.inner.text, self.inner.tag));
        }
        s
    }
}

#[pyclass]
#[derive(Clone)]
struct DomState {
    inner: bu_dom::DomState,
}

#[pymethods]
impl DomState {
    #[getter]
    fn url(&self) -> String {
        self.inner.url.clone()
    }
    #[getter]
    fn title(&self) -> String {
        self.inner.title.clone()
    }
    #[getter]
    fn viewport(&self) -> (u32, u32, f64) {
        let v = &self.inner.viewport;
        (v.width, v.height, v.device_pixel_ratio)
    }
    #[getter]
    fn elements(&self) -> Vec<DomElement> {
        self.inner
            .elements
            .iter()
            .map(|e| DomElement { inner: e.clone() })
            .collect()
    }
    fn to_llm_string(&self) -> String {
        self.inner.to_llm_string()
    }
    fn __len__(&self) -> usize {
        self.inner.elements.len()
    }
    fn __repr__(&self) -> String {
        format!(
            "DomState(url={:?}, elements={})",
            self.inner.url,
            self.inner.elements.len()
        )
    }
}

// ---------- BrowserSession ----------

#[pyclass]
struct BrowserSession {
    inner: Arc<Mutex<Option<bu_browser::BrowserSession>>>,
    launch_opts: Arc<bu_browser::LaunchOptions>,
}

#[pymethods]
impl BrowserSession {
    #[new]
    #[pyo3(signature = (headless=true, viewport=Some((1280, 900)), chrome_path=None, extra_chrome_args=None, cdp_url=None, allowed_domains=None, prohibited_domains=None, stealth=false, user_data_dir=None))]
    fn new(
        headless: bool,
        viewport: Option<(u32, u32)>,
        chrome_path: Option<String>,
        extra_chrome_args: Option<Vec<String>>,
        cdp_url: Option<String>,
        allowed_domains: Option<Vec<String>>,
        prohibited_domains: Option<Vec<String>>,
        stealth: bool,
        user_data_dir: Option<String>,
    ) -> Self {
        let opts = bu_browser::LaunchOptions {
            headless,
            chrome_path: chrome_path.map(std::path::PathBuf::from),
            user_data_dir: user_data_dir.map(std::path::PathBuf::from),
            extra_args: extra_chrome_args.unwrap_or_default(),
            viewport,
            cdp_url,
            allowed_domains: allowed_domains.unwrap_or_default(),
            prohibited_domains: prohibited_domains.unwrap_or_default(),
            stealth,
        };
        Self {
            inner: Arc::new(Mutex::new(None)),
            launch_opts: Arc::new(opts),
        }
    }

    /// Alias for stop() — matches browser_use's BrowserSession.kill() name.
    fn kill<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.stop(py)
    }

    fn start<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let opts = (*self.launch_opts).clone();
        future_into_py(py, async move {
            // Idempotent: if already started, no-op. Lets the Agent call
            // start() unconditionally even when the caller pre-started.
            {
                let guard = inner.lock().await;
                if guard.is_some() {
                    return Ok(());
                }
            }
            let s = bu_browser::BrowserSession::launch(opts)
                .await
                .map_err(map_err)?;
            *inner.lock().await = Some(s);
            Ok(())
        })
    }

    fn navigate<'py>(&self, py: Python<'py>, url: String) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.navigate(&url).await.map_err(map_err)?;
            Ok(())
        })
    }

    fn screenshot<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let bytes = {
                let guard = inner.lock().await;
                let s = guard
                    .as_ref()
                    .ok_or_else(|| map_err("session not started — call start() first"))?;
                s.screenshot().await.map_err(map_err)?
            };
            Python::with_gil(|py| Ok(PyBytes::new(py, &bytes).unbind()))
        })
    }

    fn pdf<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let bytes = {
                let guard = inner.lock().await;
                let s = guard
                    .as_ref()
                    .ok_or_else(|| map_err("session not started — call start() first"))?;
                s.pdf().await.map_err(map_err)?
            };
            Python::with_gil(|py| Ok(PyBytes::new(py, &bytes).unbind()))
        })
    }

    fn current_url<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.current_url().await.map_err(map_err)
        })
    }

    fn dom_snapshot<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let snap = {
                let guard = inner.lock().await;
                let s = guard
                    .as_ref()
                    .ok_or_else(|| map_err("session not started — call start() first"))?;
                s.dom_snapshot().await.map_err(map_err)?
            };
            Ok(DomState { inner: snap })
        })
    }

    fn click_index<'py>(&self, py: Python<'py>, index: u32) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.click_index(index).await.map_err(map_err)?;
            Ok(())
        })
    }

    fn upload_file<'py>(
        &self,
        py: Python<'py>,
        index: u32,
        paths: Vec<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.upload_file(index, &paths).await.map_err(map_err)?;
            Ok(())
        })
    }

    fn type_index<'py>(
        &self,
        py: Python<'py>,
        index: u32,
        text: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.type_index(index, &text).await.map_err(map_err)?;
            Ok(())
        })
    }

    fn scroll<'py>(&self, py: Python<'py>, dy: f64) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.scroll(dy).await.map_err(map_err)?;
            Ok(())
        })
    }

    fn scroll_to_index<'py>(
        &self,
        py: Python<'py>,
        index: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.scroll_to_index(index).await.map_err(map_err)?;
            Ok(())
        })
    }

    fn scroll_to_top<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.scroll_to_top().await.map_err(map_err)?;
            Ok(())
        })
    }

    fn scroll_to_bottom<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.scroll_to_bottom().await.map_err(map_err)?;
            Ok(())
        })
    }

    fn get_text<'py>(&self, py: Python<'py>, selector: String) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.get_text(&selector).await.map_err(map_err)
        })
    }

    #[pyo3(signature = (max_chars=10000))]
    fn page_text<'py>(
        &self,
        py: Python<'py>,
        max_chars: usize,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.page_text(max_chars).await.map_err(map_err)
        })
    }

    fn get_links<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.get_links().await.map_err(map_err)
        })
    }

    #[pyo3(signature = (timeout_ms=10000))]
    fn wait_for_navigation<'py>(
        &self,
        py: Python<'py>,
        timeout_ms: u64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.wait_for_navigation(timeout_ms).await.map_err(map_err)
        })
    }

    #[pyo3(signature = (selector, timeout_ms=5000))]
    fn wait_for_selector<'py>(
        &self,
        py: Python<'py>,
        selector: String,
        timeout_ms: u64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.wait_for_selector(&selector, timeout_ms)
                .await
                .map_err(map_err)
        })
    }

    fn list_tabs<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            let tabs = s.list_tabs().await.map_err(map_err)?;
            Ok(tabs
                .into_iter()
                .map(|t| (t.target_id, t.url, t.title, t.target_type, t.is_active))
                .collect::<Vec<_>>())
        })
    }

    fn switch_tab<'py>(
        &self,
        py: Python<'py>,
        target_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.switch_tab(&target_id).await.map_err(map_err)?;
            Ok(())
        })
    }

    #[pyo3(signature = (url=String::new()))]
    fn new_tab<'py>(&self, py: Python<'py>, url: String) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            let t = s.new_tab(&url).await.map_err(map_err)?;
            Ok((t.target_id, t.url, t.title, t.target_type, t.is_active))
        })
    }

    fn close_tab<'py>(
        &self,
        py: Python<'py>,
        target_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.close_tab(&target_id).await.map_err(map_err)?;
            Ok(())
        })
    }

    fn download_dir<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            Ok(s.download_dir().to_string_lossy().to_string())
        })
    }

    fn get_cookies<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            let cookies = s.get_cookies().await.map_err(map_err)?;
            Ok(cookies
                .into_iter()
                .map(|c| {
                    (
                        c.name,
                        c.value,
                        c.domain,
                        c.path,
                        c.expires,
                        c.secure,
                        c.http_only,
                    )
                })
                .collect::<Vec<_>>())
        })
    }

    #[pyo3(signature = (name, value, domain, path=String::from("/"), expires=-1.0, secure=false, http_only=false))]
    #[allow(clippy::too_many_arguments)]
    fn set_cookie<'py>(
        &self,
        py: Python<'py>,
        name: String,
        value: String,
        domain: String,
        path: String,
        expires: f64,
        secure: bool,
        http_only: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            let cookie = bu_browser::Cookie {
                name,
                value,
                domain,
                path,
                expires,
                secure,
                http_only,
            };
            s.set_cookie(&cookie).await.map_err(map_err)?;
            Ok(())
        })
    }

    #[pyo3(signature = (name, domain=None, path=None))]
    fn delete_cookie<'py>(
        &self,
        py: Python<'py>,
        name: String,
        domain: Option<String>,
        path: Option<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.delete_cookie(&name, domain.as_deref(), path.as_deref())
                .await
                .map_err(map_err)?;
            Ok(())
        })
    }

    fn clear_cookies<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            s.clear_cookies().await.map_err(map_err)?;
            Ok(())
        })
    }

    fn list_downloads<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let s = guard
                .as_ref()
                .ok_or_else(|| map_err("session not started — call start() first"))?;
            let downloads = s.list_downloads().await;
            Ok(downloads
                .into_iter()
                .map(|d| {
                    (
                        d.guid,
                        d.suggested_filename,
                        d.url,
                        d.state,
                        d.received_bytes,
                        d.total_bytes,
                        d.file_path.to_string_lossy().to_string(),
                    )
                })
                .collect::<Vec<_>>())
        })
    }

    fn stop<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let taken = inner.lock().await.take();
            if let Some(s) = taken {
                s.stop().await.map_err(map_err)?;
            }
            Ok(())
        })
    }
}

// ---------- module ----------

#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let mut builder = tokio::runtime::Builder::new_multi_thread();
    builder.enable_all();
    pyo3_async_runtimes::tokio::init(builder);

    m.add_class::<BrowserSession>()?;
    m.add_class::<DomState>()?;
    m.add_class::<DomElement>()?;
    m.add_class::<Bbox>()?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    Ok(())
}
