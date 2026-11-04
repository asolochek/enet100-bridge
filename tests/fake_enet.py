"""A simulated GPIB-ENET/100 for tests, built from the captures in docs/PROTOCOL.md.

It enforces the real box's worst habit: closing a board session that never got
as far as binding its :5015 channel "wedges" it, after which it accepts TCP
connections but never answers.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass, field

TMO_SECONDS = [None, 10e-6, 30e-6, 100e-6, 300e-6, 1e-3, 3e-3, 10e-3, 30e-3, 100e-3,
               300e-3, 1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0]


@dataclass
class FakeInstrument:
    """Answers `ID?` like an HP 3457A with END OFF: replies end in CR LF, no EOI."""
    ident: bytes = b"HP3457A\r\n"
    eoi: bool = False
    output: bytearray = field(default_factory=bytearray)
    written: list[bytes] = field(default_factory=list)
    stb: int = 0
    srq: bool = False
    cleared: int = 0
    triggered: int = 0
    local_calls: int = 0

    def write(self, data: bytes) -> None:
        self.written.append(data)
        for cmd in data.replace(b";", b"\n").split(b"\n"):
            cmd = cmd.strip().upper()
            if cmd in (b"ID?", b"*IDN?"):
                self.output += self.ident
            elif cmd == b"BIG?":
                self.output += b"".join(b" %13.7E\r\n" % (i * 1.5) for i in range(150))
            elif cmd == b"SRQ":
                self.stb = 0x48
                self.srq = True

    def serial_poll(self) -> int:
        s = self.stb
        self.stb = 0
        self.srq = False
        return s


def _status(sta: int, err: int = 0, cnt: int = 0, extra: bytes = b"") -> bytes:
    payload = struct.pack("!HH4xI", sta, err, cnt) + extra
    return struct.pack("!HH", 0, len(payload)) + payload


class FakeEnet:
    def __init__(self):
        self.instruments: dict[int, FakeInstrument] = {4: FakeInstrument()}
        self.wedged = False
        self.violations: list[str] = []
        self.ren = False
        self.cic = False                         # becomes CIC on IFC, like the real box
        # Board options a new board session starts with, as a real box reported
        # them (NI-488.2 IbcXXX codes). Like the box, they reset per session.
        self.default_board_opts = {0x01: 0, 0x02: 0, 0x03: 13, 0x07: 1, 0x08: 0, 0x0A: 1, 0x0B: 0,
                           0x10: 0, 0x11: 2, 0x17: 0, 0x19: 0, 0x1A: 1, 0x1F: 0, 0x20: 0,
                           0x21: 0}
        self.board_opts = dict(self.default_board_opts)
        self.log: list[tuple[str, bytes]] = []
        self._lock = threading.RLock()
        self._bound: set[int] = set()            # client ports with a bound :5015
        self._socks: list[socket.socket] = []
        self.cmd_srv = self._listen(self._cmd_conn)
        self.wait_srv = self._listen(self._wait_conn)
        self.cmd_port = self.cmd_srv.getsockname()[1]
        self.wait_port = self.wait_srv.getsockname()[1]
        self.closed_cleanly: list[str] = []

    # -- plumbing -------------------------------------------------------------------------

    def _listen(self, handler) -> socket.socket:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(16)

        def loop():
            while True:
                try:
                    c, addr = s.accept()
                except OSError:
                    return
                self._socks.append(c)
                threading.Thread(target=self._guard, args=(handler, c, addr), daemon=True).start()

        threading.Thread(target=loop, daemon=True).start()
        return s

    def _guard(self, handler, c, addr):
        try:
            handler(c, addr)
        except (OSError, ConnectionError):
            pass
        finally:
            if not self.wedged:          # the real box FINs back unless it is wedged
                try:
                    c.close()
                except OSError:
                    pass

    @staticmethod
    def _recv(c: socket.socket, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            chunk = c.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def close(self) -> None:
        for s in [self.cmd_srv, self.wait_srv] + self._socks:
            try:
                s.close()
            except OSError:
                pass

    def power_cycle(self) -> None:
        """Drop every connection with RST and forget all state."""
        for c in self._socks:
            try:
                c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                c.close()
            except OSError:
                pass
        self._socks.clear()
        self._bound.clear()
        self.wedged = False
        self.cic = False

    # -- :5000 ----------------------------------------------------------------------------------

    def _cmd_conn(self, c: socket.socket, addr):
        st = {"kind": None, "setup": [], "dev": None, "online": False, "offline": False,
              "port": addr[1]}
        while True:
            blk = self._recv(c, 12)
            if blk is None:
                self._on_close(st)
                return
            if self.wedged:
                continue
            cmd, args = blk[0], blk[1:]
            data = b""
            if cmd in (0x61, 0x62):
                (n,) = struct.unpack_from("!I", blk, 4)
                data = self._recv(c, n) or b""
            with self._lock:
                reply = self._handle(st, cmd, args, data, c)
            self.log.append((f"{st['kind']}:{cmd:02x}", blk + data))
            if reply:
                # Send the extra status bytes in a separate segment, like the box.
                c.sendall(reply[:16])
                if len(reply) > 16:
                    time.sleep(0.001)
                    c.sendall(reply[16:])

    def _on_close(self, st):
        if st["kind"] == "board":
            if st["port"] not in self._bound:
                self.violations.append("board session closed before its :5015 bind")
                self.wedged = True
            else:
                self.closed_cleanly.append("board")
        elif st["kind"] == "dev":
            if not st["offline"]:
                self.violations.append(f"device {st['dev']['pad']} closed without 58 00 01")
            else:
                self.closed_cleanly.append(f"dev{st['dev']['pad']}")

    def _handle(self, st, cmd, args, data, c) -> bytes | None:
        if cmd == 0x0B:
            st["kind"] = "board"
            self.board_opts = dict(self.default_board_opts)
            self.ren = False
            extra = (b"GPIB-ENET  REV C.9\n" b"National Instruments Corp.        \n"
                     + bytes.fromhex("00802f123456") + socket.inet_aton("127.0.0.1")
                     + socket.inet_aton("127.0.0.1") + struct.pack("!H", st["port"])
                     + b"\x01" + bytes(73))
            return _status(0x0100, 0, 0, extra)
        if cmd == 0x50:
            return _status(0x0100, 0xFFFF)
        if cmd == 0x07:
            if args[0] == 0x02:
                st["kind"] = "dev"
                st["dev"] = {"mode": args[2], "pad": args[3], "sad": args[4], "eos": args[5],
                             "tmo": args[7], "eot": bool(args[2] & 1),
                             "eosrd": bool(args[2] & 4)}
            return _status(0x0100)
        if cmd == 0x58:
            if args[0] == 1:
                st["online"] = True
            else:
                st["offline"] = True
            return _status(0x0100)
        if cmd == 0x12:
            return _status(0x0100, 1)
        if cmd == 0x06:
            return self._ibconfig(st, args[0], args[1])
        if cmd == 0x4E:
            if st["kind"] == "board":
                if args[0] not in self.board_opts:
                    return _status(0x8130, 4)    # EARG
                return _status(0x0130, self.board_opts[args[0]])
            return _status(0x0100)
        if st["kind"] == "board":
            return self._board_cmd(cmd, args, data)
        return self._dev_cmd(st, cmd, args, data, c)

    def _ibconfig(self, st, opt, val) -> bytes:
        d = st["dev"]
        prev = 0
        if d is None:
            if opt not in self.board_opts:
                return _status(0x8130, 4)        # EARG
            prev = self.board_opts[opt]
            self.board_opts[opt] = val
            if opt == 0x0B:
                self.ren = bool(val)
            return _status(0x0130, prev)
        if opt == 0x03:
            prev, d["tmo"] = d["tmo"], val
        elif opt == 0x04:
            prev, d["eot"] = int(d["eot"]), bool(val)
        elif opt == 0x0C:
            prev, d["eosrd"] = int(d["eosrd"]), bool(val)
        elif opt == 0x0F:
            prev, d["eos"] = d["eos"], val
        return _status(0x0100, prev)

    def _board_cmd(self, cmd, args, data) -> bytes:
        if cmd == 0x1C:
            self.cic = True
            return _status(0x0120)
        if cmd == 0x61:
            return _status(0x0138, 0, len(data))
        if cmd == 0x0D:
            return _status(0x0130, 0, 0, struct.pack("!H", 0x42FF | (0x1000 if self.ren else 0)))
        if cmd == 0x0F:
            if not self.cic:
                return _status(0x8100, 1)        # ECIC
            return _status(0x0130, 0, 0, struct.pack("!H", int(args[0] in self.instruments)))
        return _status(0x0130)

    def _dev_cmd(self, st, cmd, args, data, c) -> bytes | None:
        d = st["dev"]
        inst = self.instruments.get(d["pad"])
        if cmd == 0x62:
            if inst is None:
                return _status(0x8100, 2, 0)
            inst.write(data)
            return _status(0x0100, 0, len(data))
        if cmd == 0x16:
            (maxlen,) = struct.unpack_from("!I", args, 3)
            c.sendall(_status(0x0100))
            if inst is None:
                c.sendall(b"\x00\x01\x00\x00")
                return _status(0x8100, 2, 0)
            out = b""
            sta = 0x0100
            deadline = time.monotonic() + (TMO_SECONDS[d["tmo"]] or 1000)
            while True:
                if inst.output:
                    b = bytes(inst.output[:1])
                    del inst.output[:1]
                    out += b
                    if len(out) >= maxlen:
                        break
                    if d["eosrd"] and b[0] == d["eos"]:
                        sta |= 0x2000
                        break
                    if not inst.output and inst.eoi:
                        sta |= 0x2000
                        break
                    continue
                if time.monotonic() >= deadline:
                    sta, err = 0xC100, 6
                    break
                self._lock.release()
                time.sleep(0.005)
                self._lock.acquire()
            if out:
                c.sendall(struct.pack("!HH", 0, len(out)) + out)
            c.sendall(b"\x00\x01\x00\x00")
            return _status(sta, 6 if sta & 0x8000 else 0, len(out))
        if inst is None and cmd in (0x04, 0x20, 0x10, 0x19):
            return _status(0x8100, 2, 0)
        if cmd == 0x04:
            inst.cleared += 1
            inst.output.clear()
            return _status(0x0100)
        if cmd == 0x20:
            inst.triggered += 1
            return _status(0x0100)
        if cmd == 0x10:
            inst.local_calls += 1
            return _status(0x0100)
        if cmd == 0x19:
            return _status(0x0100, 0, 0, bytes([inst.serial_poll()]))
        return _status(0x0100)

    # -- :5015 ----------------------------------------------------------------------------------

    def _wait_conn(self, c: socket.socket, addr):
        bound = None
        while True:
            blk = self._recv(c, 12)
            if blk is None:
                return
            if self.wedged:
                continue
            cmd = blk[0]
            if cmd == 0x55:
                (port,) = struct.unpack_from("!H", blk, 6)
                bound = (blk[3], port)
                self._bound.add(port)
                c.sendall(_status(0x0100, 0xFFFF))
            elif cmd == 0x22:
                (mask,) = struct.unpack_from("!H", blk, 2)
                pad = self._pad_for_port(bound[1]) if bound else None
                c.sendall(self._wait(pad, mask))

    def _pad_for_port(self, port):
        # The test harness only needs "the instrument at 4".
        return 4

    def _wait(self, pad, mask) -> bytes:
        inst = self.instruments.get(pad)
        tmo = 1.0
        deadline = time.monotonic() + tmo
        while True:
            if inst and inst.srq and mask & 0x0800:
                return _status(0x0900)
            if mask & 0x4000 and time.monotonic() >= deadline:
                return _status(0x4100)
            time.sleep(0.01)
