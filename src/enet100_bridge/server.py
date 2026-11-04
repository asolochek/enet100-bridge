"""Configuration and process wiring: portmapper, VXI-11 core/abort channels,
one Box per configured ENET/100, and a localhost admin port."""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import threading
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from . import rpc
from .box import Box, BoxConfig
from .vxi11 import DEVICE_ASYNC, DEVICE_CORE, VXI11_VERSION, Vxi11Server

log = logging.getLogger(__name__)


@dataclass
class BridgeConfig:
    listen: str = "0.0.0.0"
    core_port: int = 9010
    abort_port: int = 9011
    portmapper: str = "auto"          # auto | builtin | rpcbind | off
    admin_port: int = 9012            # localhost only; 0 disables
    max_recv_size: int = 4096
    log_level: str = "INFO"
    log_file: str = ""
    boxes: list[BoxConfig] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)


def _apply(obj, data: dict, where: str) -> None:
    known = {f.name for f in fields(obj)}
    for k, v in data.items():
        key = k.replace("-", "_")
        if key not in known or key in ("boxes", "aliases"):
            raise ValueError(f"{where}: unknown setting {k!r}")
        setattr(obj, key, v)


def load_config(path: str | Path | None) -> BridgeConfig:
    cfg = BridgeConfig()
    if path is None:
        return cfg
    with open(path, "rb") as f:
        data = tomllib.load(f)
    _apply(cfg, data.get("bridge", {}), "[bridge]")
    for i, b in enumerate(data.get("box", [])):
        bc = BoxConfig()
        _apply(bc, b, f"[[box]] #{i + 1}")
        if not bc.host:
            raise ValueError(f"[[box]] #{i + 1}: host is required")
        cfg.boxes.append(bc)
    cfg.aliases = dict(data.get("aliases", {}))
    names = [b.name.lower() for b in cfg.boxes]
    if len(set(names)) != len(names):
        raise ValueError("two [[box]] entries have the same name")
    return cfg


class Bridge:
    def __init__(self, cfg: BridgeConfig):
        if not cfg.boxes:
            raise ValueError("no [[box]] configured")
        self.cfg = cfg
        self.boxes = {b.name: Box(b) for b in cfg.boxes}
        self.vxi = Vxi11Server(self.boxes, cfg.aliases, cfg.max_recv_size)
        self._servers: list[socketserver.BaseServer] = []
        self._registered: list[tuple[int, int, int, int]] = []
        self.stopped = threading.Event()

    def start(self) -> None:
        c = self.cfg
        core = rpc.Dispatcher()
        core.add(DEVICE_CORE, VXI11_VERSION, self.vxi.core)
        abort = rpc.Dispatcher()
        abort.add(DEVICE_ASYNC, VXI11_VERSION, self.vxi.abort)

        core_srv = rpc.serve_tcp(core, c.listen, c.core_port)
        abort_srv = rpc.serve_tcp(abort, c.listen, c.abort_port)
        self._servers += [core_srv, abort_srv]
        core_port = core_srv.server_address[1]
        self.vxi.abort_port = abort_port = abort_srv.server_address[1]
        log.info("VXI-11 core channel on %s:%d, abort channel on :%d", c.listen, core_port,
                 abort_port)

        mappings = [(DEVICE_CORE, VXI11_VERSION, rpc.IPPROTO_TCP, core_port),
                    (DEVICE_ASYNC, VXI11_VERSION, rpc.IPPROTO_TCP, abort_port)]
        self._start_portmapper(mappings)

        if c.admin_port:
            self._servers.append(self._serve_admin(c.admin_port))
        for b in self.boxes.values():
            b.start()

    def _start_portmapper(self, mappings) -> None:
        mode = self.cfg.portmapper
        if mode == "off":
            log.warning("portmapper disabled: clients must be told the core port directly")
            return
        if mode in ("auto", "builtin"):
            pm = rpc.Portmapper()
            for m in mappings:
                pm.set(*m)
            d = rpc.Dispatcher()
            d.add(rpc.PMAP_PROG, rpc.PMAP_VERS, pm.handler)
            try:
                self._servers.append(rpc.serve_tcp(d, self.cfg.listen, rpc.PMAP_PORT))
                self._servers.append(rpc.serve_udp(d, self.cfg.listen, rpc.PMAP_PORT))
                log.info("portmapper on port %d (TCP+UDP)", rpc.PMAP_PORT)
                return
            except OSError as e:
                for s in self._servers[2:]:
                    s.shutdown()
                    s.server_close()
                del self._servers[2:]
                if mode == "builtin":
                    raise
                log.info("port %d is taken (%s); registering with the running rpcbind",
                         rpc.PMAP_PORT, e)
        try:
            if not rpc.register_with_rpcbind(mappings):
                raise OSError("rpcbind refused the registration")
        except (OSError, rpc.XdrError) as e:
            raise RuntimeError(f"can't serve or register with a portmapper on port "
                               f"{rpc.PMAP_PORT}: {e}") from e
        self._registered = mappings
        log.info("registered with rpcbind")

    def _serve_admin(self, port: int) -> socketserver.TCPServer:
        bridge = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                line = self.rfile.readline(200).decode("ascii", "replace").strip()
                if line == "status":
                    reply = bridge.status()
                elif line.startswith("scan "):
                    reply = bridge.scan(line[5:].strip())
                elif line == "shutdown":
                    reply = {"ok": True}
                    threading.Thread(target=bridge.stop, daemon=True).start()
                else:
                    reply = {"error": f"unknown command {line!r}"}
                self.wfile.write((json.dumps(reply) + "\n").encode())

        srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, name="admin", daemon=True).start()
        return srv

    def status(self) -> dict:
        return {
            "boxes": [b.status() for b in self.boxes.values()],
            "links": [{"id": l.lid, "device": str(l.target), "client": l.conn.peer[0]
                       if l.conn else None, "srq": l.srq_enabled}
                      for l in list(self.vxi.links.values())],
        }

    def scan(self, name: str) -> dict:
        """List listeners on a box's bus, using the bridge's own session."""
        from .box import BoxUnavailable
        from .enet import EnetError, GpibError
        box = next((b for n, b in self.boxes.items() if n.lower() == name.lower()), None)
        if box is None:
            return {"error": f"no box named {name!r}"}
        try:
            with box.lock:
                box.ensure_open()
                return {"box": box.name, "listeners": box.enet.find_listeners()}
        except EnetError as e:
            box.note_session_lost(e)
            return {"error": str(e)}
        except (BoxUnavailable, GpibError) as e:
            return {"error": str(e)}

    def stop(self) -> None:
        if self.stopped.is_set():
            return
        log.info("shutting down")
        self.vxi.stop()
        for s in self._servers:
            try:
                s.shutdown()
                s.server_close()
            except Exception:
                pass
        if self._registered:
            rpc.unregister_with_rpcbind(self._registered)
        for b in self.boxes.values():
            b.stop()
        self.stopped.set()


def admin_request(cmd: str, port: int = 9012, timeout: float = 15.0) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(cmd.encode() + b"\n")
        f = s.makefile("rb")
        return json.loads(f.readline())
