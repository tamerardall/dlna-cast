#!/usr/bin/env python3
"""Mirror (or extend) a Wayland desktop to a DLNA TV.

Pipeline: xdg-desktop-portal (screen capture) -> GStreamer (H.264/HEVC + AAC, MPEG-TS)
          -> local HTTP server -> UPnP AVTransport command to the TV (SetAVTransportURI + Play).
Usage: dlna-cast.py [--extend] [--list] [TV_IP]   (the TV is found automatically if no IP is given)
  --extend: instead of mirroring, add the TV as a separate virtual monitor.
"""
import argparse
import itertools
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst

try:
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix
    unix_signal_add = GLibUnix.signal_add
except (ImportError, ValueError):  # older PyGObject
    unix_signal_add = GLib.unix_signal_add

SAMSUNG_CONTROL_PATH = ":9197/upnp/control/AVTransport1"  # used when the device description can't be read
AVT = "urn:schemas-upnp-org:service:AVTransport:1"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def parse_args():
    p = argparse.ArgumentParser(description="Mirror or extend a Wayland desktop to a DLNA TV.")
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
    return p.parse_args()


# ---------- 0) Finding the TV: SSDP + UPnP device description ----------
def discover_renderers(timeout=3):
    """Searches the network for UPnP MediaRenderers; returns a list of (ip, description URL)."""
    msg = (b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           b"MX: 2\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    found = {}
    try:
        s.sendto(msg, ("239.255.255.250", 1900))
        while True:
            data, addr = s.recvfrom(4096)
            for line in data.decode(errors="ignore").splitlines():
                if line.lower().startswith("location:"):
                    found.setdefault(addr[0], line.split(":", 1)[1].strip())
    except OSError:
        pass
    finally:
        s.close()
    return list(found.items())


def describe(location):
    """Reads the device description; returns (friendly name, AVTransport control URL) or None."""
    try:
        with urllib.request.urlopen(location, timeout=4) as r:
            root = ET.fromstring(r.read())
    except (OSError, ET.ParseError):
        return None
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    name = root.findtext(".//d:friendlyName", "?", ns)
    for svc in root.iterfind(".//d:service", ns):
        if svc.findtext("d:serviceType", "", ns).startswith("urn:schemas-upnp-org:service:AVTransport:"):
            return name, urllib.parse.urljoin(location, svc.findtext("d:controlURL", "", ns))
    return None


def find_tv(wanted_ip):
    """Returns (ip, AVTransport control URL). Exits with a hint if no TV is found."""
    renderers = discover_renderers()
    for ip, loc in renderers:
        if wanted_ip and ip != wanted_ip:
            continue
        info = describe(loc)
        if info:
            log(f"TV: {info[0]} ({ip})")
            others = [r for r in renderers if r[0] != ip]
            if others and not wanted_ip:
                log("Other renderers found (pick one with an IP argument):", ", ".join(r[0] for r in others))
            return ip, info[1]
    if wanted_ip:  # not answering SSDP (e.g. another subnet): assume the Samsung layout
        log(f"{wanted_ip} did not answer discovery, trying the Samsung default control URL.")
        return wanted_ip, f"http://{wanted_ip}{SAMSUNG_CONTROL_PATH}"
    log("No DLNA TV found. Is it on and on the same network? You can also pass its IP: dlna-cast.py 192.168.1.50")
    sys.exit(1)


ARGS = parse_args()
if ARGS.list:
    for ip, loc in discover_renderers():
        info = describe(loc)
        print(f"{ip:15}  {info[0] if info else '(no AVTransport, cannot play streams)'}")
    sys.exit(0)
EXTEND = ARGS.extend
TV_IP, TV_CONTROL = find_tv(ARGS.tv_ip)
PORT = ARGS.port
FPS = ARGS.fps
BITRATE = ARGS.bitrate  # kbit/s
dropped = [0]  # TS chunks dropped because the TV fell behind (causes artifacts)
counts = {"source": 0, "output": 0}  # real captured frames / frames sent to the TV
gap = [0.0, None]  # longest gap in data sent to the TV in the last 5 s (s), time of the last data
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "dlna-cast")
STATE_FILE = os.path.join(CONFIG_DIR, "extend.json" if EXTEND else "mirror.json")

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SC_IFACE = "org.freedesktop.portal.ScreenCast"


# ---------- 1) Portal: screen capture permission and PipeWire stream ----------
bus = Gio.bus_get_sync(Gio.BusType.SESSION)
sender = bus.get_unique_name()[1:].replace(".", "_")
counter = itertools.count(1)


