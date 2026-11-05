"""Keeps one GPIB-ENET/100 usable: connects when it's safe, reconnects after a
power cycle, and optionally runs a recovery command if the box wedges."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass

from . import discovery
from .enet import (MODE_BIN, MODE_EOT, Device, Enet100, EnetError, EnetTimeout, GpibError)

log = logging.getLogger(__name__)


PROBE_INTERVAL_S = 15.0


class BoxUnavailable(Exception):
    """The box can't be used right now (absent, booting, or wedged)."""


@dataclass
class BoxConfig:
    name: str = "gpib0"
    host: str = ""
    cmd_port: int = 5000
    wait_port: int = 5015
    boot_settle_s: float = 60.0
    connect_on_start: bool = True
    use_discovery: bool = True
    recovery_command: str = ""
    recovery_cooldown_s: float = 300.0
    retry_interval_s: float = 15.0
    keepalive_s: float = 60.0
    eos_8bit: bool = False
    # GPIB (board) settings, as in NI MAX; sent to the box on every connect.
    # Defaults are NI-488.2's. See gpib_settings.py.
    gpib_address: int = 0
    gpib_secondary: int = -1          # -1 = none
    system_controller: bool = True
    assert_ren: bool = True
    autopoll: bool = True
    cic_protocol: bool = False
    bus_timing_ns: int = 500          # 2000, 500 or 350
    hs488_cable_m: int = 0            # 0 = HS488 off
    board_timeout_s: float = 10
    ifc_on_connect: bool = False      # pulse IFC after connecting (become CIC at once)
    # Present the box to NI-488.2 (NI MAX, Keysight Connection Expert) as a GPIB-ENET/1000
    # on TCP 5000/5003/5005/5015 + HTTP 80 of `ni_listen`. Those ports are fixed, so each
    # box needs its own address; by default only the first box gets the bridge's address.
    ni_emulation: bool = True
    ni_listen: str = ""               # default: [bridge] listen


