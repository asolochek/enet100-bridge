"""Advertise the bridge over mDNS / DNS-SD as a VXI-11 device, as LXI instruments do.

Service type `_vxi-11._tcp`, port 111 (the portmapper), which is what VISA
discovery tools browse for.

- Windows: registered with the operating system's own mDNS responder through
  DnsServiceRegister (dnsapi.dll, Windows 10 1809 and later), so nothing else
  has to bind UDP 5353.
- Linux: an Avahi service file in /etc/avahi/services, if Avahi is installed.
- Elsewhere: not advertised (VXI-11 broadcast discovery still works).
"""

from __future__ import annotations

import ctypes
import logging
import os
import socket
import struct
import sys
from pathlib import Path

log = logging.getLogger(__name__)

SERVICE_TYPE = "_vxi-11._tcp"
AVAHI_FILE = Path("/etc/avahi/services/enet100-bridge.service")


def default_instance_name() -> str:
    return f"enet100-bridge on {socket.gethostname()}"


class Advertiser:
    def __init__(self, instance: str, port: int = 111, txt: dict[str, str] | None = None,
                 service_type: str = SERVICE_TYPE):
        self.instance = instance
        self.port = port
        self.txt = txt or {}
        self.service_type = service_type
        self._impl = None

    def start(self) -> bool:
        try:
            if sys.platform == "win32":
                self._impl = _WindowsDnsSd(self.instance, self.port, self.txt, self.service_type)
            elif AVAHI_FILE.parent.is_dir():
                self._impl = _AvahiFile(self.instance, self.port, self.txt, self.service_type)
            else:
                log.info("mDNS: no Avahi on this system; not advertising "
                         "(VXI-11 broadcast discovery still works)")
                return False
            self._impl.start()
            log.info("mDNS: advertising %r as %s on port %d", self.instance, self.service_type,
                     self.port)
            return True
        except Exception as e:
            log.warning("mDNS: couldn't advertise the bridge: %s", e)
            self._impl = None
            return False

    def stop(self) -> None:
        if self._impl:
            try:
                self._impl.stop()
            except Exception as e:
                log.debug("mDNS: stop failed: %s", e)
            self._impl = None


# -- Linux: Avahi -------------------------------------------------------------------------------

class _AvahiFile:
    def __init__(self, instance, port, txt, service_type):
        self.instance, self.port, self.txt = instance, port, txt
        self.service_type = service_type
        self.path = AVAHI_FILE if service_type == SERVICE_TYPE else AVAHI_FILE.with_name(
            f"enet100-bridge-{service_type.strip('_').split('.')[0]}.service")

    def start(self):
        from xml.sax.saxutils import escape
        recs = "".join(f"    <txt-record>{escape(k)}={escape(v)}</txt-record>\n"
                       for k, v in self.txt.items())
        self.path.write_text(
            '<?xml version="1.0" standalone="no"?>\n'
            '<!DOCTYPE service-group SYSTEM "avahi-service.dtd">\n'
            "<!-- written by enet100-bridge; removed when it stops -->\n"
            "<service-group>\n"
            f"  <name>{escape(self.instance)}</name>\n"
            "  <service>\n"
            f"    <type>{self.service_type}</type>\n"
            f"    <port>{self.port}</port>\n"
            f"{recs}"
            "  </service>\n"
            "</service-group>\n", encoding="utf-8")

    def stop(self):
        self.path.unlink(missing_ok=True)


# -- Windows: DnsServiceRegister ------------------------------------------------------------------

if sys.platform == "win32":
    from ctypes import wintypes

    class DNS_SERVICE_INSTANCE(ctypes.Structure):
        _fields_ = [
            ("pszInstanceName", wintypes.LPWSTR),
            ("pszHostName", wintypes.LPWSTR),
            ("ip4Address", ctypes.POINTER(wintypes.DWORD)),
            ("ip6Address", ctypes.c_void_p),
            ("wPort", wintypes.WORD),
            ("wPriority", wintypes.WORD),
            ("wWeight", wintypes.WORD),
            ("dwPropertyCount", wintypes.DWORD),
            ("keys", ctypes.POINTER(wintypes.LPWSTR)),
            ("values", ctypes.POINTER(wintypes.LPWSTR)),
            ("dwInterfaceIndex", wintypes.DWORD),
        ]

    REGISTER_COMPLETE = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.c_void_p,
                                           ctypes.POINTER(DNS_SERVICE_INSTANCE))

    class DNS_SERVICE_REGISTER_REQUEST(ctypes.Structure):
        _fields_ = [
            ("Version", wintypes.ULONG),
            ("InterfaceIndex", wintypes.ULONG),
            ("pServiceInstance", ctypes.POINTER(DNS_SERVICE_INSTANCE)),
            ("pRegisterCompletionCallback", REGISTER_COMPLETE),
            ("pQueryContext", ctypes.c_void_p),
            ("hCredentials", wintypes.HANDLE),
            ("unicastEnabled", wintypes.BOOL),
        ]

    DNS_REQUEST_PENDING = 9506
    ERROR_SUCCESS = 0