def portal_call(method, fixed, sig, options):
    """Calls a portal method, waits for the Request.Response signal, returns the results."""
    token = f"dlnacast{next(counter)}"
    options["handle_token"] = GLib.Variant("s", token)
    path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    loop = GLib.MainLoop()
    out = {}

    def on_response(_c, _s, _o, _i, _sig, params):
        out["r"] = params.unpack()
        loop.quit()

    sub = bus.signal_subscribe(PORTAL, "org.freedesktop.portal.Request", "Response", path,
                               None, Gio.DBusSignalFlags.NONE, on_response)
    bus.call_sync(PORTAL, PORTAL_PATH, SC_IFACE, method,
                  GLib.Variant(sig, (*fixed, options)), None, Gio.DBusCallFlags.NONE, -1, None)
    loop.run()
    bus.signal_unsubscribe(sub)
    code, results = out["r"]
    if code != 0:
        log(f"Portal {method} was cancelled or failed (code {code}).")
        sys.exit(1)
    return results


def load_token():
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("restore_token")
    except (OSError, ValueError):
        return None


def save_token(tok):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"restore_token": tok}, f)


def open_screencast():
    res = portal_call("CreateSession", (), "(a{sv})",
                      {"session_handle_token": GLib.Variant("s", "dlnacast_session")})
    session = res["session_handle"]
    opts = {
        "types": GLib.Variant("u", 4 if EXTEND else 1),  # 1 = existing monitor, 4 = virtual monitor (extend)
        "multiple": GLib.Variant("b", False),
        "cursor_mode": GLib.Variant("u", 2),  # cursor embedded in the image
        "persist_mode": GLib.Variant("u", 2),  # remember the permission
    }
    tok = load_token()
    if tok:
        opts["restore_token"] = GLib.Variant("s", tok)
    portal_call("SelectSources", (session,), "(oa{sv})", opts)
    log("Confirm in the screen sharing dialog..." if EXTEND
        else "Pick a screen in the screen sharing dialog and confirm...")
    res = portal_call("Start", (session, ""), "(osa{sv})", {})
    if res.get("restore_token"):
        save_token(res["restore_token"])
    node_id = res["streams"][0][0]
    reply, fds = bus.call_with_unix_fd_list_sync(
        PORTAL, PORTAL_PATH, SC_IFACE, "OpenPipeWireRemote",
        GLib.Variant("(oa{sv})", (session, {})), GLib.VariantType("(h)"),
        Gio.DBusCallFlags.NONE, -1, None, None)
    fd = fds.get(reply.unpack()[0])
    return session, node_id, fd


def close_session(session):
    """Ends the screen cast explicitly; KDE may otherwise keep the virtual monitor after we exit."""
    try:
        bus.call_sync(PORTAL, session, "org.freedesktop.portal.Session", "Close",
                      None, None, Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error as e:
        log("Could not close the screen sharing session:", e.message)


def arrange_outputs():
    """--extend on KDE: KWin may switch the laptop screen off when the virtual monitor appears
    (it applies a stored layout). Keeps real screens on and puts the virtual monitor to their right."""
    if not shutil.which("kscreen-doctor"):  # not KDE: leave the layout to the desktop
        return
    for _ in range(10):  # the virtual output may not exist right after Start
        try:
            outs = json.loads(subprocess.run(["kscreen-doctor", "-j"], check=False, capture_output=True,
                                             text=True, timeout=5).stdout)["outputs"]
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
            log("Could not read the screen layout:", e)
            return
        virt = [o for o in outs if o["name"].startswith("Virtual-")]
        real = [o for o in outs if o["connected"] and not o["name"].startswith("Virtual-")]
        if virt and real:
            break
        time.sleep(0.5)
    else:
        log("Virtual monitor not found, screen layout left unchanged.")
        return
    right = max(o["pos"]["x"] + round(o["size"]["width"] / (o["scale"] or 1)) for o in real)
    # Names may contain dots (Virtual-...org.kde.konsole), so kscreen-doctor gets numeric ids
    cmd = [f"output.{o['id']}.enable" for o in real if not o["enabled"]]
    cmd += [f"output.{o['id']}.position.{right},0" for o in virt]
    r = subprocess.run(["kscreen-doctor", *cmd], check=False, capture_output=True, text=True)
    log("Virtual monitor placed to the right of the screen." if r.returncode == 0
        else f"Could not arrange screens: {(r.stdout + r.stderr).strip()[:300]}")


# ---------- 2) HTTP server: serves the MPEG-TS stream to the TV ----------
clients, clients_lock = [], threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # no content length, streams until the connection closes

    def _headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "video/mpeg")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org",
                         "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def do_HEAD(self):
        self._headers()

    def do_GET(self):
        log("Client connected:", self.client_address[0])
        self._headers()
        q = queue.Queue(maxsize=2400)  # ~760 chunks/s at 8 Mbit/s -> ~3 s buffer
        with clients_lock:
            clients.append(q)
        try:
            while True:
                self.wfile.write(q.get())
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with clients_lock:
                clients.remove(q)
            log("Client disconnected.")

    def log_message(self, *_):
        pass


