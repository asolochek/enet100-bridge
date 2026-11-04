# enet100-bridge

A small VXI-11 server that makes a **National Instruments GPIB-ENET/100** usable
from modern VISA libraries: Keysight IO Libraries, NI-VISA, PyVISA (with either
backend or pyvisa-py), and anything built on them.

NI dropped GPIB-ENET/100 support after NI-488.2 17.6 and the box's firmware
(REV C.9) is final. The bridge speaks the box's own protocol on one side and
standard VXI-11 on the other, so instruments on its GPIB bus appear as ordinary
LAN instruments:

```
TCPIP::<bridge host>::gpib0,<address>::INSTR
```

```
 VISA app ──VXI-11──▶ enet100-bridge ──NI ENET protocol──▶ GPIB-ENET/100 ──GPIB──▶ instruments
 (any host)           (Windows/Linux/Pi)  TCP 5000/5015
```

No dependencies beyond Python 3.11+. The bridge doesn't need NI-488.2 or NI-VISA.

## Status

Tested against a GPIB-ENET/100 (firmware REV C.9) and an HP 3457A, from
Keysight VISA through PyVISA on Windows 11: identify, reads with LF termination,
150-reading bursts, device clear, serial poll, SRQ events, REN/GTL, writes and
reads to empty addresses, clean shutdown and reconnect.

Not supported: the GPIB-ENET/1000 (it uses a different handshake), ATN and
pass-control `docmd`s, and interrupting a call in progress (`device_abort` is
accepted but the call ends at its own timeout).

## Install

### Windows

From an **elevated** PowerShell in the repository folder:

```powershell
powershell -ExecutionPolicy Bypass -File install\install-windows.ps1
```

