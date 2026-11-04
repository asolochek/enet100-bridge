from pathlib import Path

import pytest

from enet100_bridge import configure, discovery
from enet100_bridge.box import BoxConfig
from enet100_bridge.configure import Configurator, Ui, dump_config, save_config, scan_bus
from enet100_bridge.enet import Enet100
from enet100_bridge.server import BridgeConfig, load_config

from fake_enet import FakeEnet


def _info(ip="192.168.1.50", model="GPIB-ENET/100", mac="00:80:2F:12:34:56"):
    return discovery.BoxInfo(mac=mac, model=model, hostname="gpib-enet-lab", firmware="C9",
                             ip=ip, netmask="255.255.255.0", serial=1)


def scripted(answers):
    it = iter(answers)
    out = []

    def inp(prompt):
        a = next(it)
        out.append(prompt + a)
        return a
    return Ui(inp, out.append), out


def test_config_round_trip(tmp_path):
    cfg = BridgeConfig(core_port=9100, log_file="C:/ProgramData/x/bridge.log")
    cfg.boxes += [BoxConfig(name="gpib0", host="192.168.1.50"),
                  BoxConfig(name="lab", host="enet.example", boot_settle_s=90,
                            recovery_command='curl -d "{\\"a\\":1}" http://x')]
    cfg.aliases["dmm"] = "gpib0,22"
    p = tmp_path / "config.toml"
    save_config(cfg, p)
    back = load_config(p)
    assert dump_config(back) == dump_config(cfg)
    assert back.boxes[1].recovery_command == cfg.boxes[1].recovery_command
    assert back.core_port == 9100 and back.aliases == {"dmm": "gpib0,22"}
    save_config(back, p)
    assert (tmp_path / "config.toml.bak").exists()


def test_configure_pick_found_box(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: [_info()])
    ui, out = scripted(["y", "n", "w"])     # add as gpib0, don't scan, save
    p = tmp_path / "c.toml"
    assert Configurator(BridgeConfig(), p, ui).run()
    cfg = load_config(p)
    assert [(b.name, b.host) for b in cfg.boxes] == [("gpib0", "192.168.1.50")]


def test_configure_manual_ip_when_discovery_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: [])
    monkeypatch.setattr(discovery, "is_present", lambda *a, **k: None)
    # add -> IP -> "add anyway" -> name -> no scan -> save
    ui, out = scripted(["a", "10.9.8.7", "y", "bench", "n", "w"])
    p = tmp_path / "c.toml"
    assert Configurator(BridgeConfig(), p, ui).run()
    assert [(b.name, b.host) for b in load_config(p).boxes] == [("bench", "10.9.8.7")]
    assert any("No GPIB-ENET box answered" in line for line in out)


def test_configure_rejects_enet1000_and_duplicates(tmp_path, monkeypatch):
    found = [_info(), _info(ip="192.168.1.51", model="GPIB-ENET/1000", mac="00:80:2F:00:00:01")]
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: found)
    cfg = BridgeConfig()
    cfg.boxes.append(BoxConfig(name="gpib0", host="192.168.1.50"))
    ui, out = scripted(["a", "1", "a", "2", "q"])
    assert not Configurator(cfg, tmp_path / "c.toml", ui).run()
    assert any("Already configured as gpib0" in l for l in out)
    assert any("isn't supported" in l for l in out)
    assert not (tmp_path / "c.toml").exists()


def test_configure_non_interactive_add(tmp_path):
    p = tmp_path / "c.toml"
    assert configure.main(str(p), "192.168.1.50", None, "C:/x/bridge.log") == 0
    assert configure.main(str(p), "192.168.1.60", "gpib1", None) == 0
    assert configure.main(str(p), "192.168.1.70", "gpib1", None) == 1
    cfg = load_config(p)
    assert [(b.name, b.host) for b in cfg.boxes] == [("gpib0", "192.168.1.50"),
                                                    ("gpib1", "192.168.1.60")]
    assert cfg.log_file == "C:/x/bridge.log"


@pytest.fixture
def fake():
    f = FakeEnet()
    yield f
    f.close()


