"""`enet100-bridge configure`: set up the bridge like NI MAX's "add device" flow.

Finds GPIB-ENET/100 boxes by discovery (or takes an IP address), lets you pick
and name them (gpib0, gpib1, ...), scans each box's bus for instruments, shows
the box's network settings, and writes the config file.

The box's network settings come from DHCP, so they are shown but not edited.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import sys
from dataclasses import fields
from pathlib import Path

from . import discovery
from .box import BoxConfig
from .server import BridgeConfig, admin_request, load_config

_NAME_OK = re.compile(r"^[a-z][a-z0-9_-]*$", re.I)


# -- writing the config ---------------------------------------------------------------------

def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def dump_config(cfg: BridgeConfig) -> str:
    defaults_box = BoxConfig()
    out = ["# enet100-bridge configuration (written by `enet100-bridge configure`).",
           "# See config.example.toml in the source for every setting.", "", "[bridge]"]
    for f in fields(cfg):
        if f.name in ("boxes", "aliases"):
            continue
        out.append(f"{f.name} = {_toml_value(getattr(cfg, f.name))}")
    for b in cfg.boxes:
        out += ["", "[[box]]", f"name = {_toml_value(b.name)}", f"host = {_toml_value(b.host)}"]
        for f in fields(b):
            if f.name in ("name", "host"):
                continue
            v = getattr(b, f.name)
            if v != getattr(defaults_box, f.name):
                out.append(f"{f.name} = {_toml_value(v)}")
    if cfg.aliases:
        out += ["", "[aliases]"]
        out += [f"{_toml_value(k)} = {_toml_value(v)}" for k, v in sorted(cfg.aliases.items())]
    return "\n".join(out) + "\n"


def save_config(cfg: BridgeConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(dump_config(cfg), encoding="utf-8")
    load_config(tmp)                       # never write something we can't read back
    os.replace(tmp, path)


# -- bus scan ---------------------------------------------------------------------------------

def scan_bus(cfg: BridgeConfig, box: BoxConfig) -> tuple[list[int] | None, str]:
    """Listener addresses on a box's bus. Goes through the running bridge if
    there is one (only one client should own the box), else connects directly."""
    if cfg.admin_port:
        try:
            r = admin_request(f"scan {box.name}", cfg.admin_port, timeout=30)
            if "listeners" in r:
                return r["listeners"], "via the running bridge"
            if "no box named" not in r.get("error", ""):
                return None, f"the running bridge says: {r.get('error')}"
        except OSError:
            pass                           # no bridge running
    from .enet import Enet100, EnetError, GpibError
    e = Enet100(box.host, cmd_port=box.cmd_port, wait_port=box.wait_port)
    try:
        e.open()
    except (EnetError, OSError) as ex:
        return None, f"couldn't open a session: {ex}"
    try:
        return e.find_listeners(), "directly"
    except (EnetError, GpibError) as ex:
        return None, f"scan failed: {ex}"
    finally:
        try:
            e.close()
        except Exception:
            pass


# -- interactive UI ---------------------------------------------------------------------------

class Ui:
    def __init__(self, inp=input, out=print):
        self.input, self.print = inp, out

    def ask(self, prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        try:
            v = self.input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            v = ""
        return v or default

    def yes(self, prompt: str, default: bool = True) -> bool:
        v = self.ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
        return default if not v else v.startswith("y")


def _resolve(host: str) -> str | None:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def _next_name(cfg: BridgeConfig) -> str:
    used = {b.name.lower() for b in cfg.boxes}
    i = 0
    while f"gpib{i}" in used:
        i += 1
    return f"gpib{i}"


class Configurator:
    def __init__(self, cfg: BridgeConfig, path: Path, ui: Ui | None = None):
        self.cfg, self.path, self.ui = cfg, path, ui or Ui()
        self.found: list[discovery.BoxInfo] = []
        self.dirty = False

    # listing ----------------------------------------------------------------------------

    def _configured_for(self, info: discovery.BoxInfo) -> BoxConfig | None:
        for b in self.cfg.boxes:
            if b.host == info.ip or _resolve(b.host) == info.ip:
                return b
        return None

    def search(self) -> None:
        self.ui.print("Searching for GPIB-ENET boxes (up to 12 s)...")
        self.found = sorted(discovery.discover(), key=lambda i: socket.inet_aton(i.ip))
        if not self.found:
            self.ui.print(discovery.NO_REPLY_HINT)

    def show(self) -> None:
        p = self.ui.print
        p("")
        p("Configured boxes:")
        if not self.cfg.boxes:
            p("  (none)")
        for b in self.cfg.boxes:
            p(f"  {b.name:<8} {b.host}")
        p("")
        p("Found on the network:")
        if not self.found:
            p("  (none)")
        for n, info in enumerate(self.found, 1):
            c = self._configured_for(info)
            tag = f"  <- {c.name}" if c else ""
            if info.model != "GPIB-ENET/100":
                tag += "  (not supported: ENET/100 only)"
            p(f"  {n}. {info}{tag}")
        p("")

    # actions ------------------------------------------------------------------------------

    def add(self) -> None:
        ui = self.ui
        choice = ui.ask("Number from the list, or an IP address / host name")
        if not choice:
            return
        info = None
        if choice.isdigit() and 1 <= int(choice) <= len(self.found):
            info = self.found[int(choice) - 1]
            host = info.ip
        else:
            host = choice
            ip = _resolve(host)
            if ip is None:
                ui.print(f"Can't resolve {host!r}.")
                return
            ui.print(f"Checking {host} ({ip}) with a discovery query (up to 12 s)...")
            info = discovery.is_present(ip)
            if info is None:
                ui.print("It didn't answer. That's fine if discovery is blocked here "
                         "(firewall, another subnet); the bridge will still try it.")
                if not ui.yes("Add it anyway"):
                    return
        if info is not None:
            if info.model != "GPIB-ENET/100":
                ui.print(f"{info.model} isn't supported (GPIB-ENET/100 only).")
                return
            existing = self._configured_for(info)
            if existing:
                ui.print(f"Already configured as {existing.name}.")
                return
            ui.print(f"  {info}")
            if info.hostname and _resolve(info.hostname) == info.ip:
                if ui.yes(f"Use its host name {info.hostname!r} instead of the IP "
                          f"(survives address changes)", default=False):
                    host = info.hostname
        name = self._ask_name(_next_name(self.cfg))
        self.cfg.boxes.append(BoxConfig(name=name, host=host))
        self.dirty = True
        ui.print(f"Added {name} -> {host}. Instruments: TCPIP::<bridge host>::{name},<address>::INSTR")
        if ui.yes("Scan its bus for instruments now"):
            self.scan(self.cfg.boxes[-1])

    def _ask_name(self, default: str, current: str | None = None) -> str:
        while True:
            name = self.ui.ask("Interface name", default)
            taken = {b.name.lower() for b in self.cfg.boxes} - {(current or "").lower()}
            if not _NAME_OK.match(name):
                self.ui.print("Use letters, digits, - or _, starting with a letter (e.g. gpib1).")
            elif name.lower() in taken:
                self.ui.print(f"{name} is already used.")
            else:
                return name

    def _pick_box(self) -> BoxConfig | None:
        if not self.cfg.boxes:
            self.ui.print("No boxes configured yet.")
            return None
        if len(self.cfg.boxes) == 1:
            return self.cfg.boxes[0]
        name = self.ui.ask("Which box (name)", self.cfg.boxes[0].name)
        for b in self.cfg.boxes:
            if b.name.lower() == name.lower():
                return b
        self.ui.print(f"No box named {name}.")
        return None

    def remove(self) -> None:
        b = self._pick_box()
        if b and self.ui.yes(f"Remove {b.name} ({b.host})", default=False):
            self.cfg.boxes.remove(b)
            self.dirty = True

    def rename(self) -> None:
        b = self._pick_box()
        if b:
            new = self._ask_name(b.name, current=b.name)
            if new != b.name:
                b.name = new
                self.dirty = True

    def change_host(self) -> None:
        b = self._pick_box()
        if b:
            host = self.ui.ask(f"New IP address / host name for {b.name}", b.host)
            if host != b.host:
                b.host = host
                self.dirty = True

    def scan(self, b: BoxConfig | None = None) -> None:
        b = b or self._pick_box()
        if not b:
            return
        self.ui.print(f"Scanning {b.name} ({b.host}) for listeners at addresses 1-30...")
        self.ui.print("  (this opens a session on the box; nothing else should be using it)")
        listeners, how = scan_bus(self.cfg, b)
        if listeners is None:
            self.ui.print(f"  Scan failed: {how}")
            return
        if not listeners:
            self.ui.print(f"  No instruments answered ({how}).")
            return
        self.ui.print(f"  Found {len(listeners)} ({how}):")
        for pad in listeners:
            self.ui.print(f"    address {pad:<3} TCPIP::<bridge host>::{b.name},{pad}::INSTR")

    def network_settings(self) -> None:
        b = self._pick_box()
        if not b:
            return
        ip = _resolve(b.host)
        info = discovery.is_present(ip) if ip else None
        p = self.ui.print
        if info is None:
            p(f"{b.name} ({b.host}) didn't answer discovery, so its settings can't be read.")
            return
        p(f"Network settings of {b.name}, as the box reports them:")
        p(f"  Model        {info.model}  (firmware {info.firmware}, serial? {info.serial})")
        p(f"  MAC address  {info.mac}")
        p(f"  Host name    {info.hostname}")
        p(f"  IP address   {info.ip}")
        p(f"  Netmask      {info.netmask}")
        p(f"  Mode field   {info.ip_mode_text}")
        p(f"  Gateway?     {info.addr_a6}   (offset 0xA6, meaning unconfirmed)")
        p(f"  DNS server?  {info.addr_aa}   (offset 0xAA, meaning unconfirmed)")
        p("")
        p("The box gets these from DHCP; neither this tool nor NI's can change them.")
        p(f"To give it a fixed address or name, add a DHCP reservation for {info.mac}")
        p("on your DHCP server (it appears to take its host name from the reply too),")
        p("then power-cycle the box and wait a minute.")

    # main loop -------------------------------------------------------------------------------

    def run(self) -> bool:
        ui = self.ui
        self.search()
        if not self.cfg.boxes and len(self.found) == 1 and self.found[0].model == "GPIB-ENET/100":
            info = self.found[0]
            ui.print(f"Found {info}")
            if ui.yes("Add it as gpib0"):
                self.cfg.boxes.append(BoxConfig(name=_next_name(self.cfg), host=info.ip))
                self.dirty = True
                if ui.yes("Scan its bus for instruments now"):
                    self.scan(self.cfg.boxes[-1])
        while True:
            self.show()
            ui.print("  a) add a box        r) remove   n) rename   h) change address")
            ui.print("  s) scan a bus       i) box network settings")
            ui.print("  f) search again     w) save and exit        q) exit without saving")
            c = ui.ask("Choice", "w" if self.dirty else "q").lower()[:1]
            if c == "a":
                self.add()
            elif c == "r":
                self.remove()
            elif c == "n":
                self.rename()
            elif c == "h":
                self.change_host()
            elif c == "s":
                self.scan()
            elif c == "i":
                self.network_settings()
            elif c == "f":
                self.search()
            elif c == "w":
                if not self.cfg.boxes:
                    ui.print("Add at least one box first (the bridge needs one).")
                    continue
                save_config(self.cfg, self.path)
                ui.print(f"Saved {self.path}")
                return True
            elif c == "q":
                if self.dirty and not ui.yes("Discard changes", default=False):
                    continue
                return False


def default_config_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "enet100-bridge" / "config.toml"
    return Path("/etc/enet100-bridge/config.toml")


def main(path: str | None, add: str | None, name: str | None, log_file: str | None) -> int:
    p = Path(path) if path else default_config_path()
    if p.exists():
        cfg = load_config(p)
    else:
        cfg = BridgeConfig()
        if log_file:
            cfg.log_file = log_file
    if add:                                     # non-interactive: --add HOST [--name N]
        n = name or _next_name(cfg)
        if any(b.name.lower() == n.lower() for b in cfg.boxes):
            print(f"{n} is already configured", file=sys.stderr)
            return 1
        cfg.boxes.append(BoxConfig(name=n, host=add))
        save_config(cfg, p)
        print(f"Added {n} -> {add}; saved {p}")
        return 0
    if not sys.stdin.isatty():
        print("configure is interactive; use --add HOST [--name NAME] in scripts", file=sys.stderr)
        return 2
    return 0 if Configurator(cfg, p).run() else 1
