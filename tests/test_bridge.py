import socket
import struct
import threading
import time

import pytest

from enet100_bridge import discovery, rpc
from enet100_bridge.box import BoxConfig
from enet100_bridge.client import Vxi11Client, Vxi11Error
from enet100_bridge.enet import Enet100, tmo_code_for
from enet100_bridge.server import Bridge, BridgeConfig
from enet100_bridge.vxi11 import (CMD_BUS_STATUS, CMD_SEND_COMMAND, DEVICE_INTR,
                                  DEVICE_LOCKED, IO_ERROR, IO_TIMEOUT)

from fake_enet import FakeEnet

NI_TEST_PORTS = {"cmd": 0, "aux": 0, "keep": 0, "wait": 0, "http": 0}

# A discovery reply in the GPIB-ENET/100 layout (example MAC, name and addresses).
REPLY_HEX = (
    "45440801 4e02c930 4339 00d22dee 00802f123456 0000000000000000"
    "475049422d454e45542f313030" + "00" * 19 +
    "6770 69 62 2d 65 6e 65 74 2d 6c 61 62".replace(" ", "") + "00" * 83 +
    "0200 c0a80132 ffffff00 00000000 c0a80101 0000000000000000 4e49")


def test_parse_discovery_reply():
    rec = bytes.fromhex(REPLY_HEX.replace(" ", ""))
    assert len(rec) == 184
    info = discovery.parse_record(rec)
    assert info.model == "GPIB-ENET/100"
    assert info.hostname == "gpib-enet-lab"
    assert info.mac == "00:80:2F:12:34:56"
    assert info.ip == "192.168.1.50"
    assert info.firmware == "C9"


def test_query_record_matches_ni_max():
    q = discovery.query_record()
    assert q[:6] == bytes.fromhex("454401000002") and q[-2:] == b"NI" and len(q) == 184
    assert discovery.parse_record(q) is None


def test_tmo_codes():
    assert tmo_code_for(1.0) == 11
    assert tmo_code_for(2.0) == 12
    assert tmo_code_for(0.000001) == 1
    assert tmo_code_for(1e9) == 17


@pytest.fixture
def fake():
    f = FakeEnet()
    yield f
    f.close()


@pytest.fixture
def bridge(fake):
    cfg = BridgeConfig(listen="127.0.0.1", core_port=0, abort_port=0, portmapper="off",
                       admin_port=0)
    cfg.boxes.append(BoxConfig(name="gpib0", host="127.0.0.1", cmd_port=fake.cmd_port,
                               wait_port=fake.wait_port, use_discovery=False,
                               boot_settle_s=0, retry_interval_s=0, connect_on_start=False))
    b = Bridge(cfg)
    b.ni_ports = NI_TEST_PORTS
    b.start()
    b.core_port = b._servers[0].server_address[1]
    yield b
    b.stop()


def client(bridge, dev="gpib0,4", timeout=1.0):
    return Vxi11Client("127.0.0.1", dev, timeout=timeout, port=bridge.core_port)


def test_identify_and_board_setup_bytes(fake):
    e = Enet100("127.0.0.1", cmd_port=fake.cmd_port, wait_port=fake.wait_port)
    ident = e.open()
    assert ident.model == "GPIB-ENET  REV C.9"
    assert ident.mac == "00:80:2F:12:34:56"
    board = [blk for k, blk in fake.log if k.startswith("board")]
    assert board[0] == bytes([0x0B]) + bytes(11)
    assert board[1] == bytes.fromhex("500500000000000000000000")
    assert board[2] == bytes.fromhex("07001801000000000d000000")
    assert board[3] == bytes.fromhex("501001000000000000000000")
    e.close()
    assert fake.violations == [] and "board" in fake.closed_cleanly


def test_query(bridge, fake):
    with client(bridge) as c:
        assert c.query("ID?") == "HP3457A\r\n"
        assert fake.instruments[4].written[-1] == b"ID?"


