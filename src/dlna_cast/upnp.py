"""Finding the TV (SSDP + device description) and sending it AVTransport commands."""
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from dlna_cast.log import log

SAMSUNG_CONTROL_PATH = ":9197/upnp/control/AVTransport1"  # used when the device description can't be read
AVT = "urn:schemas-upnp-org:service:AVTransport:1"


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
    """Returns a TV for wanted_ip, or the first renderer found. Exits with a hint if there is none."""
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
            return TV(ip, info[1])
    if wanted_ip:  # not answering SSDP (e.g. another subnet): assume the Samsung layout
        log(f"{wanted_ip} did not answer discovery, trying the Samsung default control URL.")
        return TV(wanted_ip, f"http://{wanted_ip}{SAMSUNG_CONTROL_PATH}")
    log("No DLNA TV found. Is it on and on the same network? You can also pass its IP: dlna-cast 192.168.1.50")
    sys.exit(1)


class TV:
    def __init__(self, ip, control_url):
        self.ip = ip
        self.control_url = control_url

    def soap(self, action, body, timeout=8):
        xml = ('<?xml version="1.0" encoding="utf-8"?>'
               '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
               's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
               f'<u:{action} xmlns:u="{AVT}">{body}</u:{action}>'
               '</s:Body></s:Envelope>').encode()
        req = urllib.request.Request(self.control_url, data=xml, headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{AVT}#{action}"'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode(errors="ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="ignore")

    def local_ip(self):
        """The laptop's address on the route to the TV."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((self.ip, 9))
            return s.getsockname()[0]
        finally:
            s.close()

    def play(self, url):
        didl = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
                '<item id="1" parentID="0" restricted="1"><dc:title>Screen</dc:title>'
                '<upnp:class>object.item.videoItem</upnp:class>'
                f'<res protocolInfo="http-get:*:video/mpeg:*">{escape(url)}</res></item></DIDL-Lite>')
        st, txt = self.soap("SetAVTransportURI",
                            f"<InstanceID>0</InstanceID><CurrentURI>{escape(url)}</CurrentURI>"
                            f"<CurrentURIMetaData>{escape(didl)}</CurrentURIMetaData>")
        log("SetAVTransportURI ->", st, "" if st == 200 else txt[:300])
        time.sleep(1)
        st, txt = self.soap("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
        log("Play ->", st, "" if st == 200 else txt[:300])

    def play_safe(self, url):
        try:
            self.play(url)
        except OSError as e:  # TV off/unreachable: the stream stays up, the URL can be given manually later
            log("Could not reach the TV:", e)

    def state(self):
        """Returns the TV's AVTransport state (PLAYING, STOPPED, ...), or None if unreachable."""
        try:
            st, txt = self.soap("GetTransportInfo", "<InstanceID>0</InstanceID>", timeout=4)
        except OSError:
            return None
        if st != 200 or "<CurrentTransportState>" not in txt:
            return None
        return txt.split("<CurrentTransportState>")[1].split("<")[0]

    def stop(self):
        try:
            self.soap("Stop", "<InstanceID>0</InstanceID>", timeout=2)
        except OSError as e:
            log("Could not send Stop to the TV:", e)
