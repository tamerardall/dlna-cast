"""Mirror (or extend) a Wayland desktop to a DLNA TV.

Pipeline: xdg-desktop-portal (screen capture) -> GStreamer (H.264/HEVC + AAC, MPEG-TS)
          -> local HTTP server -> UPnP AVTransport command to the TV (SetAVTransportURI + Play).
"""
__version__ = "0.1.0"