def test_read_without_termchar_times_out_with_data(bridge, fake):
    # END OFF instrument: without a termchar, the read can only end at the timeout.
    with client(bridge, timeout=0.3) as c:
        c.write(b"ID?\n")
        with pytest.raises(Vxi11Error) as ei:
            c.read(termchar=None)
        assert ei.value.code == IO_TIMEOUT
        assert ei.value.data == b"HP3457A\r\n"


def test_large_read(bridge, fake):
    with client(bridge) as c:
        c.write(b"BIG?\n")
        # Line by line with LF as termchar, as VISA does for an END OFF instrument.
        lines = [c.read() for _ in range(150)]
        assert len(lines) == 150 and all(l.endswith(b"\r\n") for l in lines)


def test_requested_size(bridge, fake):
    with client(bridge) as c:
        c.write(b"ID?\n")
        first = c._core(12, rpc.Packer().int(c.lid).uint(3).uint(1000).uint(0).int(0).int(0)
                        .bytes())
        assert first.int() == 0 and first.int() == 1 and first.opaque() == b"HP3"
        assert c.read() == b"457A\r\n"


def test_no_listener(bridge, fake):
    with client(bridge, "gpib0,30") as c:
        with pytest.raises(Vxi11Error) as ei:
            c.write(b"ID?\n")
        assert ei.value.code == IO_ERROR


def test_unknown_device_name(bridge):
    with pytest.raises(Vxi11Error):
        client(bridge, "gpib7,4")
    with pytest.raises(Vxi11Error):
        client(bridge, "gpib0,31")


def test_clear_trigger_local_stb(bridge, fake):
    inst = fake.instruments[4]
    with client(bridge) as c:
        c.clear()
        c.trigger()
        c.local()
        c.write(b"SRQ\n")
        assert c.readstb() == 0x48
        assert c.readstb() == 0
    assert (inst.cleared, inst.triggered, inst.local_calls) == (1, 1, 1)


def test_two_links_share_one_handle_and_locking(bridge, fake):
    a, b = client(bridge), client(bridge)
    try:
        assert a.query("ID?") == b.query("ID?") == "HP3457A\r\n"
        devs = [blk for k, blk in fake.log if blk[0] == 0x07 and blk[1] == 0x02]
        assert len(devs) == 1
        a.lock()
        with pytest.raises(Vxi11Error) as ei:
            b.write(b"ID?\n")
        assert ei.value.code == DEVICE_LOCKED
        a.unlock()
        assert b.query("ID?") == "HP3457A\r\n"
    finally:
        a.close()
        b.close()


def test_lock_released_when_connection_drops(bridge, fake):
    a = client(bridge)
    a.lock()
    a.sock.close()                      # vanish without destroy_link
    time.sleep(0.2)
    with client(bridge) as b:
        assert b.query("ID?") == "HP3457A\r\n"


def test_interface_link(bridge, fake):
    with client(bridge, "gpib0") as c:
        assert c.docmd(CMD_SEND_COMMAND, b"\x3f\x5f\x40\x24") == b"\x3f\x5f\x40\x24"
        assert struct.unpack("!H", c.docmd(CMD_BUS_STATUS, struct.pack("!H", 1), 2))[0] == 1
        c.local()                        # REN off
        assert fake.ren is False
        assert struct.unpack("!H", c.docmd(CMD_BUS_STATUS, struct.pack("!H", 1), 2))[0] == 0
        c.remote()
        assert fake.ren is True


def test_srq_interrupt(bridge, fake):
    got = threading.Event()
    handles = []

    def intr(call):
        handles.append(call.args.opaque())
        got.set()
        return b""

    d = rpc.Dispatcher()
    d.add(DEVICE_INTR, 1, intr)
    srv = rpc.serve_tcp(d, "127.0.0.1", 0)
    port = srv.server_address[1]
    try:
        with client(bridge) as c:
            err = c._core(25, rpc.Packer().uint(0x7F000001).uint(port).uint(DEVICE_INTR)
                          .uint(1).int(0).bytes()).int()
            assert err == 0
            assert c._core(20, rpc.Packer().int(c.lid).bool(True).opaque(b"h1").bytes()).int() == 0
            c.write(b"SRQ\n")
            assert got.wait(5), "no device_intr_srq arrived"
            assert handles == [b"h1"]
            assert c.readstb() == 0x48
            c._core(20, rpc.Packer().int(c.lid).bool(False).opaque(b"").bytes())
    finally:
        srv.shutdown()
        srv.server_close()


