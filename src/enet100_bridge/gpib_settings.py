"""The GPIB (board) settings NI MAX offers for a GPIB-ENET/100.

They live in the bridge's config and are sent to the box with board-level
ibconfig every time the bridge opens its board session, the way NI's driver
applies the settings MAX stores. They belong to the session: each new board
session starts from the box's defaults (REN off, T1 500 ns, ...). Option codes
are NI-488.2's IbcXXX values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .enet import (IBC_AUTOPOLL, IBC_CICPROT, IBC_HSCABLE, IBC_PAD, IBC_SAD, IBC_SC,
                   IBC_SRE, IBC_TIMING, IBC_TMO, TMO_SECONDS, tmo_code_for)

TIMING_NS = {2000: 1, 500: 2, 350: 3}


@dataclass(frozen=True)
class Setting:
    key: str                      # BoxConfig field
    label: str
    opt: int                      # ibconfig option
    kind: str                     # "bool", "int", "choice"
    encode: Callable[[object], int]
    decode: Callable[[int], object]
    help: str
    lo: int = 0
    hi: int = 0
    choices: tuple = ()

    def fmt(self, v) -> str:
        if self.kind == "bool":
            return "yes" if v else "no"
        if self.key == "gpib_secondary":
            return "none" if v < 0 else str(v)
        if self.key == "bus_timing_ns":
            return f"{v} ns" if v < 1000 else f"{v // 1000} us"
        if self.key == "hs488_cable_m":
            return "off" if v == 0 else f"{v} m"
        if self.key == "board_timeout_s":
            return f"{v:g} s"
        return str(v)

    def parse(self, text: str):
        t = text.strip().lower()
        if self.kind == "bool":
            if t in ("y", "yes", "on", "1", "true"):
                return True
            if t in ("n", "no", "off", "0", "false"):
                return False
            raise ValueError("answer yes or no")
        if self.key == "gpib_secondary" and t in ("none", "-", "-1", ""):
            return -1
        if self.key == "hs488_cable_m" and t == "off":
            return 0
        m = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*(ns|us|µs|ms|s|m)?", t)
        if not m:
            raise ValueError("not a number")
        v = float(m.group(1))
        unit = m.group(2)
        if self.key == "bus_timing_ns" and unit in ("us", "µs"):
            v *= 1000
        if self.key == "board_timeout_s" and unit == "ms":
            v /= 1000
        v = int(v) if v.is_integer() else v
        if self.kind == "choice":
            if v not in self.choices:
                raise ValueError("choose one of " + ", ".join(self.fmt(c) for c in self.choices))
            return v
        if not isinstance(v, int):
            raise ValueError("must be a whole number")
        if not self.lo <= v <= self.hi:
            raise ValueError(f"must be {self.lo}..{self.hi}")
        return v


_TMO_CHOICES = tuple(s for s in (0.01, 0.03, 0.1, 0.3, 1, 3, 10, 30, 100, 300, 1000))

SETTINGS: list[Setting] = [
    Setting("gpib_address", "Primary address", IBC_PAD, "int",
            int, int, "the box's own GPIB address", 0, 30),
    Setting("gpib_secondary", "Secondary address", IBC_SAD, "int",
            lambda v: 0 if v < 0 else 0x60 + v, lambda c: -1 if c == 0 else c - 0x60,
            "the box's secondary address, or none", -1, 30),
    Setting("system_controller", "System controller", IBC_SC, "bool",
            int, bool, "the box controls the bus (IFC, REN); leave on unless another controller is"),
    Setting("assert_ren", "Assert REN when SC", IBC_SRE, "bool",
            int, bool, "hold REN so instruments go to remote when addressed"),
    Setting("autopoll", "Automatic serial polling", IBC_AUTOPOLL, "bool",
            int, bool, "the box polls instruments on SRQ; SRQ events need this"),
    Setting("cic_protocol", "CIC protocol", IBC_CICPROT, "bool",
            int, bool, "pass control back and forth with another controller"),
    Setting("bus_timing_ns", "Bus timing (T1)", IBC_TIMING, "choice",
            lambda v: TIMING_NS[v], lambda c: {v: k for k, v in TIMING_NS.items()}.get(c, c),
            "handshake settling time; use 2 us for long or slow buses",
            choices=(2000, 500, 350)),
    Setting("hs488_cable_m", "HS488 cable length", IBC_HSCABLE, "int",
            int, int, "total cable length in metres for HS488, or off", 0, 15),
    Setting("board_timeout_s", "Board timeout", IBC_TMO, "choice",
            tmo_code_for, lambda c: TMO_SECONDS[c] if 0 < c < len(TMO_SECONDS) else c,
            "timeout for bus-level operations (device reads/writes use the client's)",
            choices=_TMO_CHOICES),
]

BY_KEY = {s.key: s for s in SETTINGS}


def desired(cfg) -> list[tuple[Setting, object]]:
    return [(s, getattr(cfg, s.key)) for s in SETTINGS]
