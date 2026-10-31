//! HTTP server that fans the MPEG-TS stream out to the TV (Rust port of dlna_cast/server.py).
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{sync_channel, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};
use std::thread;

use pyo3::prelude::*;

const QUEUE_SIZE: usize = 2400; // ~760 chunks/s at 8 Mbit/s -> ~3 s buffer
const HEADERS: &[u8] = b"HTTP/1.0 200 OK\r\n\
Content-Type: video/mpeg\r\n\
transferMode.dlna.org: Streaming\r\n\
contentFeatures.dlna.org: DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000\r\n\
Cache-Control: no-cache\r\n\r\n";

/// One TS chunk, shared by every client's queue: cloning it only bumps a counter.
type Chunk = Arc<[u8]>;

struct Shared {
    clients: Mutex<Vec<SyncSender<Chunk>>>,
    dropped: AtomicU64, // chunks dropped because the TV fell behind (causes artifacts)
    log: Option<Py<PyAny>>,
}

impl Shared {
    fn log(&self, msg: &str) {
        if let Some(log) = &self.log {
            Python::attach(|py| {
                if let Err(e) = log.call1(py, (msg,)) {
                    e.print(py);
                }
            });
        }
    }
}

fn serve_client(stream: TcpStream, shared: Arc<Shared>) {
    let peer = stream.peer_addr().map(|a| a.ip().to_string()).unwrap_or_default();
    let mut reader = BufReader::new(&stream);
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).unwrap_or(0) == 0 {
        return;
    }
    loop {
        let mut line = String::new();
        match reader.read_line(&mut line) {
            Ok(0) | Err(_) => break,
            Ok(_) if line == "\r\n" || line == "\n" => break,
            Ok(_) => {}
        }
    }
    let mut out = &stream;
    if out.write_all(HEADERS).is_err() || request_line.starts_with("HEAD") {
        return;
    }

    shared.log(&format!("Client connected: {peer}"));
    let (tx, rx) = sync_channel::<Chunk>(QUEUE_SIZE);
    shared.clients.lock().unwrap().push(tx);
    for chunk in rx {
        if out.write_all(&chunk).is_err() {
            break;
        }
    }
    // rx is dropped here, so the next broadcast() sees this client's sender as disconnected and removes it.
    shared.log("Client disconnected.");
}

#[pyclass]
pub struct StreamServer {
    listener: TcpListener,
    shared: Arc<Shared>,
}

#[pymethods]
impl StreamServer {
    #[new]
    #[pyo3(signature = (port, log=None))]
    fn new(port: u16, log: Option<Py<PyAny>>) -> PyResult<Self> {
        // Keep the errno (EADDRINUSE etc.) so callers see the same OSError as with the Python server.
        let listener = TcpListener::bind(("0.0.0.0", port)).map_err(|e| {
            pyo3::exceptions::PyOSError::new_err((e.raw_os_error().unwrap_or(0), e.to_string()))
        })?;
        let shared = Arc::new(Shared {
            clients: Mutex::new(Vec::new()),
            dropped: AtomicU64::new(0),
            log,
        });
        Ok(StreamServer { listener, shared })
    }

    fn start(&self) -> PyResult<()> {
        let listener = self.listener.try_clone()?;
        let shared = Arc::clone(&self.shared);
        thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let shared = Arc::clone(&shared);
                thread::spawn(move || serve_client(stream, shared));
            }
        });
        Ok(())
    }

    fn broadcast(&self, data: &[u8]) {
        let mut clients = self.shared.clients.lock().unwrap();
        if clients.is_empty() {
            return;
        }
        let chunk: Chunk = Arc::from(data);
        clients.retain(|tx| match tx.try_send(Arc::clone(&chunk)) {
            Ok(()) => true,
            Err(TrySendError::Full(_)) => {
                self.shared.dropped.fetch_add(1, Ordering::Relaxed);
                true
            }
            Err(TrySendError::Disconnected(_)) => false,
        });
    }

    #[getter]
    fn dropped(&self) -> u64 {
        self.shared.dropped.load(Ordering::Relaxed)
    }
}
