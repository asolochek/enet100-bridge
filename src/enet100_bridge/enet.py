"""Client for the National Instruments GPIB-ENET/100 network protocol.

The protocol was worked out from packet captures of NI-488.2 17.6 talking to a
GPIB-ENET/100 (firmware REV C.9). See docs/PROTOCOL.md for the byte layouts.

Rules the box enforces (breaking them leaves it deaf until a power cycle):

* Don't talk to it in the first ~60 s after power-on.
* Never close a session that is only partly set up. A board session is complete
  once it has done 0x0B, 0x50 05, 0x07 cfg, 0x50 10 01 and bound its :5015
  channel. Complete sessions can be closed with an ordinary FIN.
* Close a device handle with 0x58 00 01 before closing its sockets.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

CMD_PORT = 5000
WAIT_PORT = 5015

# ibsta bits
ERR, TIMO, END, SRQI, RQS, CMPL = 0x8000, 0x4000, 0x2000, 0x1000, 0x0800, 0x0100
LOK, REM, CIC, ATN, TACS, LACS = 0x0080, 0x0040, 0x0020, 0x0010, 0x0008, 0x0004

# iberr codes
IBERR_NAMES = {
    0: "EDVR", 1: "ECIC", 2: "ENOL", 3: "EADR", 4: "EARG", 5: "ESAC", 6: "EABO",
    7: "ENEB", 8: "EDMA", 10: "EOIP", 11: "ECAP", 12: "EFSO", 14: "EBUS",
    15: "ESTB", 16: "ESRQ", 20: "ETAB",
}
ECIC, ENOL, EABO = 1, 2, 6

# ibconfig option codes (NI-488.2 numbering; the box uses the same values)
IBC_TMO = 0x03
IBC_EOT = 0x04
IBC_READDR = 0x06
IBC_SRE = 0x0B          # board: REN line
IBC_EOSRD = 0x0C
IBC_EOSWRT = 0x0D
IBC_EOSCMP = 0x0E
IBC_EOSCHAR = 0x0F
IBC_HSCABLE = 0x1F

# ibdev mode byte
MODE_EOT, MODE_REOS, MODE_XEOS, MODE_BIN = 0x01, 0x04, 0x08, 0x10

# NI timeout codes -> seconds
TMO_SECONDS = [
    None, 10e-6, 30e-6, 100e-6, 300e-6, 1e-3, 3e-3, 10e-3, 30e-3, 100e-3,
    300e-3, 1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0,
]

# Margin added to the box's own timeout before we decide the box is gone.
SOCKET_MARGIN_S = 8.0


def tmo_code_for(seconds: float) -> int:
    """Smallest NI timeout code that is at least `seconds` (1 = 10 us ... 17 = 1000 s)."""
    for code in range(1, len(TMO_SECONDS)):
        if TMO_SECONDS[code] >= seconds - 1e-9:
            return code
    return len(TMO_SECONDS) - 1


class EnetError(Exception):
    """The box did not answer, or answered with something we can't parse.

    After this the session is unusable and must be rebuilt.
    """


class EnetTimeout(EnetError):
    """No bytes arrived within the socket timeout."""


class GpibError(Exception):
    """The box answered with ERR set in ibsta."""

    def __init__(self, op: str, status: "Status"):
        self.op = op
        self.status = status
        super().__init__(f"{op}: {status}")

    @property
    def iberr(self) -> int:
        return self.status.err

    @property
    def timed_out(self) -> bool:
        return bool(self.status.sta & TIMO)


@dataclass
class Status:
    sta: int
    err: int
    cnt: int
    extra: bytes = b""

    @property
    def ok(self) -> bool:
        return not self.sta & ERR

    def __str__(self) -> str:
        bits = [n for n, b in (("ERR", ERR), ("TIMO", TIMO), ("END", END), ("SRQI", SRQI),
                               ("RQS", RQS), ("CMPL", CMPL), ("LOK", LOK), ("REM", REM),
                               ("CIC", CIC), ("ATN", ATN), ("TACS", TACS), ("LACS", LACS))
                if self.sta & b]
        s = f"ibsta=0x{self.sta:04x} [{' '.join(bits)}]"
        if self.sta & ERR:
            s += f" iberr={self.err}({IBERR_NAMES.get(self.err, '?')})"
        return s + f" ibcnt={self.cnt}"


@dataclass
class Identity:
    """What the box says about itself in its reply to 0x0B."""
    model: str
    vendor: str
    mac: str
    box_ip: str
    client_ip: str
    client_port: int


def _block(cmd: int, args: bytes = b"") -> bytes:
    if len(args) > 11:
        raise ValueError("command arguments are at most 11 bytes")
    return bytes([cmd]) + args + bytes(11 - len(args))


def _data_block(cmd: int, length: int) -> bytes:
    return struct.pack("!B3xI4x", cmd, length)


class _Conn:
    """One TCP connection to the box, speaking the fragment protocol."""

    def __init__(self, host: str, port: int, connect_timeout: float, name: str):
        self.name = name
        self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.local_port = self.sock.getsockname()[1]
        self.timeout = 10.0 + SOCKET_MARGIN_S

    def send(self, data: bytes) -> None:
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise EnetError(f"{self.name}: send failed: {e}") from e

    def recv_exact(self, n: int, timeout: float | None = None) -> bytes:
        self.sock.settimeout(self.timeout if timeout is None else timeout)
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout as e:
                raise EnetTimeout(f"{self.name}: no reply from the box") from e
            except OSError as e:
                raise EnetError(f"{self.name}: receive failed: {e}") from e
            if not chunk:
                raise EnetError(f"{self.name}: the box closed the connection")
            buf += chunk
        return bytes(buf)

    def frag(self, timeout: float | None = None) -> tuple[int, bytes]:
        flags, length = struct.unpack("!HH", self.recv_exact(4, timeout))
        return flags, self.recv_exact(length, timeout) if length else b""

    def status(self, timeout: float | None = None) -> Status:
        flags, payload = self.frag(timeout)
        if flags != 0 or len(payload) < 12:
            raise EnetError(f"{self.name}: expected a status fragment, got flags=0x{flags:04x} "
                            f"len={len(payload)}")
        sta, err, cnt = struct.unpack_from("!HH4xI", payload)
        return Status(sta, err, cnt, payload[12:])

    def call(self, cmd: int, args: bytes = b"", data: bytes | None = None) -> Status:
        if data is None:
            self.send(_block(cmd, args))
        else:
            self.send(_data_block(cmd, len(data)) + data)
        return self.status()

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        # Give the box a moment to send its own FIN, so it sees a tidy close.
        try:
            self.sock.settimeout(2.0)
            while self.sock.recv(4096):
                pass
        except OSError:
            pass
        self.sock.close()


class Enet100:
    """A board session to one GPIB-ENET/100.

    Keep one of these open for the life of the process; open device handles
    with :meth:`device`. All bus operations are serialised by `self.lock`.
    """

    def __init__(self, host: str, *, cmd_port: int = CMD_PORT, wait_port: int = WAIT_PORT,
                 connect_timeout: float = 5.0):
        self.host = host
        self.cmd_port = cmd_port
        self.wait_port = wait_port
        self.connect_timeout = connect_timeout
        self.lock = threading.RLock()
        self.identity: Identity | None = None
        self._board: _Conn | None = None
        self._board_wait: _Conn | None = None
        self._half_open: list[_Conn] = []   # never closed on purpose: see module docstring
        self.devices: list[Device] = []
        self.alive = False

    # -- board session -------------------------------------------------------

    def open(self) -> Identity:
        with self.lock:
            if self.alive:
                return self.identity
            b = _Conn(self.host, self.cmd_port, self.connect_timeout, "board")
            try:
                self.identity = ident = self._identify(b)
                b.call(0x50, b"\x05")
                b.call(0x07, bytes([0x00, 0x18, 0x01, 0x00, 0x00, 0x00, 0x00, 0x0D]))
                b.call(0x50, b"\x10\x01")
                w = _Conn(self.host, self.wait_port, self.connect_timeout, "board-wait")
                try:
                    w.call(0x55, b"\x02\x00\x00\x00\x00"
                           + struct.pack("!H", ident.client_port)
                           + socket.inet_aton(ident.client_ip))
                except Exception:
                    self._half_open.append(w)
                    raise
            except Exception:
                # Closing now would wedge the box. Keep the socket.
                self._half_open.append(b)
                raise
            self._board, self._board_wait = b, w
            self.alive = True
            # Same follow-up NI-488.2 sends after setup.
            self._board_call(0x12, b"\x01")          # ibonl 1
            self._board_call(0x06, bytes([IBC_HSCABLE, 0]))
            log.info("ENET/100 %s: %s, MAC %s, our id %s:%d", self.host, ident.model,
                     ident.mac, ident.client_ip, ident.client_port)
            return ident

    def _identify(self, b: _Conn) -> Identity:
        st = b.call(0x0B)
        extra = st.extra
        # "<model>\n<vendor>\n" then MAC(6) box IP(4) client IP(4) client port(2)
        first = extra.find(b"\n")
        second = extra.find(b"\n", first + 1)
        if first < 0 or second < 0 or len(extra) < second + 1 + 16:
            raise EnetError(f"unexpected identify reply: {extra!r}")
        model = extra[:first].decode("ascii", "replace").strip()
        vendor = extra[first + 1:second].decode("ascii", "replace").strip()
        p = second + 1
        mac = ":".join(f"{x:02X}" for x in extra[p:p + 6])
        box_ip = socket.inet_ntoa(extra[p + 6:p + 10])
        client_ip = socket.inet_ntoa(extra[p + 10:p + 14])
        (client_port,) = struct.unpack_from("!H", extra, p + 14)
        if client_port != b.local_port:
            log.warning("box sees our port as %d, socket says %d (NAT?)", client_port,
                        b.local_port)
        return Identity(model, vendor, mac, box_ip, client_ip, client_port)

    def close(self) -> None:
        with self.lock:
            for d in list(self.devices):
                d.close()
            if self._board_wait:
                self._board_wait.close()
            if self._board:
                self._board.close()
            self._board = self._board_wait = None
            self.alive = False

    def _board_call(self, cmd: int, args: bytes = b"", data: bytes | None = None,
                    op: str = "", check: bool = True) -> Status:
        with self.lock:
            if not self.alive or not self._board:
                raise EnetError("board session is not open")
            try:
                st = self._board.call(cmd, args, data)
            except EnetError:
                self.mark_dead()
                raise
            if check and not st.ok:
                raise GpibError(op or f"board 0x{cmd:02x}", st)
            return st

    def mark_dead(self) -> None:
        """The box stopped answering. Forget every session without closing it."""
        with self.lock:
            if self.alive:
                log.error("ENET/100 %s: session lost", self.host)
            self.alive = False
            for c in (self._board, self._board_wait):
                if c:
                    self._half_open.append(c)
            self._board = self._board_wait = None
            for d in self.devices:
                d._forget()
            self.devices.clear()

    # -- board-level GPIB operations ------------------------------------------

    def ibcmd(self, data: bytes) -> Status:
        """Send bytes with ATN asserted (multiline interface messages)."""
        return self._board_call(0x61, data=data, op="ibcmd")

    def ibsic(self) -> Status:
        """Pulse IFC."""
        return self._board_call(0x1C, op="ibsic")

    def iblines(self) -> tuple[int, Status]:
        st = self._board_call(0x0D, op="iblines")
        return struct.unpack("!H", st.extra[:2])[0], st

    def ibln(self, pad: int, sad: int = 0) -> bool:
        """Is there a listener at this address? `sad` is 0 or 0x60..0x7E."""
        st = self._board_call(0x0F, bytes([pad, sad]), op="ibln")
        return struct.unpack("!H", st.extra[:2])[0] != 0

    def find_listeners(self, addresses=range(1, 31)) -> list[int]:
        """Primary addresses that answer as listeners (no data is sent to them).

        Needs the box to be controller-in-charge. If it isn't (ECIC), send IFC
        first, as NI's FindLstn does; IFC only resets the bus interface state.
        """
        try:
            self.ibln(addresses[0] if addresses else 1)
        except GpibError as e:
            if e.iberr != ECIC:
                raise
            log.info("%s: not controller-in-charge; sending IFC", self.host)
            self.ibsic()
        return [pad for pad in addresses if self.ibln(pad)]

    def set_ren(self, on: bool) -> Status:
        return self._board_call(0x06, bytes([IBC_SRE, 1 if on else 0]), op="REN")

    def board_status(self) -> Status:
        """A harmless call that returns the board's current ibsta."""
        return self._board_call(0x4E, b"\x01", op="ibask")

    # -- device handles ---------------------------------------------------------

    def device(self, pad: int, sad: int = 0, *, timeout_s: float = 10.0, eos: int = 0x0A,
               mode: int = MODE_EOT) -> "Device":
        with self.lock:
            if not self.alive:
                self.open()
            d = Device(self, pad, sad, tmo_code_for(timeout_s), eos, mode)
            d._open()
            self.devices.append(d)
            return d


