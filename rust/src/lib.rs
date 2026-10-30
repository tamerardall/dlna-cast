//! Native helpers for dlna-cast, imported from Python as `dlna_cast_core`.
use pyo3::prelude::*;

/// Returns the CurrentTransportState value (PLAYING, STOPPED, ...) from a GetTransportInfo
/// response, or None if the XML is malformed or the element is missing or empty.
#[pyfunction]
fn parse_transport_state(xml: &str) -> Option<String> {
    let doc = roxmltree::Document::parse(xml).ok()?;
    // Compare the local name only, so a namespace prefix like <u:CurrentTransportState> still matches.
    let node = doc
        .descendants()
        .find(|n| n.tag_name().name() == "CurrentTransportState")?;
    let text = node.text()?.trim();
    if text.is_empty() {
        None
    } else {
        Some(text.to_string())
    }
}

#[pymodule]
fn dlna_cast_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_transport_state, m)?)
}
