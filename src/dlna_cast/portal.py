"""Screen capture permission and PipeWire stream from xdg-desktop-portal."""
import itertools
import json
import os
import sys

from gi.repository import Gio, GLib

from dlna_cast.log import log

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SC_IFACE = "org.freedesktop.portal.ScreenCast"
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "dlna-cast")


class ScreenCast:
    def __init__(self, extend):
        self.extend = extend
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")
        self.tokens = itertools.count(1)
        self.state_file = os.path.join(CONFIG_DIR, "extend.json" if extend else "mirror.json")
        self.session = None

    def _call(self, method, fixed, sig, options):
        """Calls a portal method, waits for the Request.Response signal, returns the results."""
        token = f"dlnacast{next(self.tokens)}"
        options["handle_token"] = GLib.Variant("s", token)
        path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"
        loop = GLib.MainLoop()
        out = {}

        def on_response(_c, _s, _o, _i, _sig, params):
            out["r"] = params.unpack()
            loop.quit()

        sub = self.bus.signal_subscribe(PORTAL, "org.freedesktop.portal.Request", "Response", path,
                                        None, Gio.DBusSignalFlags.NONE, on_response)
        self.bus.call_sync(PORTAL, PORTAL_PATH, SC_IFACE, method,
                           GLib.Variant(sig, (*fixed, options)), None, Gio.DBusCallFlags.NONE, -1, None)
        loop.run()
        self.bus.signal_unsubscribe(sub)
        code, results = out["r"]
        if code != 0:
            log(f"Portal {method} was cancelled or failed (code {code}).")
            sys.exit(1)
        return results

    def _load_token(self):
        try:
            with open(self.state_file) as f:
                return json.load(f).get("restore_token")
        except (OSError, ValueError):
            return None

    def _save_token(self, tok):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump({"restore_token": tok}, f)

    def open(self):
        """Asks for (or restores) the permission; returns (PipeWire node id, PipeWire fd)."""
        res = self._call("CreateSession", (), "(a{sv})",
                         {"session_handle_token": GLib.Variant("s", "dlnacast_session")})
        self.session = res["session_handle"]
        opts = {
            "types": GLib.Variant("u", 4 if self.extend else 1),  # 1 = existing monitor, 4 = virtual monitor
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", 2),  # cursor embedded in the image
            "persist_mode": GLib.Variant("u", 2),  # remember the permission
        }
        tok = self._load_token()
        if tok:
            opts["restore_token"] = GLib.Variant("s", tok)
        self._call("SelectSources", (self.session,), "(oa{sv})", opts)
        log("Confirm in the screen sharing dialog..." if self.extend
            else "Pick a screen in the screen sharing dialog and confirm...")
        res = self._call("Start", (self.session, ""), "(osa{sv})", {})
        if res.get("restore_token"):
            self._save_token(res["restore_token"])
        node_id = res["streams"][0][0]
        reply, fds = self.bus.call_with_unix_fd_list_sync(
            PORTAL, PORTAL_PATH, SC_IFACE, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session, {})), GLib.VariantType("(h)"),
            Gio.DBusCallFlags.NONE, -1, None, None)
        return node_id, fds.get(reply.unpack()[0])

    def close(self):
        """Ends the screen cast explicitly; KDE may otherwise keep the virtual monitor after we exit."""
        if self.session is None:
            return
        try:
            self.bus.call_sync(PORTAL, self.session, "org.freedesktop.portal.Session", "Close",
                               None, None, Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error as e:
            log("Could not close the screen sharing session:", e.message)