@dataclass
class Device:
    """One ibdev handle: a :5000 connection plus its :5015 wait channel."""

    board: Enet100
    pad: int
    sad: int
    tmo: int
    eos: int
    mode: int
    _conn: _Conn | None = field(default=None, repr=False)
    _wait: _Conn | None = field(default=None, repr=False)
    _wait_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    eosrd: bool = False

    def __post_init__(self):
        self.eosrd = bool(self.mode & MODE_REOS)
        self.eot = bool(self.mode & MODE_EOT)

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def _open(self) -> None:
        b = self.board
        c = _Conn(b.host, b.cmd_port, b.connect_timeout, f"dev{self.pad}")
        try:
            c.call(0x07, bytes([0x02, 0x00, self.mode, self.pad, self.sad, self.eos, 0x00,
                                self.tmo, 0x00, 0x04]))
            c.call(0x50, b"\x05")
            c.call(0x50, b"\x10\x01")
            c.call(0x50, b"\x15\x0b")
            w = _Conn(b.host, b.wait_port, b.connect_timeout, f"dev{self.pad}-wait")
            try:
                w.call(0x55, b"\x02\x00\x02\x00\x00" + struct.pack("!H", c.local_port)
                       + socket.inet_aton(b.identity.client_ip))
            except Exception:
                b._half_open.append(w)
                raise
            c.call(0x58, b"\x01\x01")
        except Exception:
            b._half_open.append(c)
            raise
        self._conn, self._wait = c, w
        self._set_socket_timeout()
        log.info("opened GPIB device %d%s on %s", self.pad,
                 f",{self.sad - 0x60}" if self.sad else "", b.host)

    def _forget(self) -> None:
        for c in (self._conn, self._wait):
            if c:
                self.board._half_open.append(c)
        self._conn = self._wait = None

    def close(self) -> None:
        with self.board.lock:
            if not self._conn:
                return
            try:
                self._conn.call(0x58, b"\x00\x01")
            except EnetError:
                self._forget()
                self.board.mark_dead()
                return
            self._wait.close()
            self._conn.close()
            self._conn = self._wait = None
            if self in self.board.devices:
                self.board.devices.remove(self)

    def _set_socket_timeout(self) -> None:
        t = (TMO_SECONDS[self.tmo] or 1000.0) + SOCKET_MARGIN_S
        if self._conn:
            self._conn.timeout = t

    def _call(self, op: str, cmd: int, args: bytes = b"", data: bytes | None = None,
              check: bool = True) -> Status:
        with self.board.lock:
            if not self._conn:
                raise EnetError(f"device {self.pad} is not open")
            try:
                st = self._conn.call(cmd, args, data)
            except EnetError:
                self.board.mark_dead()
                raise
            if check and not st.ok:
                raise GpibError(op, st)
            return st

    # -- configuration (cached, so we only send what changes) -----------------

    def ibconfig(self, opt: int, val: int) -> Status:
        return self._call(f"ibconfig 0x{opt:02x}", 0x06, bytes([opt, val & 0xFF]))

    def set_timeout(self, seconds: float) -> None:
        code = tmo_code_for(seconds)
        if code != self.tmo:
            self.ibconfig(IBC_TMO, code)
            self.tmo = code
            self._set_socket_timeout()

    def set_read_termination(self, char: int | None) -> None:
        """Stop reads on `char` (as well as on EOI), or only on EOI if None."""
        if char is None:
            if self.eosrd:
                self.ibconfig(IBC_EOSRD, 0)
                self.eosrd = False
            return
        if char != self.eos:
            self.ibconfig(IBC_EOSCHAR, char)
            self.eos = char
        if not self.eosrd:
            self.ibconfig(IBC_EOSRD, 1)
            self.eosrd = True

    def set_eot(self, on: bool) -> None:
        if on != self.eot:
            self.ibconfig(IBC_EOT, 1 if on else 0)
            self.eot = on

    # -- I/O ---------------------------------------------------------------------

    def write(self, data: bytes, end: bool = True) -> Status:
        self.set_eot(end)
        return self._call("ibwrt", 0x62, data=bytes(data))

    def read(self, maxlen: int) -> tuple[bytes, Status]:
        """ibrd. Returns (data, final status). Raises GpibError on ERR, with the
        data received so far in `.data`."""
        with self.board.lock:
            if not self._conn:
                raise EnetError(f"device {self.pad} is not open")
            c = self._conn
            try:
                c.send(_data_block(0x16, maxlen))
                first = c.status()
                chunks = []
                if first.ok:
                    while True:
                        flags, payload = c.frag()
                        if flags == 0:
                            chunks.append(payload)
                        elif flags == 1:
                            break
                        else:
                            raise EnetError(f"ibrd: unexpected fragment flags 0x{flags:04x}")
                    st = c.status()
                else:
                    # Not captured yet: an ibrd that fails before any data. Look for
                    # a trailing terminator + status so we stay in step.
                    st = first
                    try:
                        flags, payload = c.frag(timeout=1.5)
                        if flags == 1:
                            st = c.status()
                        else:
                            raise EnetError(f"ibrd error path: unexpected fragment "
                                            f"flags=0x{flags:04x}")
                    except EnetTimeout:
                        pass
            except EnetError:
                self.board.mark_dead()
                raise
            data = b"".join(chunks)
            if not st.ok:
                err = GpibError("ibrd", st)
                err.data = data
                raise err
            return data, st

    def clear(self) -> Status:
        return self._call("ibclr", 0x04)

    def trigger(self) -> Status:
        return self._call("ibtrg", 0x20)

    def local(self) -> Status:
        return self._call("ibloc", 0x10)

    def serial_poll(self) -> int:
        st = self._call("ibrsp", 0x19)
        return st.extra[0] if st.extra else 0

    def wait(self, mask: int) -> Status:
        """ibwait on the device's :5015 channel. Blocks until a bit in `mask` is set.

        Include TIMO in `mask`, or this blocks until the condition happens.
        Not under the bus lock: other calls on this device can run meanwhile.
        """
        with self._wait_lock:
            w = self._wait
            if not w:
                raise EnetError(f"device {self.pad} is not open")
            w.timeout = (TMO_SECONDS[self.tmo] or 1000.0) + SOCKET_MARGIN_S
            try:
                return w.call(0x22, b"\x00" + struct.pack("!H", mask))
            except EnetError:
                self.board.mark_dead()
                raise

