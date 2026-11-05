"""Present a GPIB-ENET/100 to NI-488.2 as a GPIB-ENET/1000.

Current NI-488.2 releases dropped the ENET/100 but still support the ENET/1000,
whose protocol is the ENET/100 one plus a few commands (docs/PROTOCOL.md,
"ENET/1000"). This module listens where an ENET/1000 does (TCP 5000, 5003,
5005, 5015 and HTTP 80) and turns NI-488.2's sessions into ENET/100 ones, so
NI MAX and Keysight Connection Expert show the box as a GPIB interface
(GPIBn) like any other.

NI-488.2 insists on two things, both found by experiment:

* the 0x0B identify reply must name a "GPIB-ENET/1000" (anything else and the
  driver hangs up and reports EDVR), and
* the product id in /deviceinfo.xml (0x753B) picks the board type, so NI's
  tools label the interface "NI GPIB-ENET/1000" whatever name is served.

The serial number, MAC and comment served are the ENET/100's own.

How sessions map:

* NI's board session uses the bridge's own ENET/100 board session (the one
  VXI-11 clients use too); there is one bus, so one set of board settings.
* Each NI device handle (ibdev) gets its own ENET/100 device handle, opened
  and closed by the bridge with the complete sequence. NI hanging up half way
  through its own setup therefore can't wedge the box.
* GPIB commands (ibwrt, ibrd, ibcmd, ibask, ibconfig, FindLstn, ...) are
  forwarded as they are; the replies have the same format. The ENET/1000-only
  commands (0x63, 0x64, 0x65, 0x4F, 0x59, the 5003/5005 channels) and session
  setup are answered here.
"""

from __future__ import annotations

import http.server
import itertools
import logging
import socket
import socketserver
import struct
import threading
import time
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from .box import Box, BoxUnavailable
from .enet import (ERR, SOCKET_MARGIN_S, TIMO, TMO_SECONDS, Device, EnetError, EnetTimeout,
                   _Conn)

log = logging.getLogger(__name__)

CMD_PORT, AUX_PORT, KEEP_PORT, WAIT_PORT, HTTP_PORT = 5000, 5003, 5005, 5015, 80

AWAIT_BOX_S = 9.0                      # under NI-488.2's ~10 s open timeout
IDN_PROBE_TMO = 11                     # 1 s: how long *IDN? (and then ID?) get to answer
PROD_ID = "0x753B"                    # GPIB-ENET/1000; NI picks the board type from this
FIRMWARE = "1.2.1f0"                   # what a current ENET/1000 reports
CMPL = 0x0100

# The 0x0B reply of a GPIB-ENET/1000 (firmware 1.2.1f0), byte for byte after the
# status: model name, NUL padding, 04 08 07 at offset 50, 90 bytes in all.
IDENTIFY_EXTRA = (b"GPIB-ENET/1000".ljust(50, b"\0") + bytes([4, 8, 7])).ljust(90, b"\0")
IDENTIFY_CNT = 0x0809D523              # the ENET/1000 has these in the ibcnt field
ID_CNT = 0x0000D523                    # ... and these in the 0x63 / 0x64 replies

# Commands followed by data: (offset, struct format) of the data length.
DATA_LEN = {0x61: (4, "!I"), 0x62: (4, "!I")}
# FindLstn: the header is answered, then the u16 address list (length at byte 2) is sent
# and answered. Both boxes do it this way.
FINDLSTN = 0x2B


def _frag(payload: bytes, flags: int = 0) -> bytes:
    return struct.pack("!HH", flags, len(payload)) + payload


def _status(sta: int = CMPL, err: int = 0, cnt: int = 0, extra: bytes = b"") -> bytes:
    return _frag(struct.pack("!HH4xI", sta, err, cnt) + extra)


OK = _status()
OK_SETUP = _status(err=0xFFFF)          # what the box answers to 0x50 / 0x55
EDVR = _status(sta=ERR | CMPL, err=0)


