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
| 2B | FindLstn | `00 00 <listbytes> 00 <limit>`, then a u16 address list | ibcnt = number found |
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

## Changing network settings (not decoded yet)

While adding a box, NI MAX sent the box's own record back to it, opcode
`08 01`, by UDP to :44515 and over TCP :44516 (`00 00 00 b8` + record). Every
record MAX sent was byte-for-byte what the box had just reported, so nothing
changed. That is probably the settings write, but it hasn't been seen with a
changed value. We don't know whether the box applies it, needs a reboot, checks
some fields, or wants a password. Don't send modified records: a wrong address
or netmask could take the box off the network.

To decode it, capture a real change on a PC with NI-488.2 17.6 (the last
version that supports the ENET/100):

1. Start Wireshark on the interface that reaches the box with the capture
   filter `udp port 44515 or tcp port 44516 or host <box ip>`.
2. In NI MAX, open the box under *Devices and Interfaces* and change **one**
   harmless setting, ideally the host name (e.g. add `-2`), then apply it.
   If MAX offers no way to edit it, try NI's older *GPIB-ENET/100
   Configuration Utility*. NI-488.2 17.6's *Ethernet Device Configuration*
   only uses mDNS and can't see an ENET/100.
3. Note whether the box rebooted (it drops off the network for ~15 s).
4. Change the setting back the same way, still capturing.
5. Run `enet100-bridge discover` and save its output to confirm the result.

Two writes that differ only in the host name show which bytes carry it and
whether anything else, such as a checksum or a commit flag, changes with it.

## Bus scans

`ibln` (0x0F) on the board session fails with `8100` / iberr 1 (ECIC) until
the box is controller-in-charge **(live)**. Sending IFC (0x1C) first makes it
CIC, which is what NI's FindLstn does too.