This installs the bridge into `C:\Program Files\enet100-bridge` and opens the
firewall ports. It then runs `enet100-bridge configure` (see
[Configure](#configure)) to write `C:\ProgramData\enet100-bridge\config.toml`;
`-EnetHost <ip>` skips the prompts. Finally it registers a scheduled task that
starts the bridge at boot (as SYSTEM) and restarts it if it stops. Run it again
to update; it offers to review the existing config. Other actions:

```powershell
install\install-windows.ps1 -Action Status     # task state, connected box, log tail
install\install-windows.ps1 -Action Restart
install\install-windows.ps1 -Action Uninstall  # add -Purge to delete the config too
```

### Linux (systemd)

```sh
sudo ./install/install-linux.sh            # or: --host 192.168.1.50
sudo ./install/install-linux.sh --uninstall
```

This installs into `/opt/enet100-bridge`, runs `configure` to write
`/etc/enet100-bridge/config.toml`, and sets up an `enet100-bridge.service` unit.
If `rpcbind` already owns port 111, the bridge registers with it instead of
running its own portmapper.

### By hand

```sh
pip install .
enet100-bridge configure --config enet100-bridge.toml
enet100-bridge run --config enet100-bridge.toml
```

## Configure

`enet100-bridge configure` sets the bridge up much like NI MAX's "add device":

```
Found on the network:
  1. 192.168.1.50    00:80:2F:12:34:56  GPIB-ENET/100  fw C9   gpib-enet-lab  <- gpib0

  a) add a box        r) remove   n) rename   h) change address
  s) scan a bus       i) box network settings
  f) search again     w) save and exit        q) exit without saving
```

- **Add**: pick a discovered box by number, or type an IP address or host
  name. Discovery doesn't need to work; a box that doesn't answer can still be
  added. Each box gets an interface name (`gpib0`, `gpib1`, ...).
- **Scan**: lists the instruments that answer on a box's bus, with their VISA
  resource names. If the bridge is running, the scan goes through it (the box
  should have one client); otherwise configure opens a short session itself.
- **GPIB settings**: the box's GPIB address (primary and secondary), system
  controller, assert REN, automatic serial polling, CIC protocol, bus timing
  (T1), HS488 cable length, board timeout and IFC on connect. These are the
  settings NI MAX offers. The bridge sends them each time it opens its session
  with the box, because every new session starts from the box's defaults (REN
  off, for one). `l` reads the values back: from the running bridge's session
  if there is one, otherwise the defaults a new session gets.
- **Network settings**: shows what the box reports about itself (IP, netmask,
  host name, MAC, firmware). The box gets these from DHCP, and neither this
  tool nor NI's can change them. For a fixed address or name, add a DHCP
  reservation for the box's MAC address on your DHCP server.
- Without a terminal: `enet100-bridge configure --add 192.168.1.50 [--name gpib1]`.

With no `--config`, it edits the system config (`C:\ProgramData\enet100-bridge\config.toml`
or `/etc/enet100-bridge/config.toml`). Restart the bridge afterwards.

**If discovery finds nothing**:
- After answering a query, the box ignores new ones for about 10 s.
  Discovery waits that out and asks again, so a search can take ~11 s, but
  another tool searching at the same moment can still collide with it. Try
  again.
- Broadcasts don't cross routers, so a box on another subnet has to be added
  by address.
- The answer is a UDP broadcast, so a host firewall that blocks inbound UDP for
  Python drops it. The Windows installer adds a rule for discovery replies, and
  warns if an active Block rule covers the bridge's Python.

## Use

```python
import pyvisa
rm = pyvisa.ResourceManager()
dmm = rm.open_resource("TCPIP::bridge-host::gpib0,22::INSTR")
dmm.read_termination = "\n"
print(dmm.query("*IDN?"))
```

- **Keysight Connection Expert**: add a LAN instrument by host name, choose
  *VXI-11*, and set the remote device name to `gpib0,22`.
- **NI MAX**: *Create New → VISA TCP/IP Resource → Manual entry of LAN
  instrument*, with the bridge's host name and LAN device name `gpib0,22`.
- Secondary addresses: `gpib0,22,3`. The bus itself: `TCPIP::host::gpib0::INTFC`
  (IFC, REN, ATN command bytes via `viGpibCommand`, bus status).
- Several boxes: add more `[[box]]` sections named `gpib1`, `gpib2`, ...

Quick checks from the bridge host:

```sh
enet100-bridge query gpib0,22 "*IDN?"
enet100-bridge status
```

### Instruments that don't send EOI

Many older instruments (the HP 3457A, for example) don't assert EOI at the end
of a reply. Set the read termination character (`read_termination = "\n"` in
PyVISA, or `VI_ATTR_TERMCHAR_EN`) so reads end at the LF. Otherwise every read
ends with a timeout.

## The box's rules (and what the bridge does about them)

The GPIB-ENET/100 is fragile in ways that will cost you a power cycle:

1. **It ignores the first ~45–60 s after power-on.** TCP comes up at ~13 s,
   but a session opened before the firmware is ready leaves the box deaf until
   it is power-cycled. The bridge watches the box with NI's UDP discovery query,
   which is harmless. When the box reappears after being gone, the bridge waits
   `boot_settle_s` (60 s) before connecting.
2. **Never close a half-set-up session.** Closing a connection after only the
   first command wedges the box. The bridge never closes a session that didn't
   finish its setup, and closes complete ones cleanly (devices offline first).
3. **One client at a time is the safe assumption.** Two hosts using the box at
   once hasn't been tested. Point everything at the bridge instead of giving
   each host its own NI driver.

If the box wedges anyway (it accepts connections but never answers), set
`recovery_command` in the config to something that power-cycles it, e.g. a
smart plug. The example config shows a Home Assistant version.

Stop the bridge with `enet100-bridge stop`, SIGTERM, Ctrl+C, or
`install-windows.ps1 -Action Stop`. Each of these closes the sessions cleanly.
Killing the process outright is less polite to the box.

## How it works

- `src/enet100_bridge/enet.py`: the GPIB-ENET/100 protocol client (board
  session, device handles, I/O, ibconfig, serial poll, ibwait).
- `discovery.py`: NI's UDP discovery on port 44515.
- `rpc.py`: ONC RPC/XDR, record marking, and a portmapper.
- `vxi11.py`: the VXI-11 core, abort and interrupt channels.
- `box.py`: connection management, reconnects, recovery.
- `docs/PROTOCOL.md`: the GPIB-ENET/100 wire protocol, as far as it is known.

Every VXI-11 link to the same address shares one GPIB-ENET device handle. The
handle is opened on first use and kept open. SRQ is watched on the handle's
`:5015` channel only while a client has it enabled.

## Development

```sh
uv sync
uv run pytest
```

The tests run against `tests/fake_enet.py`, a simulated box that enforces the
same wedge rule as the real one, so a change that would brick real hardware
fails a test.

## License

MIT, see [LICENSE](LICENSE).

## Credits

The protocol was decoded from packet captures of NI-488.2 17.6. ESRF's BLISS
project (`bliss/comm/gpib/libnienet.py`, originally "test-enet" by Robert
Jordens, 2005) was the starting reference. This is an independent
implementation and contains none of its code.

GPIB-ENET, NI-488.2, NI-VISA and NI MAX are trademarks of National Instruments.
This project is not affiliated with NI or Keysight.
