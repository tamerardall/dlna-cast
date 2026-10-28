# dlna-cast

**English** | [Türkçe](README.tr.md)

Mirror your Linux desktop (or use the TV as a second monitor) to a smart TV over DLNA, with sound. No Miracast, no Chromecast, no app on the TV: it uses the TV's built-in DLNA media player.

A small Python package with no dependencies beyond PyGObject and GStreamer. It captures the screen through the Wayland screen-cast portal, encodes it with GStreamer and plays it on the TV as a live MPEG-TS stream.

- **Mirror mode:** the TV shows one of your screens.
- **Extend mode** (`--extend`): the TV becomes a separate virtual monitor; drag windows to it.
- Finds the TV automatically, recovers if the TV stops the stream or your IP changes.
- Steady 60 fps output, hardware encoding on NVIDIA (NVENC) or Intel/AMD (VA-API), x264 as a fallback.

**Latency is about 3 seconds** (TV-side buffering in DLNA). Good for videos, films and presentations; not for using the mouse on the TV.

## Requirements

- Linux with a **Wayland** session and `xdg-desktop-portal` (KDE Plasma or GNOME).
- PipeWire (default on current distributions).
- A TV that acts as a **DLNA/UPnP MediaRenderer** on the same network. Tested on a Samsung UE49KS8500 (Tizen, 2016). Most Samsung, LG, Sony and Philips smart TVs have one; reports for other models are welcome.
- Python 3.9+ with PyGObject, and GStreamer 1.20+ with the plugins below.

| | Tested |
|---|---|
| Desktop | KDE Plasma 6 (Wayland), Fedora |
| Encoder | NVENC (RTX 2060), x264 |
| TV | Samsung UE49KS8500 |

GNOME, VA-API and other TVs should work but haven't been tested yet. Extend mode needs a portal that supports virtual monitors (KDE Plasma 6 does; GNOME doesn't).

## Installation

Install the dependencies, then download the script:

**Fedora** (enable [RPM Fusion](https://rpmfusion.org/Configuration) for x264 and AAC):

    sudo dnf install python3-gobject gstreamer1-plugins-base gstreamer1-plugins-good \
        gstreamer1-plugins-bad-free gstreamer1-plugins-ugly gstreamer1-plugin-libav \
        pipewire-gstreamer pulseaudio-utils

**Ubuntu / Debian:**

    sudo apt install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav gstreamer1.0-pipewire pulseaudio-utils

**Arch:**

    sudo pacman -S python-gobject gst-plugins-base gst-plugins-good gst-plugins-bad \
        gst-plugins-ugly gst-libav gst-plugin-pipewire libpulse

Then:

    git clone https://github.com/tamerardall/dlna-cast.git
    cd dlna-cast
    ./main.py --list      # should show your TV

`./main.py` runs straight from the checkout. To get a `dlna-cast` command instead, install it with access to the system PyGObject:

    pipx install --system-site-packages .

For NVIDIA hardware encoding you need the proprietary driver; the `nvcodec` plugin is part of plugins-bad. For Intel/AMD, the `va` plugin (plugins-bad) plus the VA-API driver for your GPU.

## Usage

Turn the TV on, then:

    ./main.py             # mirror
    ./main.py --extend    # TV as a second monitor

On first start your desktop asks which screen to share. The choice is remembered in `~/.config/dlna-cast/`; delete that folder to choose again. Stop with Ctrl+C (the TV is told to stop as well).

If there are several TVs, or discovery fails, pass the TV's IP: `./main.py 192.168.1.50`. `./main.py --list` shows what was found.

### Options

| Option | Default | Notes |
|---|---|---|
| `--extend` | off | Add the TV as a virtual monitor. On KDE it is placed to the right of your screen. |
| `--encoder` | `auto` | `nvenc` (HEVC), `vaapi` (H.264), `x264` (H.264, CPU). `auto` picks the first available in this order. |
| `--bitrate` | `8000` | kbit/s. Raise to 10000–12000 if fast motion looks blocky; lower if the TV's Wi-Fi is weak. |
| `--fps` | `60` | TVs decode up to 60. |
| `--port` | `8765` | HTTP port the TV reads the stream from. |

The output is always 1920×1080; other screen sizes are scaled with black borders.

### Status line

Every 5 seconds:

    fps: source 50/s, output 60/s, longest gap 25 ms, dropped chunks total 0

- **source**: new frames from the screen. The compositor only sends frames when something changes, so a low number on a still screen is normal.
- **output**: frames sent to the TV. Should always be ~60.
- **longest gap**: longest pause in the data sent to the TV. Above ~50 ms you'll see stutter.
- **dropped chunks**: non-zero means the TV (or the network) can't keep up.

## How it works

1. The screen is captured through `xdg-desktop-portal` (the only sanctioned way on Wayland) as a PipeWire stream.
2. GStreamer encodes video (HEVC/H.264) and desktop audio (AAC) into MPEG-TS. A live black 60 fps background drives a `compositor`, so the output runs at a fixed rate even when the screen is still.
3. A small HTTP server on port 8765 serves the stream.
4. The TV is told to play that URL with UPnP `SetAVTransportURI` + `Play`. The control URL is read from the TV's device description found via SSDP.

## Security

The HTTP server listens on `0.0.0.0:8765` with **no authentication**. While it runs, anyone on the same network (including VPNs like Tailscale) can watch your screen at `http://<your-ip>:8765/screen.ts`. Stop it when you're not using it and don't run it on untrusted networks.

## Troubleshooting

- **TV not found:** TV on and on the same network/subnet? Some routers block multicast (SSDP) between Wi-Fi and Ethernet; pass the IP by hand. `./main.py --list` shows what answers.
- **Nothing on the TV:** check the log for `SetAVTransportURI -> 200`. Anything else means the TV refused the stream; please open an issue with the log. `Play -> 500` is harmless on Samsung (it starts playing by itself).
- **Stutter:** `ping` the TV and look for packet loss. Use 5 GHz Wi-Fi for the computer, move the TV closer to the router, or lower `--bitrate`.
- **Low frame rate under motion:** check which encoder the log says it chose. With NVIDIA use `nvautogpuh265enc` (what `nvenc` selects); the plain `nvh265enc` dropped the live source to ~20 fps in testing.
- **Laptop screen turns off in extend mode (KDE):** KWin sometimes applies a stored layout that disables the built-in screen when the virtual monitor appears. The script turns it back on and moves the virtual monitor to the right with `kscreen-doctor`.
- **No screen sharing dialog:** make sure a portal backend is running (`xdg-desktop-portal-kde` or `-gnome`): `busctl --user list | grep portal`.
- **No sound:** the log says what's missing (`pactl`, `pulsesrc` or an AAC encoder).

## Known limitations

- ~3 s latency; inherent to DLNA playback.
- Miracast was tried and failed with this TV (`P2P-GO-NEG-FAILURE`); DLNA is more reliable.
- On some NVIDIA drivers NVENC only encodes HEVC (`Codec not supported` for H.264), which is why `nvenc` uses HEVC. TVs older than ~2016 may not play HEVC; use `--encoder x264` then.
- If the TV's own IP changes while running, restart the script.

## Contributing

Bug reports and reports of working/non-working TVs are very welcome, see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
