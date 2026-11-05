"""The ENET/1000 personality, driven the way NI-488.2 25.8 drives a GPIB-ENET/1000
(board open from a capture of NI-488.2 25.8; device open from BLISS's ENET/1000 path),
against the simulated ENET/100."""

import socket
import struct
import time
import urllib.request

import pytest

from enet100_bridge.box import BoxConfig
from enet100_bridge.server import Bridge, BridgeConfig

from fake_enet import FakeEnet

PORTS = {"cmd": 0, "aux": 0, "keep": 0, "wait": 0, "http": 0}


class NiConn:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)

    def recvn(self, n):
        b = b""
        while len(b) < n:
            d = self.s.recv(n - len(b))
            assert d, "connection closed"
            b += d
        return b

    def frag(self):
        flags, n = struct.unpack("!HH", self.recvn(4))
        return flags, self.recvn(n)

    def cmd(self, hexstr, data=b""):
        self.s.sendall(bytes.fromhex(hexstr).ljust(12, b"\0") + data)
        flags, p = self.frag()
        assert flags == 0
        sta, err, cnt = struct.unpack_from("!HH4xI", p)
        return sta, err, cnt, p[12:]

    def close(self):
        self.s.shutdown(socket.SHUT_WR)
        try:
            self.s.settimeout(2)
            while self.s.recv(100):
                pass
        except OSError:
            pass
        self.s.close()


class Ni:
    """A minimal NI-488.2 (ENET/1000 protocol) client."""

    def __init__(self, ports):
        self.p = ports
        self.conns = []

    def _c(self, key):
        c = NiConn(self.p[key])
        self.conns.append(c)
        return c

    def open_board(self):
        b = self._c("cmd")
        sta, err, cnt, extra = b.cmd("0b")
        assert b"GPIB-ENET/1000" in extra
        self.ipport = b.cmd("6306")[3]
        cid = b.cmd("64")[3]
        b.cmd("65000000" + cid.hex())
        for h in ("5005", "5025", "07001801000000000d", "501001"):
            b.cmd(h)
        w = self._c("wait")
        bind = "5502" + "00000000" + self.ipport[4:].hex() + self.ipport[:4].hex()
        assert w.cmd(bind)[1] == 0xFFFF
        w.cmd("65000000" + cid.hex())
        a = self._c("aux")
        a.cmd("6306"); a.cmd("65000000" + cid.hex())
        k = self._c("keep")
        k.cmd("65000000" + cid.hex()); k.cmd("4f220000" + self.ipport.hex())
        a.cmd("501001"); a.cmd("5501" + "00000000" + self.ipport[4:].hex() + self.ipport[:4].hex())
        self.board, self.board_wait, self.cid = b, w, cid
        return b

    def open_device(self, pad, tmo=13):
        d = self._c("cmd")
        ipport = d.cmd("6306")[3]
        d.cmd("65000000" + self.cid.hex())
        d.cmd(f"070200 05 {pad:02x} 00 0a 00 {tmo:02x} 00 04".replace(" ", ""))
        d.cmd("5005"); d.cmd("501001"); d.cmd("50150b")
        w = self._c("wait")
        w.cmd("55020002" + "0000" + ipport[4:].hex() + ipport[:4].hex())
        w.cmd("65000000" + self.cid.hex())
        d.cmd("5801 01".replace(" ", ""))
        return d, w

    def read(self, d, n=200):
        d.s.sendall(struct.pack("!B3xI4x", 0x16, n))
        data = b""
        while True:
            flags, p = d.frag()
            if flags:
                break
            data += p
        flags, p = d.frag()
        return data, struct.unpack_from("!HH4xI", p)

    def findlstn(self, b, addrs):
        lst = b"".join(struct.pack("!H", a) for a in addrs)
        b.s.sendall(bytes([0x2B, 0, 0, len(lst), 0, 31]).ljust(12, b"\0"))
        flags, p = b.frag()
        b.s.sendall(lst)
        flags, p = b.frag()
        sta, err, cnt = struct.unpack_from("!HH4xI", p)
        return sta, err, cnt, p[12:]

    def close_all(self):
        for c in reversed(self.conns):
            c.close()


