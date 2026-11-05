# GPIB-ENET/100 network protocol

Decoded from packet captures of NI-488.2 17.6 and NI-VISA talking to a
GPIB-ENET/100 with firmware REV C.9, then checked with this project's own
client. Everything is big-endian. Addresses below are examples.

Marked **(live)**: confirmed by enet100-bridge against a real box, beyond what
NI's traffic showed. Marked **?**: not confirmed.

## Ports

| port | use |
|------|-----|
| TCP 5000 | commands: one board session per client process, one connection per device handle |
| TCP 5015 | "wait" channel: one per board session and one per device handle (ibwait) |
| UDP 44515 | discovery |
| TCP 44516 | the discovery record, with a u32 length prefix |

## Framing

**Client → box**: a 12-byte block, `id:u8` + 11 argument bytes, zero padded.
Data-carrying commands (0x61 ibcmd, 0x62 ibwrt) are
`id 00 00 00 <len:u32> 00 00 00 00` followed by `len` data bytes. ibrd (0x16)
puts its maximum length in the same place.

**Box → client**: fragments `flags:u16 len:u16 payload[len]`.

- Status fragment (flags 0): `ibsta:u16 iberr:u16 00 00 00 00 ibcnt:u32`
  followed by `len - 12` bytes of command-specific data. The extra bytes
  usually arrive in a separate TCP segment, so read by length.
- `iberr` only means an error when ibsta has ERR (0x8000). Otherwise it holds
  something else: the previous value for ibconfig, the answer for ibask, `ffff`
  for setup commands.

## Board session

```
B = TCP :5000
B: 0B                         identify
   <- status, extra = "GPIB-ENET  REV C.9\n" "National Instruments Corp.        \n"
      MAC(6) box IP(4) client IP(4) client port(2) 01 00...
B: 50 05                      <- iberr ffff
B: 07 00 18 01 00 00 00 00 0D board configuration
B: 50 10 01                   <- iberr ffff
S = TCP :5015
S: 55 02 00 00 00 00 <client port:u16> <client ip:4>   bind S to B   <- iberr ffff
B: 12 01                      ibonl 1
B: 06 1F 00                   ibconfig 0x1F (high-speed cable length) = 0
```

The client id is the client's IP address plus the source port of B, as the box
sees them (echoed in the 0x0B reply).

**Closing:** a session that has done everything above can be closed with a FIN
on each connection. The box FINs back and accepts a new session straight away
**(live)**. A connection closed after only 0x0B leaves the box ignoring every
client until it is power-cycled. The box also needs ~45–60 s after power-on
before it answers. Connecting earlier has the same effect.

## Device handle (ibdev)

```
D = TCP :5000
D: 07 02 00 <mode> <pad> <sad> <eos> 00 <tmo> 00 04 00
D: 50 05
D: 50 10 01
D: 50 15 0B
S2 = TCP :5015
S2: 55 02 00 02 00 00 <D's source port:u16> <client ip:4>
D: 58 01 01                   online
...
D: 58 00 01                   offline, then FIN S2 and D
```

- `mode` = 0x01 (EOT: assert EOI on the last byte written) | the NI EOS mode
  bits: 0x04 REOS (end reads on the EOS char), 0x08 XEOS, 0x10 BIN (8-bit EOS
  compare). In an NI-VISA capture with BIN set, the box dropped an instrument's
  trailing LF. The bridge leaves BIN off.
- `sad` = 0 for none, else 0x60 + secondary address.
- `tmo` = NI timeout code: 1 = 10 µs, 2 = 30 µs, … 11 = 1 s, 12 = 3 s,
  13 = 10 s, 14 = 30 s, … 17 = 1000 s.

## Commands

| id | call | arguments | reply extra |
|----|------|-----------|-------------|
| 04 | ibclr (SDC) | – | – |
| 06 | ibconfig | opt:u8 val:u8 | previous value in iberr |
| 0B | identify | – | strings + MAC/IP/client id |
| 0D | iblines | – | u16 line status (NI layout: low byte valid mask, high byte state) |
| 0F | ibln | pad:u8 sad:u8 | u16, nonzero if a listener answered |
| 10 | ibloc (GTL) | – | – |
| 12 | ibonl (board) | val:u8 | – |
| 16 | ibrd | maxlen:u32 at bytes 4..7 | see below |
| 19 | ibrsp (serial poll) | – | u8 status byte |
| 1C | ibsic (IFC) | – | – |
| 20 | ibtrg (GET) | – | – |
| 22 | ibwait (on a :5015 channel) | `00 <mask:u16>` | – |
| 2B | FindLstn | `00 00 <listbytes> 00 <limit>`; **two steps**: the header is answered, then the u16 address list is sent and answered | ibcnt = number found; extra = u16 byte count + the addresses found |
| 4E | ibask | opt:u8 | value in iberr |
| 50 | session setup | `05` / `10 01` / `15 0B` | iberr ffff |
| 55 | bind a :5015 channel | see above | iberr ffff |
| 58 | device online/offline | `01 01` / `00 01` | – |
| 61 | ibcmd (bytes with ATN) | like 0x62 | ibcnt = bytes sent |
| 62 | ibwrt | len:u32 at bytes 4..7, then data | ibcnt = bytes sent |

