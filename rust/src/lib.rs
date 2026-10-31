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

const AVTRANSPORT_PREFIX: &str = "urn:schemas-upnp-org:service:AVTransport:";

/// Trimmed text of the first direct child of `node` named `name`.
fn child_text<'a>(node: roxmltree::Node<'a, '_>, name: &str) -> Option<&'a str> {
    let child = node.children().find(|c| c.tag_name().name() == name)?;
    child.text().map(str::trim)
}

/// Reads a UPnP device description; returns (friendly name, absolute AVTransport control URL),
/// or None if the XML is malformed or the device has no AVTransport service.
#[pyfunction]
fn parse_description(xml: &str, location: &str) -> Option<(String, String)> {
    let doc = roxmltree::Document::parse(xml).ok()?;
    let base = url::Url::parse(location).ok()?;
    let name = doc
        .descendants()
        .find(|n| n.tag_name().name() == "friendlyName")
        .and_then(|n| n.text())
        .unwrap_or("?");
    for svc in doc.descendants().filter(|n| n.tag_name().name() == "service") {
        let is_avtransport = child_text(svc, "serviceType").is_some_and(|t| t.starts_with(AVTRANSPORT_PREFIX));
        if !is_avtransport {
            continue;
        }
        let Some(control) = child_text(svc, "controlURL") else {
            continue;
        };
        let control_url = base.join(control).ok()?;
        return Some((name.to_string(), control_url.to_string()));
    }
    None
}

#[pymodule]
fn dlna_cast_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_transport_state, m)?)?;
    m.add_function(wrap_pyfunction!(parse_description, m)?)
}
