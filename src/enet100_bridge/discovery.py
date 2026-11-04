"""Find GPIB-ENET boxes with NI's UDP discovery broadcast (port 44515).

The query is a 184-byte record starting "ED" and ending "NI". Each box answers
with a broadcast from :44515 to the query's source port (the ENET/100 repeats
its answer 4 times, ~2 s apart). This never touches ports 5000/5015, so it is
safe to use as a liveness check even while the box is booting.

Because the answer is a broadcast, a host firewall that blocks inbound UDP for
this Python will silently drop it (on Windows, cancelling the "allow access"
prompt creates exactly such a Block rule).

Only the query is sent. The box also accepts settings records, but that format
is not confirmed, and a wrong one could take the box off the network.
"""

from __future__ import annotations

import socket
import struct
import sys
import time
from dataclasses import dataclass

DISCOVERY_PORT = 44515
RECORD_LEN = 184


@dataclass(frozen=True)
class BoxInfo:
    mac: str
    model: str
    hostname: str
    firmware: str
    ip: str
    netmask: str
    serial: int
    ip_mode: int = 0          # u16 at 0x9C; 0x0200 and 0x0000 both seen on a DHCP box
    addr_a6: str = "0.0.0.0"  # unconfirmed: gateway?
    addr_aa: str = "0.0.0.0"  # unconfirmed: DNS server? (the LAN's DNS server seen)
    raw: bytes = b""

    def __str__(self) -> str:
        return (f"{self.ip:<15} {self.mac}  {self.model:<14} fw {self.firmware:<4} "
                f"{self.hostname}")

    @property
    def ip_mode_text(self) -> str:
        # A box on DHCP has shown both 0x0200 and 0x0000 here, so it isn't simply
        # "DHCP on/off".
        return f"0x{self.ip_mode:04x} (meaning unknown)"


def query_record() -> bytes:
    rec = bytearray(RECORD_LEN)
    rec[0:2] = b"ED"
    rec[2:4] = b"\x01\x00"
    rec[4:6] = b"\x00\x02"
    rec[-2:] = b"NI"
    return bytes(rec)


def _cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("ascii", "replace")


def parse_record(rec: bytes) -> BoxInfo | None:
    if len(rec) != RECORD_LEN or rec[:2] != b"ED" or rec[-2:] != b"NI":
        return None
    if rec[2:4] != b"\x08\x01":          # a reply, not someone else's query
        return None
    ip = lambda o: socket.inet_ntoa(rec[o:o + 4])
    return BoxInfo(
        mac=":".join(f"{x:02X}" for x in rec[0x0E:0x14]),
        model=_cstr(rec[0x1C:0x3C]),
        hostname=_cstr(rec[0x3C:0x9C]),
        firmware=_cstr(rec[0x08:0x0A]),
        ip=ip(0x9E),
        netmask=ip(0xA2),
        serial=struct.unpack_from("!I", rec, 0x0A)[0],
        ip_mode=struct.unpack_from("!H", rec, 0x9C)[0],
        addr_a6=ip(0xA6),
        addr_aa=ip(0xAA),
        raw=bytes(rec),
    )


def local_ipv4_addresses() -> list[str]:
    """This host's IPv4 addresses, skipping loopback and link-local (169.254/16)."""
    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    # The address used for the default route, in case the host name doesn't resolve.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith(("127.", "169.254.")))


def _sockets(bind: str | None) -> list[socket.socket]:
    """Sockets to send the query from.

    On Windows, a broadcast to 255.255.255.255 leaves through one adapter only,
    so send one from each local address (Windows also delivers broadcast
    replies to a socket bound to the adapter's address). Elsewhere one socket on
    0.0.0.0 does both jobs.
    """
    if bind:
        binds = [bind]
    elif sys.platform == "win32":
        binds = local_ipv4_addresses() or [""]
    else:
        binds = [""]
    socks = []
    for b in binds:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((b, 0))
            socks.append(s)
        except OSError:
            s.close()
    return socks


# After answering a query the box ignores new queries for ~10 s (it repeats its
# answer 5 times over ~7.7 s). A query sent in that window gets no answer at all.
BUSY_WINDOW_S = 10.5


def discover(timeout: float = 12.0, target: str = "255.255.255.255",
             bind: str | None = None, want_ip: str | None = None) -> list[BoxInfo]:
    """Broadcast a query and collect distinct answers for up to `timeout` seconds.

    The query is sent again after BUSY_WINDOW_S if nothing has answered, in case
    a box was still ignoring queries after an earlier one (ours or another
    host's). With `want_ip`, returns as soon as that box has answered; otherwise
    listens for the whole `timeout` (or 3 s after the first answer).

    `target` may be a box's own address; it still answers by broadcast.
    `bind` picks the local address (adapter) to use.
    """
    import select

    socks = _sockets(bind)

    def send():
        for s in socks:
            try:
                s.sendto(query_record(), (target, DISCOVERY_PORT))
            except OSError:
                pass

    try:
        send()
        start = time.monotonic()
        deadline = start + timeout
        resend_at = start + BUSY_WINDOW_S
        found: dict[str, BoxInfo] = {}
        while socks:
            now = time.monotonic()
            if now >= deadline:
                break
            if resend_at and now >= resend_at:
                resend_at = 0
                if not found:
                    send()
            wake = min(deadline, resend_at) if resend_at else deadline
            ready, _, _ = select.select(socks, [], [], max(0.0, wake - now))
            for s in ready:
                try:
                    data, _ = s.recvfrom(2048)
                except (ConnectionResetError, OSError):   # Windows: ICMP unreachable
                    continue
                info = parse_record(data)
                if info and info.mac not in found:
                    found[info.mac] = info
                    if want_ip and info.ip == want_ip:
                        return [info]
                    # Other boxes answer within a moment of each other.
                    deadline = min(deadline, time.monotonic() + 3.0)
        return list(found.values())
    finally:
        for s in socks:
            s.close()


def is_present(host: str, timeout: float = 12.0) -> BoxInfo | None:
    """Ask the box at `host` (IP or name) to identify itself. None if it doesn't
    answer within `timeout` (which should exceed BUSY_WINDOW_S)."""
    try:
        host_ip = socket.gethostbyname(host)
    except OSError:
        return None
    for info in discover(timeout=timeout, want_ip=host_ip):
        if info.ip == host_ip:
            return info
    return None


NO_REPLY_HINT = (
    "No GPIB-ENET box answered. Boxes answer discovery with a UDP broadcast, so check:\n"
    "  - the host firewall allows inbound UDP for this program (on Windows, look for a\n"
    "    Block rule on python.exe in 'Windows Defender Firewall > Inbound Rules';\n"
    "    cancelling the 'allow access' prompt creates one)\n"
    "  - the box is on the same subnet (broadcasts don't cross routers)\n"
    "  - the box finished booting (give it about a minute after power-on)\n"
    "You can always enter the box's IP address instead.")
