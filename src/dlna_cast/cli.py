import argparse
import signal
import sys
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

try:
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix
    unix_signal_add = GLibUnix.signal_add
except (ImportError, ValueError):  # older PyGObject
    unix_signal_add = GLib.unix_signal_add

from dlna_cast import kde, upnp
from dlna_cast.log import log
from dlna_cast.pipeline import Stats, build_pipeline
from dlna_cast.portal import ScreenCast
from dlna_cast.server import StreamServer


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="dlna-cast", description="Mirror or extend a Wayland desktop to a DLNA TV.")
    p.add_argument("tv_ip", nargs="?", help="TV IP address (default: discovered via SSDP)")
    p.add_argument("--extend", action="store_true",
                   help="add the TV as a separate virtual monitor instead of mirroring")
    p.add_argument("--genislet", dest="extend", action="store_true", help=argparse.SUPPRESS)  # old name
    p.add_argument("--list", action="store_true", help="list DLNA renderers on the network and exit")
    p.add_argument("--port", type=int, default=8765, help="HTTP port for the stream (default 8765)")
    p.add_argument("--fps", type=int, default=60, help="output frame rate (default 60)")
    p.add_argument("--bitrate", type=int, default=8000,
                   help="video bitrate in kbit/s (default 8000)")
    p.add_argument("--encoder", choices=["auto", "nvenc", "vaapi", "x264"], default="auto",
                   help="video encoder (default: auto = nvenc > vaapi > x264)")
    return p.parse_args(argv)


def list_renderers():
    for ip, loc in upnp.discover_renderers():
        info = upnp.describe(loc)
        print(f"{ip:15}  {info[0] if info else '(no AVTransport, cannot play streams)'}")


def start_watchers(tv, port):
    """Re-sends the stream URL when the laptop's IP changes or the TV stops playing."""
    def stream_url():
        return f"http://{current_ip}:{port}/screen.ts"

    current_ip = tv.local_ip()
    not_playing = 0  # consecutive checks in which the TV isn't playing the stream

    def watch_ip():  # switching Wi-Fi/network changes the laptop's IP
        nonlocal current_ip
        try:
            ip = tv.local_ip()
        except OSError:  # no network right now, retried when it comes back
            return True
        if ip != current_ip:
            current_ip = ip
            log("Local IP changed, new stream URL:", stream_url())
            threading.Thread(target=tv.play_safe, args=(stream_url(),), daemon=True).start()
        return True

    def check_tv():
        nonlocal not_playing
        state = tv.state()
        if state in ("STOPPED", "NO_MEDIA_PRESENT", "PAUSED_PLAYBACK"):
            not_playing += 1
            if not_playing >= 2:  # not playing for 10 s
                not_playing = 0
                log(f"TV is not playing the stream ({state}), restarting.")
                tv.play_safe(stream_url())
        else:  # PLAYING, TRANSITIONING (buffering) or unreachable: leave it alone
            not_playing = 0

    def watch_tv():
        threading.Thread(target=check_tv, daemon=True).start()
        return True

    GLib.timeout_add_seconds(5, watch_ip)
    GLib.timeout_add_seconds(5, watch_tv)
    return stream_url()


def main(argv=None):
    args = parse_args(argv)
    if args.list:
        list_renderers()
        sys.exit(0)
    tv = upnp.find_tv(args.tv_ip)

    Gst.init(None)
    screencast = ScreenCast(args.extend)
    node_id, fd = screencast.open()
    log(f"Screen capture ready (PipeWire node {node_id}).")
    if args.extend:
        kde.arrange_outputs()

    server = StreamServer(args.port)
    server.start()
    stats = Stats()
    pipe = build_pipeline(node_id, fd, fps=args.fps, bitrate=args.bitrate, encoder=args.encoder,
                          stats=stats, on_data=server.broadcast)
    loop = GLib.MainLoop()

    def on_msg(_b, m):
        if m.type == Gst.MessageType.ERROR:
            err, dbg = m.parse_error()
            log("GStreamer ERROR:", err.message, dbg or "")
            loop.quit()

    pipe.get_bus().add_signal_watch()
    pipe.get_bus().connect("message", on_msg)
    pipe.set_state(Gst.State.PLAYING)

    def report():
        log(f"fps: source {stats.source / 5:.0f}/s, output {stats.output / 5:.0f}/s, "
            f"longest gap {stats.longest_gap * 1000:.0f} ms, dropped chunks total {server.dropped}")
        stats.reset()
        return True

    GLib.timeout_add_seconds(5, report)

    url = start_watchers(tv, args.port)
    log("Stream URL:", url)
    threading.Timer(3, tv.play_safe, args=(url,)).start()

    def stop(*_):
        log("Stopping...")
        loop.quit()  # quit first: if the TV is off, a failing Stop request must not block shutdown
        tv.stop()

    unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    loop.run()
    pipe.set_state(Gst.State.NULL)
    screencast.close()
