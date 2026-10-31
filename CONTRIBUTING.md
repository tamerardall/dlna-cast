# Contributing

Thanks for helping! Issues and pull requests in English or Turkish are both fine.

## Reporting a problem (or a TV that works)

Most problems depend on the setup, so please include:

- Distribution and desktop (e.g. Fedora 44, KDE Plasma 6.5, Wayland)
- GPU and the encoder the log says it chose (`Video encoder: ...`)
- TV brand and model (`./main.py --list` shows its name)
- Mirror or `--extend` mode
- The log from start until the problem, including a few `fps:` lines

"It works on my TV model X" reports are useful too, they go into the README's tested list.

## Pull requests

- Keep it dependency-light: the standard library plus PyGObject/GStreamer.
- The code lives in `src/dlna_cast/`: `upnp` (finding and controlling the TV), `portal` (screen capture), `pipeline` (GStreamer), `server` (HTTP stream), `kde` (extend-mode layout), `cli` (options and main loop). `main.py` only starts it from a checkout.
- `rust/` is an optional native extension (PyO3) with Rust versions of the TV reply parsers and the stream server. Keep the Python code working without it: `upnp.py` and `server.py` fall back to it when `dlna_cast_core` isn't installed.
- Run `ruff check .` before sending. If you touched `rust/`, also `cargo fmt --manifest-path rust/Cargo.toml` and `cargo clippy --manifest-path rust/Cargo.toml -- -D warnings`.
- Describe how you tested it (which TV, desktop, encoder). There are no automated tests that can cover a TV, so a short note on what you saw is the test.
