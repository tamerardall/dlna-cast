"""GStreamer pipeline: PipeWire screen + desktop audio -> MPEG-TS chunks."""
import subprocess
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from dlna_cast.log import log


class Stats:
    """Counters for the periodic fps log line; reset after each report."""

    def __init__(self):
        self.source = 0  # new frames from the screen
        self.output = 0  # frames sent to the TV, repeats included
        self.longest_gap = 0.0  # longest pause between TS chunks (s), a stall indicator
        self._last_data = None

    def data_sent(self):
        now = time.monotonic()
        if self._last_data is not None:
            self.longest_gap = max(self.longest_gap, now - self._last_data)
        self._last_data = now

    def reset(self):
        self.source = self.output = 0
        self.longest_gap = 0.0


def has(element):
    return Gst.ElementFactory.find(element) is not None


def video_encoder(choice, fps, bitrate):
    """Returns the pipeline fragment for the chosen or first available encoder."""
    encoders = {
        # nvautogpuh265enc, not nvh265enc: the CUDA-mode nvh265enc dropped the live source to ~20 fps.
        # HEVC because NVENC H.264 failed with "Codec not supported" on the tested driver.
        "nvenc": ("nvautogpuh265enc",
                  (f"nvautogpuh265enc preset=p5 tune=low-latency rc-mode=cbr bitrate={bitrate} gop-size={fps} "
                   "zerolatency=true repeat-sequence-header=true ! h265parse config-interval=-1")),
        "vaapi": ("vah264enc",
                  (f"vah264enc rate-control=cbr bitrate={bitrate} key-int-max={fps} b-frames=0 ! "
                   "h264parse config-interval=-1")),
        "x264": ("x264enc",
                 (f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate} key-int-max={fps} "
                  "bframes=0 threads=4 ! video/x-h264,profile=main ! h264parse config-interval=-1")),
    }
    order = ["nvenc", "vaapi", "x264"] if choice == "auto" else [choice]
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


def build_pipeline(node_id, fd, *, fps, bitrate, encoder, stats, on_data):
    """on_data(bytes) is called from the streaming thread for every MPEG-TS chunk."""
    # A live black source drives the compositor at a fixed FPS, reusing the latest screen frame,
    # so a still screen doesn't starve the stream and motion doesn't cause bursts.
    raw = "video/x-raw,width=1920,height=1080,format=NV12,pixel-aspect-ratio=1/1"
    desc = (
        f"pipewiresrc fd={fd} path={node_id} do-timestamp=true ! videoconvert ! "
        f"videoscale add-borders=true ! {raw} ! queue name=srcq leaky=downstream max-size-buffers=2 ! comp.sink_1 "
        f"videotestsrc is-live=true pattern=black ! {raw},framerate={fps}/1 ! comp.sink_0 "
        f"compositor name=comp sink_0::zorder=0 sink_1::zorder=1 ! video/x-raw,format=NV12,framerate={fps}/1 ! "  # screen above black
        f"{video_encoder(encoder, fps, bitrate)} ! queue max-size-time=2000000000 ! mux. "
        f"{audio_branch()}"
        # drop=false: dropping TS chunks means blocky video on the TV, a short wait is better
        "mpegtsmux name=mux alignment=7 ! appsink name=out emit-signals=true sync=false max-buffers=50 drop=false"
    )
    pipe = Gst.parse_launch(desc)

    def count_source(_pad, _info):
        stats.source += 1
        return Gst.PadProbeReturn.OK

    def count_output(_pad, _info):
        stats.output += 1
        return Gst.PadProbeReturn.OK

    pipe.get_by_name("srcq").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, count_source)
    pipe.get_by_name("comp").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, count_output)

    def on_sample(sink):
        stats.data_sent()
        buf = sink.emit("pull-sample").get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            data = bytes(info.data)
            buf.unmap(info)
            on_data(data)
        return Gst.FlowReturn.OK

    pipe.get_by_name("out").connect("new-sample", on_sample)
    return pipe