ibconfig options seen: 0x03 timeout, 0x06 READDR, 0x0B REN (board), 0x0C EOS
read enable **(live)**, 0x0F EOS char, 0x1A, 0x1F. The numbering is NI-488.2's
`IbcXXX`. 0x04 (EOT) is used by the bridge for writes without END **?**.

### ibrd

```
-> 16 00 00 00 <maxlen:u32> 00 00 00 00
<- status (ibsta 0100)
<- flags=0000 len=n  data      (zero or more; one fragment may span TCP segments)
<- flags=0001 len=0            terminator
<- status: final ibsta/iberr/ibcnt
```

- END (0x2000) is set if the read ended on EOI or the EOS char.
- A timeout gives `c100` ERR|TIMO|CMPL, iberr 6 (EABO), with ibcnt = bytes
  received.
- A read from an address with no instrument follows the same sequence and
  ends in the same timeout **(live)**. An ibwrt to such an address fails at once
  with `8100`, iberr 2 (ENOL).

### ibwait / SRQ

Waits go on the device's own :5015 channel, not on :5000:

```
S2: 22 00 48 00        wait for RQS | TIMO
S2: <- status 0900 (RQS) once the instrument requests service,
       or 4100 (TIMO) after the device's timeout   (live)
D:  19                 ibrsp -> status byte, clears RQS
```

Nothing arrives on :5015 unless a wait was asked for.

## Discovery