class _WindowsDnsSd:
    """Registration through dnsapi.dll. The service instance is allocated by
    DnsServiceConstructInstance (Windows frees and copies it internally, so it
    must not be ctypes-owned memory) and freed after deregistration completes."""

    def __init__(self, instance, port, txt, service_type):
        self.instance, self.port, self.txt = instance, port, txt
        self.service_type = service_type
        self._req = None
        self._inst = None
        self._keep = []          # ctypes objects that must outlive the registration
        self._done = None

    def _api(self):
        dnsapi = ctypes.WinDLL("dnsapi.dll")
        if not hasattr(dnsapi, "DnsServiceRegister"):
            raise OSError("this Windows has no DnsServiceRegister (needs Windows 10 1809+)")
        P = ctypes.POINTER(DNS_SERVICE_INSTANCE)
        dnsapi.DnsServiceConstructInstance.restype = P
        dnsapi.DnsServiceConstructInstance.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
            wintypes.WORD, wintypes.WORD, wintypes.WORD, wintypes.DWORD,
            ctypes.POINTER(wintypes.LPCWSTR), ctypes.POINTER(wintypes.LPCWSTR)]
        dnsapi.DnsServiceFreeInstance.restype = None
        dnsapi.DnsServiceFreeInstance.argtypes = [P]
        dnsapi.DnsServiceRegister.restype = wintypes.DWORD
        dnsapi.DnsServiceRegister.argtypes = [ctypes.POINTER(DNS_SERVICE_REGISTER_REQUEST),
                                              ctypes.c_void_p]
        dnsapi.DnsServiceDeRegister.restype = wintypes.DWORD
        dnsapi.DnsServiceDeRegister.argtypes = [ctypes.POINTER(DNS_SERVICE_REGISTER_REQUEST),
                                                ctypes.c_void_p]
        return dnsapi

    def start(self):
        import threading
        from .discovery import local_ipv4_addresses
        dnsapi = self._api()
        n = len(self.txt)
        keys = (wintypes.LPCWSTR * max(1, n))(*self.txt.keys())
        values = (wintypes.LPCWSTR * max(1, n))(*self.txt.values())
        ip4 = None
        addrs = local_ipv4_addresses()
        if addrs:
            # IP4_ADDRESS holds the address in network byte order.
            ip4 = wintypes.DWORD(struct.unpack("<I", socket.inet_aton(_primary_ip(addrs)))[0])
        inst = dnsapi.DnsServiceConstructInstance(
            f"{self.instance}.{self.service_type}.local", f"{socket.gethostname()}.local",
            ctypes.byref(ip4) if ip4 is not None else None, None, self.port, 0, 0, n,
            ctypes.cast(keys, ctypes.POINTER(wintypes.LPCWSTR)),
            ctypes.cast(values, ctypes.POINTER(wintypes.LPCWSTR)))
        if not inst:
            raise OSError("DnsServiceConstructInstance failed")
        self._done = threading.Event()

        def done(status, ctx, pinst):
            try:
                if status != ERROR_SUCCESS:
                    log.warning("mDNS: registration call finished with status %d", status)
                else:
                    log.debug("mDNS: registration call complete")
                if pinst:
                    dnsapi.DnsServiceFreeInstance(pinst)     # ours to free, per the docs
            finally:
                self._done.set()

        cb = REGISTER_COMPLETE(done)
        req = DNS_SERVICE_REGISTER_REQUEST()
        req.Version = 1
        req.InterfaceIndex = 0
        req.pServiceInstance = inst
        req.pRegisterCompletionCallback = cb
        self._keep = [keys, values, cb, ip4]
        rc = dnsapi.DnsServiceRegister(ctypes.byref(req), None)
        if rc != DNS_REQUEST_PENDING:
            dnsapi.DnsServiceFreeInstance(inst)
            raise OSError(f"DnsServiceRegister returned {rc}")
        self._done.wait(5)
        self._dnsapi, self._req, self._inst = dnsapi, req, inst

    def stop(self):
        if self._req is None:
            return
        self._done.clear()
        rc = self._dnsapi.DnsServiceDeRegister(ctypes.byref(self._req), None)
        if rc == DNS_REQUEST_PENDING:
            self._done.wait(5)
        self._dnsapi.DnsServiceFreeInstance(self._inst)
        self._req = self._inst = None


def _primary_ip(addrs: list[str]) -> str:
    """The address of the default route, if it's one of ours, else the first."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
            if ip in addrs:
                return ip
    except OSError:
        pass
    return addrs[0]
