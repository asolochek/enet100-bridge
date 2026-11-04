"""Command line: enet100-bridge {run,discover,status,stop,query}."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading

from . import __version__


def _default_config() -> str | None:
    env = os.environ.get("ENET100_BRIDGE_CONFIG")
    if env:
        return env
    for p in ("enet100-bridge.toml",
              os.path.join(os.environ.get("PROGRAMDATA", ""), "enet100-bridge", "config.toml"),
              "/etc/enet100-bridge/config.toml"):
        if p and os.path.isfile(p):
            return p
    return None


def _setup_logging(level: str, log_file: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def cmd_run(args) -> int:
    from .server import Bridge, load_config
    path = args.config or _default_config()
    if not path:
        print("no config file: pass --config, set ENET100_BRIDGE_CONFIG, or create "
              "enet100-bridge.toml (see config.example.toml)", file=sys.stderr)
        return 2
    cfg = load_config(path)
    _setup_logging(args.log_level or cfg.log_level, cfg.log_file)
    log = logging.getLogger("enet100_bridge")
    log.info("enet100-bridge %s, config %s", __version__, path)
    bridge = Bridge(cfg)
    try:
        bridge.start()
    except Exception as e:
        log.error("startup failed: %s", e)
        bridge.stop()
        return 1

    def on_signal(signum, frame):
        threading.Thread(target=bridge.stop, daemon=True).start()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_signal)
    # wait() with a timeout so Ctrl+C is delivered on Windows.
    while not bridge.stopped.wait(1.0):
        pass
    log.info("stopped")
    return 0


def cmd_configure(args) -> int:
    from .configure import main as configure_main
    return configure_main(args.config, args.add, args.name, args.log_file)


def cmd_discover(args) -> int:
    from .discovery import NO_REPLY_HINT, discover
    boxes = discover(timeout=args.timeout)
    if not boxes:
        print(NO_REPLY_HINT, file=sys.stderr)
        return 1
    for b in boxes:
        print(b)
    return 0


def _admin_port(args) -> int:
    if args.admin_port:
        return args.admin_port
    path = args.config or _default_config()
    if path:
        from .server import load_config
        return load_config(path).admin_port
    return 9012


def cmd_status(args) -> int:
    from .server import admin_request
    try:
        print(json.dumps(admin_request("status", _admin_port(args)), indent=2))
    except OSError as e:
        print(f"bridge is not running (or admin port is off): {e}", file=sys.stderr)
        return 1
    return 0


def cmd_stop(args) -> int:
    from .server import admin_request
    try:
        admin_request("shutdown", _admin_port(args))
    except OSError as e:
        print(f"bridge is not running (or admin port is off): {e}", file=sys.stderr)
        return 1
    print("shutdown requested")
    return 0


def cmd_query(args) -> int:
    from .client import Vxi11Client
    term = None if args.no_termchar else 0x0A
    with Vxi11Client(args.host, args.device, timeout=args.timeout) as c:
        if args.command is None:
            print(f"opened {args.device}: link {c.lid}, maxRecvSize {c.max_recv_size}")
            return 0
        if args.write_only:
            c.write(args.command.encode())
            return 0
        print(c.query(args.command, termchar=term).rstrip("\r\n"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="enet100-bridge",
                                 description="VXI-11 bridge for NI GPIB-ENET/100 boxes")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run the bridge")
    p.add_argument("-c", "--config")
    p.add_argument("--log-level")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("configure", help="find boxes, pick and name them, scan their buses, "
                                         "and write the config")
    p.add_argument("-c", "--config", help="config file (default: the system location)")
    p.add_argument("--add", metavar="HOST", help="add this box without prompting")
    p.add_argument("--name", help="interface name for --add (default: next free gpibN)")
    p.add_argument("--log-file", help="log_file to set when creating a new config")
    p.set_defaults(fn=cmd_configure)

    p = sub.add_parser("discover", help="list GPIB-ENET boxes on the LAN (UDP broadcast)")
    p.add_argument("--timeout", type=float, default=12.0)
    p.set_defaults(fn=cmd_discover)

    for name, fn, hlp in (("status", cmd_status, "show the running bridge's state"),
                          ("stop", cmd_stop, "ask the running bridge to shut down cleanly")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("-c", "--config")
        p.add_argument("--admin-port", type=int)
        p.set_defaults(fn=fn)

    p = sub.add_parser("query", help="send a command through a VXI-11 server and print the reply")
    p.add_argument("device", help="e.g. gpib0,4")
    p.add_argument("command", nargs="?", help="e.g. '*IDN?' (omit to just open the link)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--write-only", action="store_true")
    p.add_argument("--no-termchar", action="store_true",
                   help="end the read on EOI only, not on LF")
    p.set_defaults(fn=cmd_query)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