def test_find_listeners_sends_ifc_when_not_cic(fake):
    e = Enet100("127.0.0.1", cmd_port=fake.cmd_port, wait_port=fake.wait_port)
    e.open()
    assert e.find_listeners() == [4]
    assert fake.cic
    e.close()
    assert fake.violations == []


def test_scan_bus_direct(fake):
    cfg = BridgeConfig(admin_port=0)
    box = BoxConfig(name="gpib0", host="127.0.0.1", cmd_port=fake.cmd_port,
                    wait_port=fake.wait_port)
    assert scan_bus(cfg, box) == ([4], "directly")
    assert fake.violations == [] and "board" in fake.closed_cleanly


def test_discover_resends_after_busy_window(monkeypatch):
    # A box that ignores the first query (still busy from an earlier one).
    import socket, threading
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    reply = bytearray(184)
    reply[0:4] = b"ED\x08\x01"
    reply[0x1C:0x1C + 13] = b"GPIB-ENET/100"
    reply[0x9E:0xA2] = socket.inet_aton("127.0.0.1")
    reply[-2:] = b"NI"
    seen = []

    def serve():
        while len(seen) < 2:
            data, addr = srv.recvfrom(2048)
            seen.append(data)
            if len(seen) == 2:
                srv.sendto(bytes(reply), addr)

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setattr(discovery, "DISCOVERY_PORT", port)
    monkeypatch.setattr(discovery, "BUSY_WINDOW_S", 0.3)
    found = discovery.discover(timeout=2.0, target="127.0.0.1", bind="127.0.0.1",
                               want_ip="127.0.0.1")
    srv.close()
    assert len(seen) == 2 and [f.model for f in found] == ["GPIB-ENET/100"]


def test_gpib_setting_parse_and_format():
    from enet100_bridge.gpib_settings import BY_KEY
    t = BY_KEY["bus_timing_ns"]
    assert t.parse("2us") == 2000 and t.parse("350 ns") == 350 and t.fmt(2000) == "2 us"
    with pytest.raises(ValueError):
        t.parse("100")
    sad = BY_KEY["gpib_secondary"]
    assert sad.parse("none") == -1 and sad.encode(-1) == 0 and sad.encode(5) == 0x65
    assert sad.decode(0x65) == 5 and sad.decode(0) == -1
    tmo = BY_KEY["board_timeout_s"]
    assert tmo.parse("3") == 3 and tmo.encode(3) == 12 and tmo.decode(13) == 10.0
    assert tmo.parse("300ms") == 0.3
    assert BY_KEY["hs488_cable_m"].parse("off") == 0
    with pytest.raises(ValueError):
        BY_KEY["gpib_address"].parse("31")


def test_bridge_applies_gpib_settings_on_connect(fake):
    from enet100_bridge.box import Box
    box = Box(BoxConfig(name="gpib0", host="127.0.0.1", cmd_port=fake.cmd_port,
                        wait_port=fake.wait_port, use_discovery=False, boot_settle_s=0,
                        gpib_address=2, bus_timing_ns=2000, board_timeout_s=3,
                        ifc_on_connect=True))
    box.ensure_open()
    o = fake.board_opts
    assert (o[0x01], o[0x11], o[0x03], o[0x0B]) == (2, 1, 12, 1)   # PAD, T1, TMO, REN on
    assert fake.cic                                                 # IFC sent
    assert box.read_gpib_settings()["gpib_address"] == 2
    box.stop()
    assert fake.violations == []


def test_configure_gpib_screen(tmp_path, monkeypatch, fake):
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: [])
    cfg = BridgeConfig(admin_port=0)
    cfg.boxes.append(BoxConfig(name="gpib0", host="127.0.0.1", cmd_port=fake.cmd_port,
                               wait_port=fake.wait_port))
    # g -> l (read live) -> 7 (timing) "2us" -> 1 (address) "31" (rejected) -> done -> save
    ui, out = scripted(["g", "l", "7", "2us", "1", "31", "", "w"])
    p = tmp_path / "c.toml"
    assert Configurator(cfg, p, ui).run()
    assert load_config(p).boxes[0].bus_timing_ns == 2000
    assert any("Read directly" in l for l in out) and any("new-session default" in l for l in out)
    assert any("Assert REN when SC" in l and "<- differs" in l for l in out)
    assert any("must be 0..30" in l for l in out)
