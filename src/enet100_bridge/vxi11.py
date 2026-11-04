"""VXI-11 (TCP/IP Instrument Protocol) server in front of GPIB-ENET/100 boxes.

Clients open `TCPIP::<bridge>::gpib0,<pad>[,<sad>]::INSTR` (or `...::gpib0::INTFC`
for bus-level control). `gpib0` is the name given to a box in the config.
"""

from __future__ import annotations

import logging
import re
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

from . import rpc
from .box import Box, BoxUnavailable
from .enet import END, RQS, TIMO, EnetError, GpibError
from .rpc import Packer, ProcUnavailable, RpcCall

log = logging.getLogger(__name__)

DEVICE_CORE, DEVICE_ASYNC, DEVICE_INTR = 0x0607AF, 0x0607B0, 0x0607B1
VXI11_VERSION = 1

# Device_ErrorCode
NO_ERROR = 0
SYNTAX_ERROR = 1
DEVICE_NOT_ACCESSIBLE = 3
INVALID_LINK = 4
PARAMETER_ERROR = 5
CHANNEL_NOT_ESTABLISHED = 6
OPERATION_NOT_SUPPORTED = 8
OUT_OF_RESOURCES = 9
DEVICE_LOCKED = 11
NO_LOCK_HELD = 12
IO_TIMEOUT = 15
IO_ERROR = 17
INVALID_ADDRESS = 21
ABORT = 23
CHANNEL_ALREADY_ESTABLISHED = 29

# Device_Flags
FLAG_WAITLOCK, FLAG_END, FLAG_TERMCHRSET = 0x01, 0x08, 0x80
# read reasons
REASON_REQCNT, REASON_CHR, REASON_END = 0x01, 0x02, 0x04

# VXI-11.2 docmd commands
CMD_SEND_COMMAND = 0x020000
CMD_BUS_STATUS = 0x020001
CMD_ATN_CONTROL = 0x020002
CMD_REN_CONTROL = 0x020003
CMD_PASS_CONTROL = 0x020004
CMD_BUS_ADDRESS = 0x02000A
CMD_IFC_CONTROL = 0x020010

MAX_READ = 16 * 1024 * 1024

_NAME_RE = re.compile(r"^\s*([a-z][a-z0-9_-]*?)(?:\s*,\s*(\d+)(?:\s*,\s*(\d+))?)?\s*$", re.I)


GATEWAY_NAMES = ("inst0", "inst")


@dataclass(frozen=True)
class Target:
    box: str               # "" = the bridge itself (inst0)
    pad: int | None        # None = the interface itself
    sad: int = 0

    @property
    def is_gateway(self) -> bool:
        return self.box == ""

    def __str__(self) -> str:
        if self.is_gateway:
            return "inst0"
        if self.pad is None:
            return self.box
        return f"{self.box},{self.pad}" + (f",{self.sad}" if self.sad else "")


@dataclass
class Link:
    lid: int
    target: Target
    conn: rpc.ConnContext
    client_id: int
    srq_enabled: bool = False
    srq_handle: bytes = b""
    outbuf: bytearray = field(default_factory=bytearray)    # inst0 replies


class IntrChannel:
    """Our client connection to the VISA library's interrupt (SRQ) server."""

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()

    def send_srq(self, handle: bytes) -> None:
        with self.lock:
            try:
                if self.sock is None:
                    self.sock = socket.create_connection((self.host, self.port), timeout=3)
                    threading.Thread(target=self._drain, args=(self.sock,), daemon=True).start()
                xid = int(time.monotonic() * 1000) & 0xFFFFFFFF
                msg = (Packer().uint(xid).uint(rpc.CALL).uint(2).uint(DEVICE_INTR)
                       .uint(VXI11_VERSION).uint(30).uint(0).opaque(b"").uint(0).opaque(b"")
                       .opaque(handle).bytes())
                rpc.write_record(self.sock, msg)
            except OSError as e:
                log.warning("SRQ notification to %s:%d failed: %s", self.host, self.port, e)
                self.close()

    @staticmethod
    def _drain(sock: socket.socket) -> None:
        try:
            while rpc.read_record(sock) is not None:
                pass
        except (OSError, rpc.XdrError):
            pass

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