class _Hangup(Exception):
    """Close the NI connection (the upstream session is gone or unusable)."""


def _parse_read(reply: bytes) -> tuple[bytes, int]:
    """(data, final ibsta) from an ENET/1000-format ibrd reply."""
    data, off, sta = bytearray(), 0, ERR
    while off + 4 <= len(reply):
        flags, n = struct.unpack_from("!HH", reply, off)
        payload = reply[off + 4:off + 4 + n]
        off += 4 + n
        if flags == 0 and off < len(reply):
            data += payload
        elif flags == 0 and len(payload) >= 2:
            sta = struct.unpack_from("!H", payload)[0]
    return bytes(data), sta


def _recvn(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        try:
            d = sock.recv(n - len(buf))
        except OSError:
            return None
        if not d:
            return None
        buf += d
    return bytes(buf)


@dataclass
class Session:
    """One NI-488.2 :5000 connection: a board session or a device handle."""
    sid: int
    peer: tuple[str, int]
    kind: str = ""                     # "board" | "device"
    device: Device | None = None
    closed: bool = False
    channels: list[socket.socket] = field(default_factory=list)
    idn_pending: bool = False          # last write was *IDN?
    idn_reply: bytes | None = None     # answer prepared from ID?


class NiEnetServer:
    """ENET/1000 personality for one Box."""

    def __init__(self, box: Box, listen: str = "0.0.0.0", *, ports: dict[str, int] | None = None,
                 comment: str = ""):
        self.box = box
        self.listen = listen
        p = {"cmd": CMD_PORT, "aux": AUX_PORT, "keep": KEEP_PORT, "wait": WAIT_PORT,
             "http": HTTP_PORT}
        p.update(ports or {})
        self.want_ports = p
        self.ports: dict[str, int] = {}
        self.comment = comment
        self._servers: list[socketserver.BaseServer] = []
        self._sessions: dict[tuple[str, int], Session] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._id_style: dict[tuple[int, int], str] = {}   # (pad, sad) -> "scpi" | "hp"

    # -- identity ----------------------------------------------------------------------------

    @property
    def mac(self) -> str:
        ident, info = self.box.enet.identity, self.box.info
        return (ident.mac if ident else info.mac if info else "00:80:2F:00:00:00").upper()

    @property
    def serial(self) -> int:
        info = self.box.info
        if info and info.serial:
            return info.serial
        # Not seen by discovery yet: derive one from the MAC, as NI's serials are 24-32 bits.
        return int(self.mac.replace(":", "")[-6:], 16)

    def device_info_xml(self, ip: str) -> str:
        info = self.box.info
        # Discovery has the plain model name; the 0x0B reply says "GPIB-ENET  REV C.9".
        model = info.model if info and info.model else "GPIB-ENET/100"
        status = "Running" if self.box.enet.alive or info else "Not responding"
        comment = self.comment or f"{model} at {self.box.cfg.host}, via enet100-bridge"
        rows = [
            ("DevClass", "GPIB-ENET"), ("IPAddr", ip), ("DHCPEnabled", "true"),
            ("SubnetMask", info.netmask if info else "255.255.255.0"),
            ("Gateway", "0.0.0.0"), ("DNSServer", "0.0.0.0"), ("NIPort", str(self.ports.get("http", 80))),
            ("MAC", self.mac), ("Hostname", socket.gethostname()), ("ProdId", PROD_ID),
            ("ProdName", model), ("SerialNo", f"{self.serial:X}"),
            ("NormalVersion", FIRMWARE), ("SafeVersion", "1.0.1f0"), ("BootVersion", "1.0.1f0"),
            ("Status", status), ("Link1Status", "100"), ("Link1Duplex", "FULL"),
            ("ImgPath", "/img/gpib-enet.png"), ("Comment", comment),
            ("UptimeInSeconds", "0"), ("ButtonPressed", "false"),
        ]
        body = "".join(f"    <{k}>\n       {escape(v)}\n    </{k}>\n" for k, v in rows)
        return (f'<?xml version="1.0" ?>\n<Settings>\n'
                f"    <!-- Settings for the {escape(model)} Device -->\n{body}</Settings>\n")

    def mdns_txt(self) -> dict[str, str]:
        info = self.box.info
        model = info.model if info else "GPIB-ENET/100"
        return {"MAC": self.mac, "DevClass": "GPIB-ENET", "ProdId": PROD_ID, "ProdName": model,
                "SerialNo": f"{self.serial:X}", "Status": "Running",
                "ImgPath": "/img/gpib-enet.png", "Comment": ""}

    # -- servers -----------------------------------------------------------------------------

    def start(self) -> None:
        handlers = {"cmd": self._cmd_conn, "aux": self._aux_conn, "keep": self._aux_conn,
                    "wait": self._wait_conn}
        try:
            for key, fn in handlers.items():
                srv = self._tcp(self.want_ports[key], fn)
                self._servers.append(srv)
                self.ports[key] = srv.server_address[1]
        except OSError:
            self.stop()
            raise
        try:
            srv = self._http(self.want_ports["http"])
            self._servers.append(srv)
            self.ports["http"] = srv.server_address[1]
        except OSError as e:
            log.warning("%s: can't serve HTTP on port %d (%s); NI MAX won't be able to add the "
                        "box, but a box already added keeps working", self.box.name,
                        self.want_ports["http"], e)
        log.info("%s: presenting as a GPIB-ENET/1000 for NI-488.2 on %s (ports %s)",
                 self.box.name, self.listen,
                 ", ".join(f"{k} {v}" for k, v in self.ports.items()))

    def stop(self) -> None:
        for s in self._servers:
            try:
                s.shutdown()
                s.server_close()
            except Exception:
                pass
        self._servers.clear()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            self._end(s)

    def _tcp(self, port: int, fn) -> socketserver.ThreadingTCPServer:
        class H(socketserver.BaseRequestHandler):
            def handle(self_h):
                self_h.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                try:
                    fn(self_h.request, self_h.client_address)
                except Exception:
                    log.exception("%s: NI connection on port %d failed", self.box.name, port)

        class S(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        srv = S((self.listen, port), H)
        threading.Thread(target=srv.serve_forever, name=f"nienet-{port}", daemon=True).start()
        return srv

    def _http(self, port: int) -> http.server.ThreadingHTTPServer:
        owner = self

        class H(http.server.BaseHTTPRequestHandler):
            server_version = "enet100-bridge"

            def do_GET(self):
                path = self.path.split("?")[0]
                ip = self.connection.getsockname()[0]
                if path == "/deviceinfo.xml":
                    body, ctype = owner.device_info_xml(ip), "text/html"
                elif path in ("/", "/index.html", "/index.esp"):
                    x = escape(owner.box.cfg.host)
                    body = (f"<!DOCTYPE html><html><head><title>GPIB-ENET/100 via enet100-bridge"
                            f"</title></head><body><h1>GPIB-ENET/100</h1><p>{x}, presented to "
                            f"NI-488.2 by enet100-bridge on {escape(socket.gethostname())}.</p>"
                            f"<p>Serial {owner.serial:X}, MAC {owner.mac}.</p></body></html>")
                    ctype = "text/html"
                else:
                    self.send_error(404)
                    return
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-type", ctype)
                self.send_header("Cache-control", 'no-cache="set-cookie"')
                self.send_header("Content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):
                log.debug("%s: HTTP %s", owner.box.name, fmt % args)

        srv = http.server.ThreadingHTTPServer((self.listen, port), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, name="nienet-http", daemon=True).start()
        return srv

    # -- the :5000 connection ------------------------------------------------------------------

    def _cmd_conn(self, sock: socket.socket, peer) -> None:
        s = Session(next(self._ids), (peer[0], peer[1]))
        with self._lock:
            self._sessions[s.peer] = s
        log.debug("%s: NI session %d from %s:%d", self.box.name, s.sid, *peer)
        try:
            while True:
                block = _recvn(sock, 12)
                if block is None:
                    break
                cmd, args = block[0], block[1:]
                data = b""
                if cmd == FINDLSTN:
                    if not self._findlstn(s, sock, block):
                        break
                    continue
                if cmd in DATA_LEN:
                    off, fmt = DATA_LEN[cmd]
                    (n,) = struct.unpack_from(fmt, block, off)
                    data = _recvn(sock, n) if n else b""
                    if data is None:
                        break
                try:
                    reply = self._command(s, cmd, args, block, data)
                except _Hangup as e:
                    log.info("%s: NI session %d: %s", self.box.name, s.sid, e)
                    break
                sock.sendall(reply)
                if s.closed:
                    break
        except OSError:
            pass
        finally:
            with self._lock:
                self._sessions.pop(s.peer, None)
            self._end(s)
            try:
                sock.close()
            except OSError:
                pass

    def _end(self, s: Session) -> None:
        s.closed = True
        d, s.device = s.device, None
        if d is not None:
            try:
                d.close()
            except Exception as e:
                log.debug("%s: closing device %d: %s", self.box.name, d.pad, e)
        for c in s.channels:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _command(self, s: Session, cmd: int, args: bytes, block: bytes, data: bytes) -> bytes:
        # Session setup and ENET/1000-only commands, answered here.
        if cmd == 0x0B:
            s.kind = "board"
            self._await_box()
            return _status(cnt=IDENTIFY_CNT, extra=IDENTIFY_EXTRA)
        if cmd == 0x63:
            return _status(cnt=ID_CNT, extra=socket.inet_aton(s.peer[0]) + struct.pack("!H", s.peer[1]))
        if cmd == 0x64:
            return _status(cnt=ID_CNT, extra=struct.pack("!I", s.sid))
        if cmd == 0x65:
            return OK
        if cmd == 0x50:
            return OK_SETUP
        if cmd == 0x59:
            return _status(extra=struct.pack("<I", self.serial))
        if cmd == 0x07:
            if args[0] == 0x02:
                return self._open_device(s, args)
            s.kind = s.kind or "board"
            return OK
        if cmd == 0x58:                                    # device online / offline
            if args[0] == 0x00 and s.device is not None:
                self._end(s)
            return OK
        if cmd == 0x12 and s.kind == "board":              # ibonl on the board
            if args[0] == 0x00:
                s.closed = True                            # NI is done; keep our session
            return OK
        if s.kind == "device" and s.device is not None:
            if cmd == 0x62 and data.strip() == b"*IDN?":
                return self._idn_write(s, block, data)
            if cmd == 0x16 and s.idn_pending:
                return self._idn_read(s, block)
            s.idn_pending = False
            s.idn_reply = None
        return self._forward(s, cmd, block, data)

    # -- *IDN? for instruments older than it -----------------------------------------------------
    #
    # Connection Expert and NI MAX identify instruments with *IDN?, read without a
    # termination character, and give up after ~5 s. Pre-488.2 HP instruments (the
    # 3457A, for instance) only know ID?, and end replies with CR LF and no EOI.
    # When *IDN? gets nothing within IDN_PROBE code, the bridge asks ID? (with reads
    # ending on LF), and hands the answer back as an *IDN?-style string ending with
    # EOI. Addresses known to need this skip straight to ID?. Instruments that answer
    # *IDN? are never sent anything else.

    def _idn_write(self, s: Session, block: bytes, data: bytes) -> bytes:
        key = (s.device.pad, s.device.sad)
        s.idn_pending, s.idn_reply = True, None
        if self._id_style.get(key) == "hp":
            s.idn_reply = self._ask_hp_id(s)
            if s.idn_reply is not None:
                return _status(cnt=len(data))
            self._id_style.pop(key, None)           # stopped answering ID?; try *IDN? again
        return self._forward(s, 0x62, block, data)

    def _idn_read(self, s: Session, block: bytes) -> bytes:
        s.idn_pending = False
        maxlen = struct.unpack_from("!I", block, 4)[0]
        if s.idn_reply is not None:
            reply, s.idn_reply = s.idn_reply, None
            return self._data_reply(reply[:maxlen])
        key = (s.device.pad, s.device.sad)
        if self._id_style.get(key) == "scpi":
            return self._forward(s, 0x16, block, b"")
        with self.box.lock:
            prev = self._dev_config(s, 0x03, IDN_PROBE_TMO)
            try:
                reply = self._forward(s, 0x16, block, b"")
            finally:
                self._dev_config(s, 0x03, prev)
            data, sta = _parse_read(reply)
            if data or not sta & TIMO:
                self._id_style[key] = "scpi"
                return reply
            hp = self._ask_hp_id(s)
        if hp is None:
            return reply
        self._id_style[key] = "hp"
        log.info("%s: GPIB %d doesn't know *IDN?; answered it from ID? (%r)", self.box.name,
                 s.device.pad, hp.strip())
        return self._data_reply(hp[:maxlen])

    def _ask_hp_id(self, s: Session) -> bytes | None:
        """ID? with reads ending on LF; returns an *IDN?-style reply, or None."""
        with self.box.lock:
            old_eos = self._dev_config(s, 0x0F, 0x0A)
            old_rd = self._dev_config(s, 0x0C, 1)
            old_tmo = self._dev_config(s, 0x03, IDN_PROBE_TMO)
            try:
                st = self._raw_call(s, struct.pack("!B3xI4x", 0x62, 4) + b"ID?\n")
                if st & ERR:
                    return None
                data, sta = _parse_read(self._forward(s, 0x16, struct.pack("!B3xI4x", 0x16, 256), b""))
            finally:
                self._dev_config(s, 0x03, old_tmo)
                self._dev_config(s, 0x0C, old_rd)
                self._dev_config(s, 0x0F, old_eos)
        text = data.decode("latin-1").strip()
        if not text or not text.isprintable():
            return None
        if "," in text:
            return (text + "\n").encode("latin-1")
        maker, model = ("HEWLETT-PACKARD", text[2:]) if text.upper().startswith("HP") else ("", text)
        return f"{maker},{model},0,0\n".encode("latin-1")

    def _raw_call(self, s: Session, block_and_data: bytes) -> int:
        c = self._upstream(s)
        try:
            c.send(block_and_data)
            flags, payload = c.frag()
        except EnetError as e:
            self.box.enet.mark_dead()
            self.box.note_session_lost(e)
            raise _Hangup(f"upstream call failed: {e}")
        return struct.unpack_from("!H", payload)[0] if len(payload) >= 2 else ERR

    def _dev_config(self, s: Session, opt: int, val: int) -> int:
        """Device ibconfig; returns the previous value (the box puts it in iberr)."""
        c = self._upstream(s)
        try:
            c.send(bytes([0x06, opt, val & 0xFF]).ljust(12, b"\0"))
            flags, payload = c.frag()
        except EnetError as e:
            self.box.enet.mark_dead()
            self.box.note_session_lost(e)
            raise _Hangup(f"ibconfig failed upstream: {e}")
        if opt == 0x03:
            s.device.tmo = val
            s.device._set_socket_timeout()
        return struct.unpack_from("!H", payload, 2)[0] if len(payload) >= 4 else 0

    @staticmethod
    def _data_reply(data: bytes) -> bytes:
        """An ENET/1000 ibrd reply carrying `data`, ended with EOI."""
        return (_frag(data) if data else b"") + _frag(b"", 1) + _status(sta=0x2000 | CMPL, cnt=len(data))

    def _await_box(self) -> None:
        """Wait (up to AWAIT_BOX_S) for the ENET/100 session, rather than refusing NI.

        NI-488.2 gives up on an open after ~10 s, and Keysight Connection Expert then
        marks the interface "not present" until its next scan. So if the bridge is still
        connecting (just started, box rebooting), keep NI waiting a little instead."""
        deadline = time.monotonic() + AWAIT_BOX_S
        while True:
            try:
                self.box.ensure_open()
                return
            except BoxUnavailable as e:
                if time.monotonic() >= deadline:
                    raise _Hangup(f"box unavailable: {e}")
                # ensure_open rate-limits retries; the monitor thread keeps trying too.
                time.sleep(0.5)

    def _open_device(self, s: Session, args: bytes) -> bytes:
        # 07 02 00 <mode> <pad> <sad> <eos> 00 <tmo> 00 04
        mode, pad, sad, eos, tmo = args[2], args[3], args[4], args[5], args[7]
        s.kind = "device"
        try:
            self._await_box()
        except _Hangup as e:
            log.warning("%s: NI ibdev %d: %s", self.box.name, pad, e)
            return EDVR
        try:
            with self.box.lock:
                self.box.ensure_open()
                d = Device(self.box.enet, pad, sad, tmo or 13, eos, mode)
                d._open()
                self.box.enet.devices.append(d)
        except BoxUnavailable as e:
            log.warning("%s: NI ibdev %d: %s", self.box.name, pad, e)
            return EDVR
        except EnetError as e:
            self.box.note_session_lost(e)
            raise _Hangup(f"device open failed: {e}")
        s.device = d
        return OK

    def _upstream(self, s: Session) -> _Conn:
        if s.kind == "device":
            c = s.device._conn if s.device else None
        else:
            c = self.box.enet._board if self.box.enet.alive else None
        if c is None:
            raise _Hangup("the ENET/100 session is gone")
        return c

    def _forward(self, s: Session, cmd: int, block: bytes, data: bytes) -> bytes:
        with self.box.lock:
            c = self._upstream(s)
            try:
                if cmd == 0x06 and s.device is not None and block[1] == 0x03:
                    s.device.tmo = block[2]
                    s.device._set_socket_timeout()
                c.send(block + data)
                if cmd == 0x16:
                    reply = self._read_reply(c)
                else:
                    flags, payload = c.frag()
                    reply = _frag(payload, flags)
            except EnetError as e:
                self.box.note_session_lost(e)
                self.box.enet.mark_dead()
                raise _Hangup(f"command 0x{cmd:02x} failed upstream: {e}")
            self.box.touch()
            return reply

    def _findlstn(self, s: Session, sock: socket.socket, block: bytes) -> bool:
        """Relay both steps of FindLstn, holding the bus so nothing gets in between."""
        (n,) = struct.unpack_from("!H", block, 2)
        with self.box.lock:
            try:
                sock.sendall(self._forward(s, FINDLSTN, block, b""))
                data = _recvn(sock, n) if n else b""
                if data is None:
                    # NI went away mid-command. The box waits for the list; finish it.
                    data = struct.pack("!H", 0xFFFF) * (n // 2)
                    self._forward_raw(s, data)
                    return False
                sock.sendall(self._forward_raw(s, data))
                return True
            except _Hangup as e:
                log.info("%s: NI session %d: %s", self.box.name, s.sid, e)
                return False
            except OSError:
                return False

    def _forward_raw(self, s: Session, data: bytes) -> bytes:
        """Send bytes that complete a command already sent; return the reply."""
        with self.box.lock:
            c = self._upstream(s)
            try:
                c.send(data)
                flags, payload = c.frag()
            except EnetError as e:
                self.box.enet.mark_dead()
                self.box.note_session_lost(e)
                raise _Hangup(f"FindLstn failed upstream: {e}")
            return _frag(payload, flags)

    @staticmethod
    def _read_reply(c: _Conn) -> bytes:
        """ibrd: the ENET/100 sends a status before the data, the ENET/1000 doesn't."""
        flags, first = c.frag()
        sta = struct.unpack_from("!H", first)[0] if len(first) >= 2 else ERR
        if sta & ERR:
            # Failed before any data. The ENET/100 may follow with terminator + status.
            final = first
            try:
                f2, p2 = c.frag(timeout=1.5)
                if f2 == 1:
                    _, final = c.frag()
            except EnetTimeout:
                pass
            return _frag(b"", 1) + _frag(final)
        out = bytearray()
        while True:
            flags, payload = c.frag()
            out += _frag(payload, flags)
            if flags != 0:
                break
        flags, final = c.frag()
        out += _frag(final, flags)
        return bytes(out)

    # -- :5015 (ibwait) ----------------------------------------------------------------------------

    def _owner(self, block: bytes) -> Session | None:
        # 55 <kind> 00 00 00 00 <port:u16> <ip:4>, or 55 <kind> 00 02 00 00 <port> <ip>
        port = struct.unpack_from("!H", block, 6)[0]
        ip = socket.inet_ntoa(block[8:12])
        with self._lock:
            s = self._sessions.get((ip, port))
            if s is None:                                   # NAT or another address of ours
                s = next((x for x in self._sessions.values() if x.peer[1] == port), None)
        return s

    def _wait_conn(self, sock: socket.socket, peer) -> None:
        owner: Session | None = None
        try:
            while True:
                block = _recvn(sock, 12)
                if block is None:
                    return
                cmd = block[0]
                if cmd == 0x55:
                    owner = self._owner(block)
                    if owner:
                        owner.channels.append(sock)
                    reply = OK_SETUP
                elif cmd in (0x63, 0x64, 0x65, 0x50):
                    reply = OK if cmd == 0x65 else OK_SETUP
                elif cmd == 0x22 and owner is not None:
                    reply = self._wait(owner, block)
                else:
                    log.debug("%s: :5015 command 0x%02x ignored", self.box.name, cmd)
                    reply = OK
                sock.sendall(reply)
        except _Hangup as e:
            log.info("%s: NI wait channel: %s", self.box.name, e)
        except OSError:
            pass
        finally:
            sock.close()

    def _wait(self, s: Session, block: bytes) -> bytes:
        mask = struct.unpack_from("!H", block, 2)[0]
        if s.kind == "device":
            d = s.device
            if d is None:
                raise _Hangup("device closed")
            lock, w = d._wait_lock, d._wait
            tmo = TMO_SECONDS[d.tmo] or 1000.0
        else:
            lock, w = threading.Lock(), self.box.enet._board_wait
            tmo = float(self.box.cfg.board_timeout_s)
        if w is None:
            raise _Hangup("the ENET/100 session is gone")
        with lock:
            w.timeout = (tmo if mask & TIMO else 24 * 3600.0) + SOCKET_MARGIN_S
            try:
                w.send(block)
                flags, payload = w.frag()
            except EnetError as e:
                self.box.note_session_lost(e)
                self.box.enet.mark_dead()
                raise _Hangup(f"ibwait failed upstream: {e}")
        return _frag(payload, flags)

    # -- :5003 and :5005 ---------------------------------------------------------------------------

    def _aux_conn(self, sock: socket.socket, peer) -> None:
        """ENET/1000 side channels. NI sets them up and polls 0x4F on :5005; the ENET/100
        has nothing behind them, so every command gets a plain success."""
        try:
            while True:
                block = _recvn(sock, 12)
                if block is None:
                    return
                cmd = block[0]
                if cmd == 0x63:
                    reply = _status(extra=socket.inet_aton(peer[0]) + struct.pack("!H", peer[1]))
                elif cmd in (0x50, 0x55):
                    reply = OK_SETUP
                else:
                    reply = OK
                sock.sendall(reply)
        except OSError:
            pass
        finally:
            sock.close()