UDP to `255.255.255.255:44515` (or the box's address), 184 bytes:

```
45 44 01 00 00 02 00 ... 00 4E 49          "ED" query ... "NI"
```

Each box answers with a broadcast from :44515 to the query's source port. The
ENET/100 sends its answer 5 times, 1.9 s apart (0, 1.9, 3.8, 5.8, 7.7 s)
**(live)**. For about 10 s after a query, it **ignores any new query**, from
any host **(live)**. A client must listen for longer than that, or send its
query again after ~10.5 s. Because the answer is a broadcast, a host firewall
that blocks inbound UDP for the client program drops it silently. The 184-byte
answer:

| offset | content |
|--------|---------|
| 00 | "ED" |
| 02 | `08 01` (reply) |
| 04 | 4 bytes, unknown (`4E 02 C9 30` seen) |
| 08 | firmware revision, 2 chars ("C9") |
| 0A | u32, probably the serial number |
| 0E | MAC address |
| 1C | model, 32 bytes NUL padded ("GPIB-ENET/100") |
| 3C | host name, NUL padded |
| 9C | u16, unknown: `02 00` in one capture and `00 00` later, both with the box on DHCP |
| 9E | IP address |
| A2 | netmask |
| A6 | 4 bytes, zero |
| AA | 4 bytes: the LAN's DNS server here (? gateway or DNS) |
| B6 | "NI" |

## Network settings

The box takes its IP address, netmask and DNS server from DHCP. It also seems
to take its host name from the DHCP reply: it reported the name in its DHCP
reservation, not the different one it sent as option 12 in its own request.
Neither NI MAX nor NI-488.2 17.6's tools could change these settings.
To fix the box's address or name, use a DHCP reservation for its MAC address.

While adding a box, NI MAX sent the box's own record back to it, opcode
`08 01`, by UDP to :44515 and over TCP :44516 (`00 00 00 b8` + record). Each
record was byte-for-byte what the box had just reported, and nothing changed.
What the box does with a modified record is unknown, so don't send one.

## GPIB (board) settings

The settings NI MAX offers for the box are board options, read with ibask
(0x4E) and set with ibconfig (0x06) on the board session **(live)**. They
belong to the session: REN and bus timing set in one session were back at the
defaults in the next **(live)**. So a client sets what it wants each time it
opens a board session. NI's driver presumably does the same with the values
MAX stores on the PC.

| option | setting | values | value in a new session |
|--------|---------|--------|-----------------------------|
| 0x01 | primary address | 0–30 | 0 |
| 0x02 | secondary address | 0 = none, 0x60–0x7E | 0 |
| 0x03 | board timeout | NI timeout code | 13 (10 s) |
| 0x07 | automatic serial polling | 0/1 | 1 |
| 0x08 | CIC protocol | 0/1 | 0 |
| 0x0A | system controller | 0/1 | 1 |
| 0x0B | assert REN when SC | 0/1 | 0 |
| 0x10 | parallel poll config | | 0 |
| 0x11 | bus timing (T1) | 1 = 2 µs, 2 = 500 ns, 3 = 350 ns | 2 |
| 0x17 | send LLO | | 0 |
| 0x19 | parallel poll time | | 0 |
| 0x1A | END bit is normal | | 1 |
| 0x1F | HS488 cable length | 0 = off, 1–15 m | 0 |
| 0x20 | ist | | 0 |
| 0x21 | rsv | | 0 |

0x18 (serial poll time) and 0x1B (unaddressing) answer EARG. Changing the bus
timing (2→1→2) and the primary address (0→1→0) on a live box worked, and the
instrument kept answering throughout. ibconfig returns the previous value in
iberr.

## Bus scans

`ibln` (0x0F) on the board session fails with `8100` / iberr 1 (ECIC) until
the box is controller-in-charge **(live)**. Sending IFC (0x1C) first makes it
CIC, which is what NI's FindLstn does too.

## GPIB-ENET/1000 (what enet100-bridge imitates for NI-488.2)

From captures of NI-488.2 25.8 and NI MAX with a GPIB-ENET/1000 (firmware
1.2.1f0), and from BLISS's ENET/1000 code. The command set, framing and status
fragments are the ENET/100's. The differences:

| port | use |
|------|-----|
| TCP 5000, 5015 | as on the ENET/100 |
| TCP 5003, 5005 | side channels, set up per session (below). Nothing seen on them besides the setup and NI's 0x4F polls |
| HTTP 80 | `/deviceinfo.xml`: NI MAX reads only this when adding a box |

**Board session** (NI-488.2 25.8, `ibfind gpibN`):

```
B = :5000  0B                    <- status, ibcnt 0809D523, extra 90 bytes:
                                    "GPIB-ENET/1000" NUL-padded, 04 08 07 at offset 50
B: 63 06                         <- extra = client IP(4) + client port(2), as the box sees them
B: 64                            <- extra = u32 session id
B: 65 00 00 00 <id:4>            (sent on every connection of the session)
B: 50 05, 50 25, 07 00 18 01 00 00 00 00 0D, 50 10 01
W = :5015  55 02 00 00 00 00 <port:u16> <ip:4>, 65 <id>
A = :5003  63 06, 65 <id>
K = :5005  65 <id>, 4F 22 00 00 <ip:4> <port:u16>   (NI repeats the 4F every second or so)
A: 50 10 01, 55 01 00 00 00 00 <port> <ip>
B: 59 00 00 06                   <- extra = serial number, u32 little-endian
...
B: 12 00                         ibonl 0, then FIN on every connection
```

A device handle is the same with `07 02 ...` instead of `0B`, and
`55 02 00 02 00 00 <port> <ip>` on its :5015 channel. It has no 0x0B.

**ibrd**: no status before the data. Data fragments, the `flags=1` terminator,
then the final status.

**What NI-488.2 checks.** If the 0x0B reply doesn't name `GPIB-ENET/1000`,
the driver hangs up straight after it and `ibfind` fails with EDVR. NI MAX
picks the board type from `<ProdId>` in deviceinfo.xml (0x753B = GPIB-ENET/1000,
BoardType 0xD9) and ignores `<ProdName>`. It shows `<SerialNo>` and
`<NormalVersion>`. The box also advertises `_ni._tcp` and `_http._tcp` on port 80
over mDNS, with TXT `MAC, DevClass=GPIB-ENET, ProdId=0x753B, ProdName, SerialNo,
Status=Running, ImgPath, Comment`.

deviceinfo.xml (values from an ENET/1000):

```xml
<Settings>
    <DevClass>GPIB-ENET</DevClass> <IPAddr>...</IPAddr> <DHCPEnabled>true</DHCPEnabled>
    <SubnetMask>...</SubnetMask> <Gateway>...</Gateway> <DNSServer>...</DNSServer>
    <NIPort>80</NIPort> <MAC>00:80:2F:..</MAC> <Hostname>...</Hostname>
    <ProdId>0x753B</ProdId> <ProdName>GPIB-ENET/1000</ProdName> <SerialNo>1A2B3C4</SerialNo>
    <NormalVersion>1.2.1f0</NormalVersion> <SafeVersion>1.0.1f0</SafeVersion>
    <BootVersion>1.0.1f0</BootVersion> <Status>Running</Status>
    <Link1Status>1000</Link1Status> <Link1Duplex>FULL</Link1Duplex>
    <ImgPath>/img/gpib-enet.png</ImgPath> <Comment></Comment>
    <UptimeInSeconds>...</UptimeInSeconds> <ButtonPressed>false</ButtonPressed>
</Settings>
```

(Each value sits on its own line, indented, in the real file.)