class _GpibFailure(Exception):
    def __init__(self, code: int, data: bytes = b""):
        self.code, self.data = code, data


@dataclass
class _SrqWatch:
    thread: threading.Thread
    rearm: threading.Event = field(default_factory=threading.Event)


class Vxi11Server:
    def __init__(self, boxes: dict[str, Box], aliases: dict[str, str] | None = None,
                 max_recv_size: int = 4096):
        self.boxes = {k.lower(): v for k, v in boxes.items()}
        self.aliases = {k.lower(): v for k, v in (aliases or {}).items()}
        self.max_recv_size = max_recv_size
        self.abort_port = 0
        self.links: dict[int, Link] = {}
        self._next_lid = 1
        self._lock = threading.Lock()
        self._lock_owner: dict[Target, int] = {}
        self._lock_cond = threading.Condition(self._lock)
        self._intr: dict[int, IntrChannel] = {}         # id(conn) -> channel
        self._srq: dict[Target, _SrqWatch] = {}
        self._stopping = threading.Event()

    # -- name parsing --------------------------------------------------------------------

    def parse_target(self, name: str) -> Target | None:
        name = self.aliases.get(name.strip().lower(), name)
        if name.strip().lower() in GATEWAY_NAMES:
            # Discovery tools open inst0 and send *IDN?; answer as the bridge.
            return Target("", None)
        m = _NAME_RE.match(name)
        if not m:
            return None
        box, pad, sad = m.group(1).lower(), m.group(2), m.group(3)
        if box not in self.boxes:
            return None
        if pad is None:
            return Target(box, None)
        pad_i, sad_i = int(pad), int(sad) if sad else 0
        if not 0 <= pad_i <= 30 or not 0 <= sad_i <= 30:
            return None
        return Target(box, pad_i, sad_i)

    # -- locking -------------------------------------------------------------------------

    def _acquire(self, link: Link, flags: int, lock_timeout_ms: int) -> None:
        """Wait (if allowed) until no *other* link holds the device lock."""
        with self._lock_cond:
            owner = self._lock_owner.get(link.target)
            if owner is None or owner == link.lid:
                return
            if not flags & FLAG_WAITLOCK:
                raise _GpibFailure(DEVICE_LOCKED)
            deadline = time.monotonic() + lock_timeout_ms / 1000.0
            while True:
                owner = self._lock_owner.get(link.target)
                if owner is None or owner == link.lid:
                    return
                left = deadline - time.monotonic()
                if left <= 0:
                    raise _GpibFailure(DEVICE_LOCKED)
                self._lock_cond.wait(left)

    def _release_lock_of(self, lid: int) -> None:
        with self._lock_cond:
            for t, owner in list(self._lock_owner.items()):
                if owner == lid:
                    del self._lock_owner[t]
            self._lock_cond.notify_all()

    # -- helpers ---------------------------------------------------------------------------

    def _link(self, lid: int) -> Link:
        link = self.links.get(lid)
        if link is None:
            raise _GpibFailure(INVALID_LINK)
        return link

    def _run(self, link: Link, fn):
        """Run fn(box) for a link, mapping failures to VXI-11 error codes."""
        box = self.boxes[link.target.box]
        try:
            result = fn(box)
            box.touch()
            return result
        except BoxUnavailable as e:
            log.warning("%s: %s", link.target, e)
            raise _GpibFailure(IO_ERROR)
        except EnetError as e:
            box.note_session_lost(e)
            raise _GpibFailure(IO_ERROR)
        except GpibError as e:
            code = IO_TIMEOUT if e.timed_out else IO_ERROR
            log.info("%s: %s", link.target, e)
            raise _GpibFailure(code, getattr(e, "data", b""))

    def _device(self, box: Box, link: Link, io_timeout_ms: int | None = None):
        if link.target.pad is None:
            raise _GpibFailure(OPERATION_NOT_SUPPORTED)
        d = box.device(link.target.pad, link.target.sad)
        if io_timeout_ms is not None:
            d.set_timeout(max(io_timeout_ms, 1) / 1000.0)
        return d

    # -- core channel -------------------------------------------------------------------------

    def core(self, call: RpcCall) -> bytes:
        a = call.args
        p = call.proc
        if p == 0:
            return b""
        try:
            if p == 10:
                return self._create_link(call)
            if p == 11:
                return self._device_write(a)
            if p == 12:
                return self._device_read(a)
            if p == 13:
                return self._device_readstb(a)
            if p in (14, 15, 16, 17):
                return self._generic(p, a)
            if p == 18:
                return self._device_lock(a)
            if p == 19:
                return self._device_unlock(a)
            if p == 20:
                return self._enable_srq(a)
            if p == 22:
                return self._docmd(a)
            if p == 23:
                return self._destroy_link(a)
            if p == 25:
                return self._create_intr_chan(call)
            if p == 26:
                return self._destroy_intr_chan(call)
        except _GpibFailure as f:
            return self._error_reply(p, f.code, f.data)
        raise ProcUnavailable()

    @staticmethod
    def _error_reply(proc: int, code: int, data: bytes = b"") -> bytes:
        pk = Packer().int(code)
        if proc == 10:
            pk.int(0).uint(0).uint(0)
        elif proc == 11:
            pk.uint(0)
        elif proc == 12:
            pk.int(0).opaque(data)
        elif proc == 13:
            pk.uint(0)
        elif proc == 22:
            pk.opaque(b"")
        return pk.bytes()

    def _create_link(self, call: RpcCall) -> bytes:
        a = call.args
        client_id, lock_device, lock_timeout, device = a.int(), a.bool(), a.uint(), a.string()
        target = self.parse_target(device)
        if target is None:
            log.warning("create_link: unknown device %r", device)
            raise _GpibFailure(DEVICE_NOT_ACCESSIBLE)
        if not target.is_gateway:
            box = self.boxes[target.box]
            try:
                box.ensure_open()
            except BoxUnavailable as e:
                log.warning("create_link %s: %s", device, e)
                raise _GpibFailure(DEVICE_NOT_ACCESSIBLE)
        with self._lock:
            lid = self._next_lid
            self._next_lid += 1
            link = Link(lid, target, call.conn, client_id)
            self.links[lid] = link
        if call.conn is not None:
            call.conn.on_close.append(lambda: self._drop_link(lid))
        if lock_device:
            try:
                self._acquire(link, FLAG_WAITLOCK, lock_timeout)
            except _GpibFailure:
                self._drop_link(lid)
                raise
            with self._lock:
                self._lock_owner[target] = lid
        log.info("link %d: %s from %s", lid, target, call.conn.peer[0] if call.conn else "?")
        return (Packer().int(NO_ERROR).int(lid).uint(self.abort_port)
                .uint(self.max_recv_size).bytes())

    def _drop_link(self, lid: int) -> None:
        with self._lock:
            link = self.links.pop(lid, None)
        if link is None:
            return
        self._release_lock_of(lid)
        log.info("link %d closed (%s)", lid, link.target)

    def _destroy_link(self, a) -> bytes:
        lid = a.int()
        self._link(lid)
        self._drop_link(lid)
        return Packer().int(NO_ERROR).bytes()

    def _device_write(self, a) -> bytes:
        lid, io_timeout, lock_timeout, flags, data = a.int(), a.uint(), a.uint(), a.int(), a.opaque()
        link = self._link(lid)
        self._acquire(link, flags, lock_timeout)
        if link.target.is_gateway:
            self._gateway_write(link, data)
            return Packer().int(NO_ERROR).uint(len(data)).bytes()

        def op(box: Box):
            if link.target.pad is None:
                raise _GpibFailure(OPERATION_NOT_SUPPORTED)
            with box.lock:
                d = self._device(box, link, io_timeout)
                return d.write(data, end=bool(flags & FLAG_END)).cnt

        n = self._run(link, op)
        return Packer().int(NO_ERROR).uint(n).bytes()

    def _device_read(self, a) -> bytes:
        lid, size, io_timeout, lock_timeout, flags, term = (a.int(), a.uint(), a.uint(),
                                                             a.uint(), a.int(), a.int())
        link = self._link(lid)
        self._acquire(link, flags, lock_timeout)
        size = max(1, min(size, MAX_READ))
        termchar = term & 0xFF if flags & FLAG_TERMCHRSET else None
        if link.target.is_gateway:
            if not link.outbuf:
                raise _GpibFailure(IO_TIMEOUT)
            data = bytes(link.outbuf[:size])
            del link.outbuf[:size]
            reason = REASON_END if not link.outbuf else REASON_REQCNT
            if reason == REASON_END and termchar is not None and data.endswith(bytes([termchar])):
                reason |= REASON_CHR
            return Packer().int(NO_ERROR).int(reason).opaque(data).bytes()

        def op(box: Box):
            with box.lock:
                d = self._device(box, link, io_timeout)
                d.set_read_termination(termchar)
                return d.read(size)

        data, st = self._run(link, op)
        reason = 0
        if st.sta & END:
            reason |= REASON_END
            if termchar is not None and data.endswith(bytes([termchar])):
                reason |= REASON_CHR
        if len(data) >= size and not reason:
            reason = REASON_REQCNT
        return Packer().int(NO_ERROR).int(reason).opaque(data).bytes()

    def _device_readstb(self, a) -> bytes:
        lid, flags, lock_timeout, io_timeout = a.int(), a.int(), a.uint(), a.uint()
        link = self._link(lid)
        self._acquire(link, flags, lock_timeout)
        if link.target.is_gateway:
            return Packer().int(NO_ERROR).uint(0x10 if link.outbuf else 0).bytes()   # MAV

        def op(box: Box):
            with box.lock:
                return self._device(box, link, io_timeout).serial_poll()

        stb = self._run(link, op)
        w = self._srq.get(link.target)
        if w:
            w.rearm.set()
        return Packer().int(NO_ERROR).uint(stb).bytes()

    def _generic(self, proc: int, a) -> bytes:
        lid, flags, lock_timeout, io_timeout = a.int(), a.int(), a.uint(), a.uint()
        link = self._link(lid)
        self._acquire(link, flags, lock_timeout)
        t = link.target
        if t.is_gateway:
            if proc == 15:                  # clear
                link.outbuf.clear()
            return Packer().int(NO_ERROR).bytes()

        def op(box: Box):
            with box.lock:
                if t.pad is None:
                    box.ensure_open()
                    e = box.enet
                    if proc == 14:          # trigger: GET to whoever is addressed
                        e.ibcmd(b"\x08")
                    elif proc == 15:        # clear: IFC
                        e.ibsic()
                    elif proc == 16:        # remote: assert REN
                        e.set_ren(True)
                    else:                   # local: release REN
                        e.set_ren(False)
                    return
                d = self._device(box, link, io_timeout)
                if proc == 14:
                    d.trigger()
                elif proc == 15:
                    d.clear()
                elif proc == 16:
                    box.enet.set_ren(True)
                    listen = [0x20 + t.pad] + ([0x60 + t.sad] if t.sad else [])
                    box.enet.ibcmd(bytes([0x3F, 0x5F, 0x40 + box.cfg.gpib_address] + listen))
                    box.enet.ibcmd(b"\x3F\x5F")
                else:
                    d.local()

        self._run(link, op)
        return Packer().int(NO_ERROR).bytes()

    def _device_lock(self, a) -> bytes:
        lid, flags, lock_timeout = a.int(), a.int(), a.uint()
        link = self._link(lid)
        with self._lock_cond:
            owner = self._lock_owner.get(link.target)
            if owner == lid:
                raise _GpibFailure(DEVICE_LOCKED)
        self._acquire(link, flags, lock_timeout)
        with self._lock_cond:
            if self._lock_owner.get(link.target) not in (None, lid):
                raise _GpibFailure(DEVICE_LOCKED)
            self._lock_owner[link.target] = lid
        return Packer().int(NO_ERROR).bytes()

    def _device_unlock(self, a) -> bytes:
        link = self._link(a.int())
        with self._lock_cond:
            if self._lock_owner.get(link.target) != link.lid:
                raise _GpibFailure(NO_LOCK_HELD)
            del self._lock_owner[link.target]
            self._lock_cond.notify_all()
        return Packer().int(NO_ERROR).bytes()

    # -- SRQ ------------------------------------------------------------------------------------

    def _create_intr_chan(self, call: RpcCall) -> bytes:
        a = call.args
        host_addr, host_port, prog, vers, family = a.uint(), a.uint(), a.uint(), a.uint(), a.int()
        conn = call.conn
        if conn is None or family != 0:          # TCP only
            raise _GpibFailure(OPERATION_NOT_SUPPORTED)
        key = id(conn)
        if key in self._intr:
            raise _GpibFailure(CHANNEL_ALREADY_ESTABLISHED)
        host = socket.inet_ntoa(struct.pack("!I", host_addr)) if host_addr else conn.peer[0]
        self._intr[key] = ch = IntrChannel(host, host_port)
        conn.on_close.append(lambda: (self._intr.pop(key, None), ch.close()))
        log.info("interrupt channel for %s -> %s:%d", conn.peer[0], host, host_port)
        return Packer().int(NO_ERROR).bytes()

    def _destroy_intr_chan(self, call: RpcCall) -> bytes:
        ch = self._intr.pop(id(call.conn), None) if call.conn else None
        if ch is None:
            raise _GpibFailure(CHANNEL_NOT_ESTABLISHED)
        ch.close()
        return Packer().int(NO_ERROR).bytes()

    def _enable_srq(self, a) -> bytes:
        lid, enable, handle = a.int(), a.bool(), a.opaque()
        link = self._link(lid)
        if link.target.pad is None:
            raise _GpibFailure(OPERATION_NOT_SUPPORTED)
        link.srq_enabled = enable
        link.srq_handle = handle[:40]
        if enable:
            self._start_srq_watch(link.target)
        return Packer().int(NO_ERROR).bytes()

    def _srq_wanted(self, target: Target) -> list[Link]:
        return [l for l in list(self.links.values()) if l.target == target and l.srq_enabled]

    def _start_srq_watch(self, target: Target) -> None:
        with self._lock:
            w = self._srq.get(target)
            if w and w.thread.is_alive():
                return
            t = threading.Thread(target=self._srq_loop, args=(target,),
                                 name=f"srq-{target}", daemon=True)
            self._srq[target] = _SrqWatch(t)
            t.start()

    def _srq_loop(self, target: Target) -> None:
        box = self.boxes[target.box]
        watch = self._srq[target]
        log.info("watching SRQ on %s", target)
        while not self._stopping.is_set():
            links = self._srq_wanted(target)
            if not links:
                break
            try:
                d = box.device(target.pad, target.sad)
                t0 = time.monotonic()
                st = d.wait(RQS | TIMO)
            except BoxUnavailable:
                time.sleep(5)
                continue
            except EnetError as e:
                box.note_session_lost(e)
                time.sleep(5)
                continue
            except GpibError as e:
                log.warning("SRQ wait on %s: %s", target, e)
                time.sleep(1)
                continue
            if st.sta & RQS:
                log.info("SRQ from %s", target)
                watch.rearm.clear()
                for l in self._srq_wanted(target):
                    ch = self._intr.get(id(l.conn))
                    if ch:
                        ch.send_srq(l.srq_handle)
                # Don't re-fire until someone has read the status byte.
                watch.rearm.wait(10.0)
            elif time.monotonic() - t0 < 0.2:
                time.sleep(0.2)
        log.info("stopped watching SRQ on %s", target)
        with self._lock:
            if self._srq.get(target) is watch:
                del self._srq[target]

    # -- docmd ------------------------------------------------------------------------------------

    def _docmd(self, a) -> bytes:
        lid, flags, io_timeout, lock_timeout = a.int(), a.int(), a.uint(), a.uint()
        cmd, network_order, datasize, data_in = a.int(), a.bool(), a.int(), a.opaque()
        link = self._link(lid)
        self._acquire(link, flags, lock_timeout)
        if link.target.is_gateway:
            raise _GpibFailure(OPERATION_NOT_SUPPORTED)

        def u16(b: bytes) -> int:
            if len(b) < 2:
                return b[0] if b else 0
            return struct.unpack("!H" if network_order else "<H", b[:2])[0]

        def out16(v: int) -> bytes:
            return struct.pack("!H" if network_order else "<H", v)

        def op(box: Box) -> bytes:
            with box.lock:
                box.ensure_open()
                e = box.enet
                if cmd == CMD_SEND_COMMAND:
                    e.ibcmd(data_in)
                    return data_in
                if cmd == CMD_BUS_STATUS:
                    which = u16(data_in)
                    lines, st = e.iblines()
                    value = {
                        1: bool(lines & 0x1000),        # REN
                        2: bool(lines & 0x2000),        # SRQ
                        3: bool(lines & 0x0200),        # NDAC
                        4: box.cfg.system_controller,   # system controller
                        5: bool(st.sta & 0x0020),       # CIC
                        6: bool(st.sta & 0x0008),       # talker
                        7: bool(st.sta & 0x0004),       # listener
                        8: box.cfg.gpib_address,        # our bus address
                    }.get(which)
                    if value is None:
                        raise _GpibFailure(PARAMETER_ERROR)
                    return out16(int(value))
                if cmd == CMD_REN_CONTROL:
                    e.set_ren(bool(u16(data_in)))
                    return data_in
                if cmd == CMD_IFC_CONTROL:
                    e.ibsic()
                    return b""
                raise _GpibFailure(OPERATION_NOT_SUPPORTED)

        out = self._run(link, op)
        return Packer().int(NO_ERROR).opaque(out).bytes()

    # -- inst0: the bridge's own identity ----------------------------------------------------

    def identity(self) -> str:
        from . import __version__
        # *IDN? fields are comma-separated, so the box list uses "/".
        boxes = "/".join(sorted(self.boxes)) or "none"
        return (f"enet100-bridge,GPIB-ENET/100 VXI-11 bridge ({boxes}),"
                f"{socket.gethostname()},{__version__}")

    def _gateway_write(self, link: Link, data: bytes) -> None:
        text = data.decode("latin-1", "replace")
        for cmd in re.split(r"[;\r\n]+", text):
            c = cmd.strip().upper()
            if not c:
                continue
            log.info("inst0 from %s: %s", link.conn.peer[0] if link.conn else "?", cmd.strip())
            if c == "*IDN?":
                link.outbuf += (self.identity() + "\n").encode()
            elif c in ("*RST", "*CLS"):
                link.outbuf.clear()
            elif c == "*OPC?":
                link.outbuf += b"1\n"
            elif c in ("*STB?", "*ESR?"):
                link.outbuf += b"0\n"

    # -- abort channel ----------------------------------------------------------------------------

    def abort(self, call: RpcCall) -> bytes:
        if call.proc == 0:
            return b""
        if call.proc != 1:
            raise ProcUnavailable()
        lid = call.args.int()
        if lid not in self.links:
            return Packer().int(INVALID_LINK).bytes()
        # An ENET/100 call in progress can't be interrupted safely (dropping the
        # connection would wedge the box); it ends at its own timeout.
        log.info("device_abort on link %d (ignored: the call ends at its timeout)", lid)
        return Packer().int(NO_ERROR).bytes()

    def stop(self) -> None:
        self._stopping.set()
        for ch in list(self._intr.values()):
            ch.close()
