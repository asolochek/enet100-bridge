"""Just enough ONC RPC (RFC 5531) and XDR (RFC 4506) for a VXI-11 server.

Includes a portmapper (program 100000 v2) that can either serve port 111
itself or register our programs with an rpcbind that already owns it.
"""

from __future__ import annotations

import logging
import random
import socket
import socketserver
import struct
import threading
from typing import Callable

log = logging.getLogger(__name__)

CALL, REPLY = 0, 1
MSG_ACCEPTED, MSG_DENIED = 0, 1
SUCCESS, PROG_UNAVAIL, PROG_MISMATCH, PROC_UNAVAIL, GARBAGE_ARGS, SYSTEM_ERR = range(6)

PMAP_PROG, PMAP_VERS, PMAP_PORT = 100000, 2, 111
IPPROTO_TCP, IPPROTO_UDP = 6, 17


class XdrError(Exception):
    pass


class Unpacker:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise XdrError("short XDR data")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def uint(self) -> int:
        return struct.unpack("!I", self._take(4))[0]

    def int(self) -> int:
        return struct.unpack("!i", self._take(4))[0]

    def bool(self) -> bool:
        return self.uint() != 0

    def opaque(self) -> bytes:
        n = self.uint()
        b = self._take(n)
        self._take((4 - n % 4) % 4)
        return b

    def string(self) -> str:
        return self.opaque().decode("latin-1")


class Packer:
    def __init__(self):
        self.parts: list[bytes] = []

    def uint(self, v: int) -> "Packer":
        self.parts.append(struct.pack("!I", v & 0xFFFFFFFF))
        return self

    def int(self, v: int) -> "Packer":
        self.parts.append(struct.pack("!i", v))
        return self

    def bool(self, v: bool) -> "Packer":
        return self.uint(1 if v else 0)

    def opaque(self, b: bytes) -> "Packer":
        self.uint(len(b))
        self.parts.append(bytes(b) + bytes((4 - len(b) % 4) % 4))
        return self

    def bytes(self) -> bytes:
        return b"".join(self.parts)


class RpcCall:
    def __init__(self, xid: int, prog: int, vers: int, proc: int, args: Unpacker, conn):
        self.xid, self.prog, self.vers, self.proc, self.args = xid, prog, vers, proc, args
        self.conn = conn       # per-connection context object (TCP) or None (UDP)


class ProcUnavailable(Exception):
    pass


class NoReply(Exception):
    """Raised by a handler that must not answer (e.g. a broadcast CALLIT it can't serve)."""


class UdpPeer:
    """call.conn for UDP calls: just the sender's address."""

    def __init__(self, peer):
        self.peer = peer


# handler(call) -> reply body bytes
Handler = Callable[[RpcCall], bytes]


def parse_call(msg: bytes, conn) -> tuple[int, RpcCall | None, bytes | None]:
    """Returns (xid, call, error_reply). Exactly one of call/error_reply is set."""
    u = Unpacker(msg)
    xid = u.uint()
    if u.uint() != CALL:
        return xid, None, None
    rpcvers = u.uint()
    prog, vers, proc = u.uint(), u.uint(), u.uint()
    u.uint(); u.opaque()        # credentials
    u.uint(); u.opaque()        # verifier
    if rpcvers != 2:
        rej = Packer().uint(xid).uint(REPLY).uint(MSG_DENIED).uint(0).uint(2).uint(2).bytes()
        return xid, None, rej
    return xid, RpcCall(xid, prog, vers, proc, Unpacker(msg[u.pos:]), conn), None


def accepted(xid: int, stat: int, body: bytes = b"") -> bytes:
    return Packer().uint(xid).uint(REPLY).uint(MSG_ACCEPTED).uint(0).uint(0).uint(stat).bytes() + body


