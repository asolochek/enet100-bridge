"""A small VXI-11 client, for `enet100-bridge query` and the tests.

Not a replacement for a VISA library: no async, no SRQ server, one link per
object.
"""

from __future__ import annotations

import random
import socket

from . import rpc
from .rpc import Packer, Unpacker
from .vxi11 import (DEVICE_CORE, FLAG_END, FLAG_TERMCHRSET, REASON_CHR, REASON_END,
                    VXI11_VERSION)


class Vxi11Error(Exception):
    def __init__(self, op: str, code: int):
        self.op, self.code = op, code
        super().__init__(f"{op}: VXI-11 error {code}")


def _getport(host: str, prog: int, vers: int, timeout: float) -> int:
    s = socket.create_connection((host, rpc.PMAP_PORT), timeout=timeout)
    try:
        body = Packer().uint(prog).uint(vers).uint(rpc.IPPROTO_TCP).uint(0).bytes()
        return _call(s, rpc.PMAP_PROG, rpc.PMAP_VERS, 3, body).uint()
    finally:
        s.close()


def _call(sock: socket.socket, prog: int, vers: int, proc: int, body: bytes) -> Unpacker:
    xid = random.getrandbits(32)
    msg = (Packer().uint(xid).uint(rpc.CALL).uint(2).uint(prog).uint(vers).uint(proc)
           .uint(0).opaque(b"").uint(0).opaque(b"").bytes() + body)
    rpc.write_record(sock, msg)
    while True:
        rec = rpc.read_record(sock)
        if rec is None:
            raise ConnectionError("server closed the connection")
        u = Unpacker(rec)
        if u.uint() != xid:
            continue
        if u.uint() != rpc.REPLY or u.uint() != rpc.MSG_ACCEPTED:
            raise rpc.XdrError("call rejected")
        u.uint(); u.opaque()
        stat = u.uint()
        if stat != rpc.SUCCESS:
            raise rpc.XdrError(f"call not accepted (accept_stat {stat})")
        return u


class Vxi11Client:
    def __init__(self, host: str, device: str, *, timeout: float = 10.0,
                 port: int | None = None):
        self.timeout = timeout
        core_port = port or _getport(host, DEVICE_CORE, VXI11_VERSION, timeout)
        if not core_port:
            raise ConnectionError(f"{host}: no VXI-11 server registered with the portmapper")
        self.sock = socket.create_connection((host, core_port), timeout=timeout + 30)
        u = self._core(10, Packer().int(random.getrandbits(31)).bool(False).uint(0)
                       .opaque(device.encode()).bytes())
        err = u.int()
        if err:
            self.sock.close()
            raise Vxi11Error(f"create_link {device}", err)
        self.lid = u.int()
        self.abort_port = u.uint()
        self.max_recv_size = u.uint()

    @property
    def _tmo_ms(self) -> int:
        return int(self.timeout * 1000)

    def _core(self, proc: int, body: bytes) -> Unpacker:
        return _call(self.sock, DEVICE_CORE, VXI11_VERSION, proc, body)

    def write(self, data: bytes) -> int:
        sent = 0
        chunk = max(1, self.max_recv_size)
        while True:
            part = data[sent:sent + chunk]
            last = sent + len(part) >= len(data)
            u = self._core(11, Packer().int(self.lid).uint(self._tmo_ms).uint(0)
                           .int(FLAG_END if last else 0).opaque(part).bytes())
            err, n = u.int(), u.uint()
            if err:
                raise Vxi11Error("device_write", err)
            sent += n
            if last:
                return sent

    def read(self, size: int = 65536, termchar: int | None = 0x0A) -> bytes:
        out = bytearray()
        while True:
            flags = FLAG_TERMCHRSET if termchar is not None else 0
            u = self._core(12, Packer().int(self.lid).uint(size).uint(self._tmo_ms).uint(0)
                           .int(flags).int(termchar or 0).bytes())
            err, reason, data = u.int(), u.int(), u.opaque()
            out += data
            if err:
                e = Vxi11Error("device_read", err)
                e.data = bytes(out)
                raise e
            if reason & (REASON_END | REASON_CHR) or len(out) >= size:
                return bytes(out)

    def query(self, cmd: str, termchar: int | None = 0x0A) -> str:
        self.write(cmd.encode())
        return self.read(termchar=termchar).decode("latin-1")

    def _generic(self, proc: int) -> Unpacker:
        return self._core(proc, Packer().int(self.lid).int(0).uint(0).uint(self._tmo_ms).bytes())

    def readstb(self) -> int:
        u = self._generic(13)
        err, stb = u.int(), u.uint()
        if err:
            raise Vxi11Error("device_readstb", err)
        return stb

    def _simple(self, proc: int, name: str) -> None:
        err = self._generic(proc).int()
        if err:
            raise Vxi11Error(name, err)

    def trigger(self) -> None:
        self._simple(14, "device_trigger")

    def clear(self) -> None:
        self._simple(15, "device_clear")

    def remote(self) -> None:
        self._simple(16, "device_remote")

    def local(self) -> None:
        self._simple(17, "device_local")

    def lock(self, wait_ms: int = 0) -> None:
        err = self._core(18, Packer().int(self.lid).int(1 if wait_ms else 0).uint(wait_ms)
                         .bytes()).int()
        if err:
            raise Vxi11Error("device_lock", err)

    def unlock(self) -> None:
        err = self._core(19, Packer().int(self.lid).bytes()).int()
        if err:
            raise Vxi11Error("device_unlock", err)

    def docmd(self, cmd: int, data: bytes = b"", datasize: int = 1) -> bytes:
        u = self._core(22, Packer().int(self.lid).int(0).uint(self._tmo_ms).uint(0).int(cmd)
                       .bool(True).int(datasize).opaque(data).bytes())
        err, out = u.int(), u.opaque()
        if err:
            raise Vxi11Error("device_docmd", err)
        return out

    def close(self) -> None:
        try:
            self._core(23, Packer().int(self.lid).bytes())
        except (OSError, rpc.XdrError, ConnectionError):
            pass
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
