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
}

#[pymethods]
impl BrowserSession {
    #[new]
    fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(None)),
        }
    }

    fn start<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let s = bu_browser::BrowserSession::start().await.map_err(map_err)?;
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