@pytest.fixture
def fake():
    f = FakeEnet()
    yield f
    f.close()


@pytest.fixture
def bridge(fake):
    cfg = BridgeConfig(listen="127.0.0.1", core_port=0, abort_port=0, portmapper="off",
                       admin_port=0, mdns=False)
    cfg.boxes.append(BoxConfig(name="gpib0", host="127.0.0.1", cmd_port=fake.cmd_port,
                               wait_port=fake.wait_port, use_discovery=False,
                               boot_settle_s=0, retry_interval_s=0, connect_on_start=False))
    b = Bridge(cfg)
    b.ni_ports = PORTS
    b.start()
    yield b
    b.stop()


def test_board_session_and_settings(bridge, fake):
    ni = Ni(bridge.ni["gpib0"].ports)
    b = ni.open_board()
    sta, err, cnt, _ = b.cmd("4e11")                 # ibask TIMING: forwarded to the box
    assert err == 2
    sta, err, cnt, _ = b.cmd("061103")               # ibconfig TIMING 350 ns
    assert err == 2 and fake.board_opts[0x11] == 3
    serial = struct.unpack("<I", b.cmd("59000006")[3])[0]
    assert serial == 0x123456                          # from the box's MAC: no discovery here
    b.cmd("12")
    ni.close_all()
    time.sleep(0.3)
    # NI's board close leaves the bridge's own ENET/100 session alone.
    assert bridge.boxes["gpib0"].enet.alive
    assert fake.violations == []


def test_device_write_read_poll(bridge, fake):
    ni = Ni(bridge.ni["gpib0"].ports)
    ni.open_board()
    d, w = ni.open_device(4)
    sta, err, cnt, _ = d.cmd("620000000000000400000000", b"ID?\n")
    assert cnt == 4 and not sta & 0x8000
    data, (sta, err, cnt) = ni.read(d)
    assert data == b"HP3457A\r\n" and cnt == len(data)
    d.cmd("620000000000000400000000", b"SRQ\n")
    sta, *_ = w.cmd("22004800")                      # ibwait RQS|TIMO on :5015
    assert sta & 0x0800
    sta, err, cnt, extra = d.cmd("19")               # ibrsp
    assert extra[0] == 0x48
    d.cmd("580001")                                  # offline
    ni.close_all()
    time.sleep(0.3)
    assert "dev4" in fake.closed_cleanly
    assert fake.violations == []


def test_findlstn(bridge, fake):
    ni = Ni(bridge.ni["gpib0"].ports)
    b = ni.open_board()
    sta, err, cnt, extra = ni.findlstn(b, range(1, 31))
    assert sta & 0x8000 and err == 1                 # ECIC: not controller yet
    b.cmd("1c")                                      # SendIFC
    sta, err, cnt, extra = ni.findlstn(b, range(1, 31))
    assert cnt == 1 and extra == bytes.fromhex("00020004")
    ni.close_all()


def test_half_open_ni_session_is_harmless(bridge, fake):
    ports = bridge.ni["gpib0"].ports
    for _ in range(5):                                # what NI did when it disliked the reply
        c = NiConn(ports["cmd"])
        c.cmd("0b")
        c.s.close()
    c = NiConn(ports["cmd"])                          # a device that never finishes setup
    c.cmd("6306")
    c.s.close()
    time.sleep(0.3)
    assert fake.violations == [] and not fake.wedged
    ni = Ni(ports)
    ni.open_board()
    d, w = ni.open_device(4)
    d.cmd("620000000000000400000000", b"ID?\n")
    assert ni.read(d)[0] == b"HP3457A\r\n"
    ni.close_all()


def test_device_info_xml(bridge):
    port = bridge.ni["gpib0"].ports["http"]
    xml = urllib.request.urlopen(f"http://127.0.0.1:{port}/deviceinfo.xml", timeout=5).read()
    assert b"<ProdId>\n       0x753B" in xml
    assert b"00:80:2F:12:34:56" in xml or b"00:80:2F:00:00:00" in xml
    assert b"<DevClass>\n       GPIB-ENET" in xml