class Dispatcher:
    """Maps (prog, vers) to a handler."""

    def __init__(self):
        self.programs: dict[int, dict[int, Handler]] = {}

    def add(self, prog: int, vers: int, handler: Handler) -> None:
        self.programs.setdefault(prog, {})[vers] = handler

    def dispatch(self, msg: bytes, conn) -> bytes | None:
        try:
            xid, call, err = parse_call(msg, conn)
        except XdrError:
            return None
        if err is not None or call is None:
            return err
        versions = self.programs.get(call.prog)
        if not versions:
            return accepted(xid, PROG_UNAVAIL)
        handler = versions.get(call.vers)
        if handler is None:
            lo, hi = min(versions), max(versions)
            return accepted(xid, PROG_MISMATCH, Packer().uint(lo).uint(hi).bytes())
        try:
            body = handler(call)
        except NoReply:
            return None
        except ProcUnavailable:
            return accepted(xid, PROC_UNAVAIL)
        except XdrError:
            return accepted(xid, GARBAGE_ARGS)
        except Exception:
            log.exception("RPC handler failed (prog %d proc %d)", call.prog, call.proc)
            return accepted(xid, SYSTEM_ERR)
        return accepted(xid, SUCCESS, body)


# -- record marking over TCP -------------------------------------------------------

def read_record(sock: socket.socket) -> bytes | None:
    out = bytearray()
    while True:
        hdr = _recv_exact(sock, 4)
        if hdr is None:
            return None
        (word,) = struct.unpack("!I", hdr)
        n = word & 0x7FFFFFFF
        if n > 64 * 1024 * 1024:
            raise XdrError("RPC record too large")
        frag = _recv_exact(sock, n)
        if frag is None:
            return None
        out += frag
        if word & 0x80000000:
            return bytes(out)


def write_record(sock: socket.socket, data: bytes) -> None:
    sock.sendall(struct.pack("!I", 0x80000000 | len(data)) + data)


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


class ConnContext:
    """Per-TCP-connection state handed to handlers as call.conn."""

    def __init__(self, sock: socket.socket, peer):
        self.sock = sock
        self.peer = peer
        self.closed = threading.Event()
        self.on_close: list[Callable[[], None]] = []


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = False


def serve_tcp(dispatcher: Dispatcher, host: str, port: int) -> socketserver.TCPServer:
    class H(socketserver.BaseRequestHandler):
        def handle(self):
            sock = self.request
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            ctx = ConnContext(sock, self.client_address)
            try:
                while True:
                    msg = read_record(sock)
                    if msg is None:
                        break
                    reply = dispatcher.dispatch(msg, ctx)
                    if reply is not None:
                        write_record(sock, reply)
            except (OSError, XdrError) as e:
                log.debug("RPC connection %s ended: %s", self.client_address, e)
            finally:
                ctx.closed.set()
                for fn in ctx.on_close:
                    try:
                        fn()
                    except Exception:
                        log.exception("connection cleanup failed")

    srv = _ThreadingTCPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, name=f"rpc-tcp-{port}", daemon=True).start()
    return srv