def start_http():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()


# ---------- 3) GStreamer pipeline ----------
def has(element):
    return Gst.ElementFactory.find(element) is not None


def video_encoder():
    """Returns (name, pipeline fragment) for the chosen or first available encoder."""
    encoders = {
        # nvautogpuh265enc, not nvh265enc: the CUDA-mode nvh265enc dropped the live source to ~20 fps.
        # HEVC because NVENC H.264 failed with "Codec not supported" on the tested driver.
        "nvenc": ("nvautogpuh265enc",
                  (f"nvautogpuh265enc preset=p5 tune=low-latency rc-mode=cbr bitrate={BITRATE} gop-size={FPS} "
                   "zerolatency=true repeat-sequence-header=true ! h265parse config-interval=-1")),
        "vaapi": ("vah264enc",
                  (f"vah264enc rate-control=cbr bitrate={BITRATE} key-int-max={FPS} b-frames=0 ! "
                   "h264parse config-interval=-1")),
        "x264": ("x264enc",
                 (f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={BITRATE} key-int-max={FPS} "
                  "bframes=0 threads=4 ! video/x-h264,profile=main ! h264parse config-interval=-1")),
    }
    order = ["nvenc", "vaapi", "x264"] if ARGS.encoder == "auto" else [ARGS.encoder]
    for key in order:
        element, desc = encoders[key]
        if has(element):
            log(f"Video encoder: {element}")
            return desc
    log(f"Encoder {', '.join(encoders[k][0] for k in order)} not available. "
        "Install the GStreamer plugins (see README).")
    sys.exit(1)


def audio_branch():
    """Pipeline fragment that captures desktop audio as AAC, or "" when not possible."""
    try:
        sink = subprocess.run(["pactl", "get-default-sink"], check=False, capture_output=True, text=True).stdout.strip()
    except OSError:
        sink = ""
    enc = next((e for e in ("fdkaacenc", "avenc_aac", "voaacenc") if has(e)), None)
    if not sink or not enc or not has("pulsesrc"):
        log("No audio: needs pactl, pulsesrc and an AAC encoder (fdkaacenc/avenc_aac/voaacenc).")
        return ""
    return (f"pulsesrc device={sink}.monitor ! audioconvert ! audioresample ! "
            f"audio/x-raw,rate=48000,channels=2 ! {enc} bitrate=128000 ! aacparse ! "
            "queue max-size-time=2000000000 ! mux. ")


def build_pipeline(node_id, fd):
    # Keep the output independent of screen updates: a live black background drives the compositor at a
    # fixed FPS and the compositor reuses the latest screen image each frame. A still screen doesn't starve
    # the stream, and motion doesn't cause bursts.
    raw = "video/x-raw,width=1920,height=1080,format=NV12,pixel-aspect-ratio=1/1"
    desc = (
        f"pipewiresrc fd={fd} path={node_id} do-timestamp=true ! videoconvert ! "
        f"videoscale add-borders=true ! {raw} ! queue name=srcq leaky=downstream max-size-buffers=2 ! comp.sink_1 "
        f"videotestsrc is-live=true pattern=black ! {raw},framerate={FPS}/1 ! comp.sink_0 "
        f"compositor name=comp sink_0::zorder=0 sink_1::zorder=1 ! video/x-raw,format=NV12,framerate={FPS}/1 ! "  # screen above black
        f"{video_encoder()} ! queue max-size-time=2000000000 ! mux. "
        f"{audio_branch()}"
        # drop=false: dropping TS chunks means blocky video on the TV, a short wait is better
        "mpegtsmux name=mux alignment=7 ! appsink name=out emit-signals=true sync=false max-buffers=50 drop=false"
    )
    pipe = Gst.parse_launch(desc)
    appsink = pipe.get_by_name("out")

    def counter(key):
        def probe(_pad, _info):
            counts[key] += 1
            return Gst.PadProbeReturn.OK
        return probe

    pipe.get_by_name("srcq").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, counter("source"))
    pipe.get_by_name("comp").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, counter("output"))

    def on_sample(s):
        now = time.monotonic()
        if gap[1] is not None:
            gap[0] = max(gap[0], now - gap[1])
        gap[1] = now
        buf = s.emit("pull-sample").get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            data = bytes(info.data)
            buf.unmap(info)
            with clients_lock:
                for q in clients:
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        dropped[0] += 1
        return Gst.FlowReturn.OK

    appsink.connect("new-sample", on_sample)
    return pipe