def test_clean_shutdown(fake):
    cfg = BridgeConfig(listen="127.0.0.1", core_port=0, abort_port=0, portmapper="off",
                       admin_port=0)
    cfg.boxes.append(BoxConfig(name="gpib0", host="127.0.0.1", cmd_port=fake.cmd_port,
                               wait_port=fake.wait_port, use_discovery=False,
                               boot_settle_s=0, connect_on_start=False))
    b = Bridge(cfg)
    b.ni_ports = NI_TEST_PORTS
    b.start()
    port = b._servers[0].server_address[1]
    with Vxi11Client("127.0.0.1", "gpib0,4", timeout=1, port=port) as c:
        c.query("ID?")
    b.stop()
    time.sleep(0.3)
    assert fake.violations == []
    assert set(fake.closed_cleanly) == {"board", "dev4"}


def test_reconnect_after_power_cycle(bridge, fake):
    with client(bridge) as c:
        assert c.query("ID?") == "HP3457A\r\n"
        fake.power_cycle()
        with pytest.raises(Vxi11Error):
            c.query("ID?")
        # The bridge rebuilds its sessions on the next call.
        assert c.query("ID?") == "HP3457A\r\n"
    assert not fake.wedged


def test_portmapper_getport_and_dump():
    pm = rpc.Portmapper()
    pm.set(0x0607AF, 1, 6, 9010)
    d = rpc.Dispatcher()
    d.add(rpc.PMAP_PROG, rpc.PMAP_VERS, pm.handler)
    srv = rpc.serve_udp(d, "127.0.0.1", 0)
    try:
        port = srv.server_address[1]
        u = rpc.rpc_call_udp("127.0.0.1", port, rpc.PMAP_PROG, rpc.PMAP_VERS, 3,
                             rpc.Packer().uint(0x0607AF).uint(1).uint(6).uint(0).bytes())
        assert u.uint() == 9010
        u = rpc.rpc_call_udp("127.0.0.1", port, rpc.PMAP_PROG, rpc.PMAP_VERS, 3,
                             rpc.Packer().uint(0x0607AF).uint(1).uint(17).uint(0).bytes())
        assert u.uint() == 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_inst0_identifies_the_bridge(bridge, fake):
    with client(bridge, "inst0") as c:
        idn = c.query("*IDN?").strip()
        assert idn.startswith("enet100-bridge,GPIB-ENET/100 VXI-11 bridge (gpib0),")
        assert len(idn.split(",")) == 4
        assert c.readstb() == 0
        c.clear()
        with pytest.raises(Vxi11Error) as ei:
            c.read()                       # nothing queued
        assert ei.value.code == IO_TIMEOUT
    # inst0 never touches the box
    assert not [k for k, _ in fake.log if k.startswith("dev")]


def test_portmapper_callit_answers_only_for_our_programs():
    pm = rpc.Portmapper()
    pm.set(0x0607AF, 1, 6, 9010)
    d = rpc.Dispatcher()
    d.add(rpc.PMAP_PROG, rpc.PMAP_VERS, pm.handler)
    srv = rpc.serve_udp(d, "127.0.0.1", 0)
    port = srv.server_address[1]
    try:
        u = rpc.rpc_call_udp("127.0.0.1", port, rpc.PMAP_PROG, rpc.PMAP_VERS, 5,
                             rpc.Packer().uint(0x0607AF).uint(1).uint(0).opaque(b"").bytes())
        assert u.uint() == 9010 and u.opaque() == b""
        with pytest.raises(OSError):        # unknown program: silence, so a timeout
            rpc.rpc_call_udp("127.0.0.1", port, rpc.PMAP_PROG, rpc.PMAP_VERS, 5,
                             rpc.Packer().uint(0x123456).uint(1).uint(0).opaque(b"").bytes(),
                             timeout=0.5)
    finally:
        srv.shutdown()
        srv.server_close()