def serve_udp(dispatcher: Dispatcher, host: str, port: int) -> socketserver.UDPServer:
    class H(socketserver.BaseRequestHandler):
        def handle(self):
            data, sock = self.request
            reply = dispatcher.dispatch(data, UdpPeer(self.client_address))
            if reply is not None:
                sock.sendto(reply, self.client_address)

    srv = socketserver.ThreadingUDPServer((host, port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name=f"rpc-udp-{port}", daemon=True).start()
    return srv


# -- portmapper ----------------------------------------------------------------------

class Portmapper:
    """Program 100000 v2: GETPORT and DUMP for our own registrations."""

    def __init__(self):
        self.mappings: dict[tuple[int, int, int], int] = {}

    def set(self, prog: int, vers: int, prot: int, port: int) -> None:
        self.mappings[(prog, vers, prot)] = port

    def handler(self, call: RpcCall) -> bytes:
        a = call.args
        peer = call.conn.peer[0] if call.conn is not None else "?"
        kind = "TCP" if isinstance(call.conn, ConnContext) else "UDP"
        if call.proc == 0:                                   # NULL
            log.info("portmapper: NULL from %s (%s)", peer, kind)
            return b""
        if call.proc == 3:                                   # GETPORT
            prog, vers, prot, _ = a.uint(), a.uint(), a.uint(), a.uint()
            port = self.mappings.get((prog, vers, prot), 0)
            log.info("portmapper: GETPORT prog 0x%06x v%d %s from %s (%s) -> %d", prog, vers,
                     {6: "tcp", 17: "udp"}.get(prot, prot), peer, kind, port)
            return Packer().uint(port).bytes()
        if call.proc == 5:                                   # CALLIT (broadcast search)
            prog, vers, proc = a.uint(), a.uint(), a.uint()
            a.opaque()
            port = self.mappings.get((prog, vers, IPPROTO_TCP), 0)
            log.info("portmapper: CALLIT prog 0x%06x v%d proc %d from %s (%s)%s", prog, vers,
                     proc, peer, kind, f" -> {port}" if port and proc == 0 else " (no reply)")
            if not port or proc != 0:
                raise NoReply()                              # stay silent, per RFC 1833
            # Our services are TCP-only; NULL has no results.
            return Packer().uint(port).opaque(b"").bytes()
        if call.proc == 4:                                   # DUMP
            log.info("portmapper: DUMP from %s (%s)", peer, kind)
            p = Packer()
            for (prog, vers, prot), port in sorted(self.mappings.items()):
                p.bool(True).uint(prog).uint(vers).uint(prot).uint(port)
            return p.bool(False).bytes()
        if call.proc in (1, 2):                              # SET / UNSET: refuse
            return Packer().bool(False).bytes()
        raise ProcUnavailable()


def rpc_call_udp(host: str, port: int, prog: int, vers: int, proc: int, args: bytes,
                 timeout: float = 2.0) -> Unpacker:
    """Make one RPC call over UDP (used to register with an existing rpcbind)."""
    xid = random.getrandbits(32)
    msg = (Packer().uint(xid).uint(CALL).uint(2).uint(prog).uint(vers).uint(proc)
           .uint(0).opaque(b"").uint(0).opaque(b"").bytes() + args)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(msg, (host, port))
        while True:
            data, _ = s.recvfrom(65536)
            u = Unpacker(data)
            if u.uint() != xid:
                continue
            if u.uint() != REPLY or u.uint() != MSG_ACCEPTED:
                raise XdrError("RPC call rejected")
            u.uint(); u.opaque()
            if u.uint() != SUCCESS:
                raise XdrError("RPC call not accepted")
            return u
    finally:
        s.close()


def register_with_rpcbind(mappings: list[tuple[int, int, int, int]]) -> bool:
    """PMAPPROC_SET each (prog, vers, prot, port) with the local rpcbind."""
    ok = True
    for prog, vers, prot, port in mappings:
        # Drop any stale registration from a previous run first.
        rpc_call_udp("127.0.0.1", PMAP_PORT, PMAP_PROG, PMAP_VERS, 2,
                     Packer().uint(prog).uint(vers).uint(prot).uint(0).bytes())
        u = rpc_call_udp("127.0.0.1", PMAP_PORT, PMAP_PROG, PMAP_VERS, 1,
                         Packer().uint(prog).uint(vers).uint(prot).uint(port).bytes())
        ok = u.bool() and ok
    return ok


def unregister_with_rpcbind(mappings: list[tuple[int, int, int, int]]) -> None:
    for prog, vers, prot, _ in mappings:
        try:
            rpc_call_udp("127.0.0.1", PMAP_PORT, PMAP_PROG, PMAP_VERS, 2,
                         Packer().uint(prog).uint(vers).uint(prot).uint(0).bytes())
        except (OSError, XdrError):
            pass
