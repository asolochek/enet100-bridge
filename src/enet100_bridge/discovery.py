"""Find GPIB-ENET boxes with NI's UDP discovery broadcast (port 44515).

The query is a 184-byte record starting "ED" and ending "NI". Each box answers
with a broadcast from :44515 to the query's source port (the ENET/100 repeats
its answer 4 times, ~2 s apart). This never touches ports 5000/5015, so it is
safe to use as a liveness check even while the box is booting.

Only the query is sent. The box also accepts settings records, but that format
is not understood, and a wrong one could take the box off the network.
"""

from __future__ import annotations

import socket
import struct
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

    def __str__(self) -> str:
        return (f"{self.ip:<15} {self.mac}  {self.model:<14} fw {self.firmware:<4} "
                f"{self.hostname}")


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
    return BoxInfo(
        mac=":".join(f"{x:02X}" for x in rec[0x0E:0x14]),
        model=_cstr(rec[0x1C:0x3C]),
        hostname=_cstr(rec[0x3C:0x9C]),
        firmware=_cstr(rec[0x08:0x0A]),
        ip=socket.inet_ntoa(rec[0x9E:0xA2]),
        netmask=socket.inet_ntoa(rec[0xA2:0xA6]),
        serial=struct.unpack_from("!I", rec, 0x0A)[0],
    )


def discover(timeout: float = 2.5, target: str = "255.255.255.255",
             bind: str = "") -> list[BoxInfo]:
    """Broadcast a query and collect distinct answers for `timeout` seconds.

    `target` may be a box's own address; it still answers by broadcast.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind((bind, 0))
        s.sendto(query_record(), (target, DISCOVERY_PORT))
        found: dict[str, BoxInfo] = {}
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            s.settimeout(left)
            try:
                data, _ = s.recvfrom(2048)
            except socket.timeout:
                break
            except ConnectionResetError:     # Windows: ICMP port unreachable
                continue
            info = parse_record(data)
            if info and info.mac not in found:
                found[info.mac] = info
        return list(found.values())
    finally:
        s.close()


def is_present(host_ip: str, timeout: float = 2.5) -> BoxInfo | None:
    """Ask the box at `host_ip` to identify itself. None if it doesn't answer."""
    deadline = time.monotonic() + timeout
    for target in (host_ip, "255.255.255.255"):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        for info in discover(timeout=min(left, timeout / 2), target=target):
            if info.ip == host_ip:
                return info
    return None