class Box:
    def __init__(self, cfg: BoxConfig):
        self.cfg = cfg
        self.enet = Enet100(cfg.host, cmd_port=cfg.cmd_port, wait_port=cfg.wait_port)
        self.lock = self.enet.lock
        self.devices: dict[tuple[int, int], Device] = {}
        self.last_absent: float | None = None
        self.last_failure: float | None = None
        self.last_failure_msg = ""
        self.last_recovery: float | None = None
        self.last_activity = time.monotonic()
        self.info: discovery.BoxInfo | None = None
        self._last_probe: float | None = None
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None

    @property
    def name(self) -> str:
        return self.cfg.name

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        self._monitor = threading.Thread(target=self._monitor_loop, name=f"box-{self.name}",
                                         daemon=True)
        self._monitor.start()

    def stop(self) -> None:
        self._stop.set()
        with self.lock:
            if self.enet.alive:
                log.info("%s: closing sessions", self.name)
                try:
                    self.enet.close()
                except Exception:
                    log.exception("%s: error while closing", self.name)
            self.devices.clear()

    def _probe_presence(self) -> bool:
        if not self.cfg.use_discovery:
            return True
        # Don't query more often than the box can answer (see discovery.BUSY_WINDOW_S).
        if self._last_probe and time.monotonic() - self._last_probe < PROBE_INTERVAL_S:
            return self.info is not None
        self._last_probe = time.monotonic()
        info = discovery.is_present(self.cfg.host)
        if info is None:
            if self.last_absent is None or self.info is not None:
                log.warning("%s: %s is not answering discovery", self.name, self.cfg.host)
            self.last_absent = time.monotonic()
            self.info = None
            return False
        if self.info is None:
            log.info("%s: found %s", self.name, info)
        self.info = info
        return True

    def ensure_open(self) -> None:
        """Open the board session if it isn't, or raise BoxUnavailable."""
        with self.lock:
            if self.enet.alive:
                return
            now = time.monotonic()
            if self.last_failure and now - self.last_failure < self.cfg.retry_interval_s:
                raise BoxUnavailable(f"{self.name}: {self.last_failure_msg} (retrying soon)")
            if not self._probe_presence():
                raise BoxUnavailable(f"{self.name}: {self.cfg.host} is not answering")
            if self.last_absent is not None:
                up = time.monotonic() - self.last_absent
                if up < self.cfg.boot_settle_s:
                    raise BoxUnavailable(
                        f"{self.name}: box came up {up:.0f} s ago; waiting until "
                        f"{self.cfg.boot_settle_s:.0f} s before connecting")
            try:
                self.devices.clear()
                self.enet.open()
                self.apply_gpib_settings()
                self.last_failure = None
                self.last_activity = time.monotonic()
            except (EnetError, OSError, GpibError) as e:
                self._failed(f"connect failed: {e}", wedged=isinstance(e, EnetTimeout))
                raise BoxUnavailable(f"{self.name}: {e}") from e

    def apply_gpib_settings(self) -> dict[str, object]:
        """Send the configured GPIB settings to the box (only those that differ).
        Returns {key: value read back} for every setting."""
        from .gpib_settings import SETTINGS
        actual: dict[str, object] = {}
        with self.lock:
            for s in SETTINGS:
                want = getattr(self.cfg, s.key)
                try:
                    have = s.decode(self.enet.ask(s.opt))
                    if have != want:
                        self.enet.config(s.opt, s.encode(want))
                        log.info("%s: %s %s -> %s", self.name, s.label, s.fmt(have),
                                 s.fmt(want))
                        have = s.decode(self.enet.ask(s.opt))
                        if have != want:
                            log.warning("%s: %s reads back as %s, not %s", self.name,
                                        s.label, s.fmt(have), s.fmt(want))
                    actual[s.key] = have
                except GpibError as e:
                    log.warning("%s: can't set %s: %s", self.name, s.label, e)
            # Bus-level calls may legitimately take as long as the board timeout.
            from .enet import SOCKET_MARGIN_S
            if self.enet._board:
                self.enet._board.timeout = max(10.0, float(self.cfg.board_timeout_s)) + SOCKET_MARGIN_S
            if self.cfg.ifc_on_connect and self.cfg.system_controller:
                try:
                    self.enet.ibsic()
                except GpibError as e:
                    log.warning("%s: IFC failed: %s", self.name, e)
        return actual

    def read_gpib_settings(self) -> dict[str, object]:
        from .gpib_settings import SETTINGS
        with self.lock:
            self.ensure_open()
            return {s.key: s.decode(self.enet.ask(s.opt)) for s in SETTINGS}

    def _failed(self, msg: str, wedged: bool) -> None:
        self.last_failure = time.monotonic()
        self.last_failure_msg = msg
        log.error("%s: %s", self.name, msg)
        if wedged:
            log.error("%s: the box accepts TCP but doesn't answer: it is probably wedged "
                      "and needs a power cycle", self.name)
            self._recover()

    def _recover(self) -> None:
        cmd = self.cfg.recovery_command
        if not cmd:
            return
        now = time.monotonic()
        if self.last_recovery and now - self.last_recovery < self.cfg.recovery_cooldown_s:
            return
        self.last_recovery = now
        log.warning("%s: running recovery command", self.name)
        try:
            r = subprocess.run(cmd, shell=True, timeout=120, capture_output=True, text=True)
            log.warning("%s: recovery command exited %d %s", self.name, r.returncode,
                        (r.stdout + r.stderr).strip()[:500])
        except Exception as e:
            log.error("%s: recovery command failed: %s", self.name, e)
        # Whatever happened, treat the box as just booted.
        self.last_absent = time.monotonic()

    def note_session_lost(self, e: Exception) -> None:
        with self.lock:
            self.devices.clear()
            self._failed(f"session lost: {e}", wedged=isinstance(e, EnetTimeout))

    # -- devices -------------------------------------------------------------------------

    def device(self, pad: int, sad: int = 0) -> Device:
        """A cached, open device handle. sad is 0 (none) or 0..30 (sent as 0x60+sad)."""
        with self.lock:
            self.ensure_open()
            key = (pad, sad)
            d = self.devices.get(key)
            if d is not None and d.is_open:
                return d
            mode = MODE_EOT | (MODE_BIN if self.cfg.eos_8bit else 0)
            try:
                d = self.enet.device(pad, 0x60 + sad if sad else 0, timeout_s=10.0,
                                     eos=0x0A, mode=mode)
            except EnetError as e:
                self.note_session_lost(e)
                raise BoxUnavailable(f"{self.name}: {e}") from e
            self.devices[key] = d
            return d

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    # -- monitor ---------------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        first = True
        while first or not self._stop.wait(5.0):
            first = False
            try:
                if not self.enet.alive:
                    if self.cfg.connect_on_start or self.devices:
                        try:
                            self.ensure_open()
                        except BoxUnavailable as e:
                            log.debug("%s", e)
                    elif self.cfg.use_discovery:
                        self._probe_presence()
                    continue
                idle = time.monotonic() - self.last_activity
                if idle >= self.cfg.keepalive_s and self.lock.acquire(timeout=0.1):
                    try:
                        if self.enet.alive:
                            self.enet.board_status()
                            self.last_activity = time.monotonic()
                    except EnetError as e:
                        self.note_session_lost(e)
                    except GpibError:
                        pass
                    finally:
                        self.lock.release()
            except Exception:
                log.exception("%s: monitor error", self.name)

    def status(self) -> dict:
        ident = self.enet.identity
        return {
            "name": self.name,
            "host": self.cfg.host,
            "connected": self.enet.alive,
            "model": ident.model if ident else (self.info.model if self.info else None),
            "mac": ident.mac if ident else (self.info.mac if self.info else None),
            "present": self.info is not None if self.cfg.use_discovery else None,
            "devices": sorted(f"{p}" + (f",{s}" if s else "") for p, s in self.devices),
            "last_error": self.last_failure_msg or None,
        }