# ---------- 4) UPnP commands to the TV ----------
def soap(action, body, timeout=8):
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           f'<u:{action} xmlns:u="{AVT}">{body}</u:{action}>'
           '</s:Body></s:Envelope>').encode()
    req = urllib.request.Request(TV_CONTROL, data=xml, headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{AVT}#{action}"'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((TV_IP, 9))
    ip = s.getsockname()[0]
    s.close()
    return ip


def tv_play(url):
    didl = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="1" parentID="0" restricted="1"><dc:title>Screen</dc:title>'
            '<upnp:class>object.item.videoItem</upnp:class>'
            f'<res protocolInfo="http-get:*:video/mpeg:*">{escape(url)}</res></item></DIDL-Lite>')
    st, txt = soap("SetAVTransportURI",
                   f"<InstanceID>0</InstanceID><CurrentURI>{escape(url)}</CurrentURI>"
                   f"<CurrentURIMetaData>{escape(didl)}</CurrentURIMetaData>")
    log("SetAVTransportURI ->", st, "" if st == 200 else txt[:300])
    time.sleep(1)
    st, txt = soap("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
    log("Play ->", st, "" if st == 200 else txt[:300])


def tv_state():
    """Returns the TV's AVTransport state (PLAYING, STOPPED, ...), or None if unreachable."""
    try:
        st, txt = soap("GetTransportInfo", "<InstanceID>0</InstanceID>", timeout=4)
    except OSError:
        return None
    if st != 200 or "<CurrentTransportState>" not in txt:
        return None
    return txt.split("<CurrentTransportState>")[1].split("<")[0]


def main():
    Gst.init(None)
    session, node_id, fd = open_screencast()
    log(f"Screen capture ready (PipeWire node {node_id}).")
    if EXTEND:
        arrange_outputs()
    start_http()
    pipe = build_pipeline(node_id, fd)
    loop = GLib.MainLoop()

    def on_msg(_b, m):
        if m.type == Gst.MessageType.ERROR:
            err, dbg = m.parse_error()
            log("GStreamer ERROR:", err.message, dbg or "")
            loop.quit()

    pipe.get_bus().add_signal_watch()
    pipe.get_bus().connect("message", on_msg)
    pipe.set_state(Gst.State.PLAYING)

    def stats():  # source = new frames from the screen, output = frames sent to the TV (incl. repeats), gap = stall indicator
        log(f"fps: source {counts['source'] / 5:.0f}/s, output {counts['output'] / 5:.0f}/s, "
            f"longest gap {gap[0] * 1000:.0f} ms, dropped chunks total {dropped[0]}")
        counts["source"] = counts["output"] = 0
        gap[0] = 0.0
        return True

    GLib.timeout_add_seconds(5, stats)

    url = f"http://{local_ip()}:{PORT}/screen.ts"
    log("Stream URL:", url)

    def tv_play_safe(u):
        try:
            tv_play(u)
        except OSError as e:  # TV off/unreachable: the stream stays up, the URL can be given manually later
            log("Could not reach the TV:", e)

    threading.Timer(3, tv_play_safe, args=(url,)).start()

    cur_ip = [local_ip()]

    def watch_ip():  # switching Wi-Fi/network changes the laptop's IP: give the TV the new stream URL
        try:
            ip = local_ip()
        except OSError:  # no network right now, retried when it comes back
            return True
        if ip != cur_ip[0]:
            cur_ip[0] = ip
            new_url = f"http://{ip}:{PORT}/screen.ts"
            log("Local IP changed, new stream URL:", new_url)
            threading.Thread(target=tv_play_safe, args=(new_url,), daemon=True).start()
        return True

    GLib.timeout_add_seconds(5, watch_ip)

    bad = [0]  # consecutive checks in which the TV isn't playing the stream

    def watch_tv():  # restart playback if the TV drops/stops the stream
        def check():
            state = tv_state()
            if state in ("STOPPED", "NO_MEDIA_PRESENT", "PAUSED_PLAYBACK"):
                bad[0] += 1
                if bad[0] >= 2:  # not playing for 10 s
                    bad[0] = 0
                    log(f"TV is not playing the stream ({state}), restarting.")
                    tv_play_safe(f"http://{cur_ip[0]}:{PORT}/screen.ts")
            else:  # PLAYING, TRANSITIONING (buffering) or unreachable: leave it alone
                bad[0] = 0
        threading.Thread(target=check, daemon=True).start()
        return True

    GLib.timeout_add_seconds(5, watch_tv)

    def stop(*_):
        log("Stopping...")
        loop.quit()  # quit first: if the TV is off, a failing Stop request must not block shutdown
        try:
            soap("Stop", "<InstanceID>0</InstanceID>", timeout=2)
        except OSError as e:
            log("Could not send Stop to the TV:", e)

    unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    loop.run()
    pipe.set_state(Gst.State.NULL)
    close_session(session)


if __name__ == "__main__":
    main()
