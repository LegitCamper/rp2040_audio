#!/usr/bin/env python3
"""Check normal-runner RX markers, clean negative controls and stock reset."""
import argparse
from pathlib import Path
import re
import runpy

READY = "ready channel=0 mhz=2404 interval-ms=250 window-ms=20000"
PACKET = bytes.fromhex("0542201e0000001a00") + bytes(range(26))


def check(capture, stock, silent=False, bad_crc=False, peer=None, stream=False):
    assert not (silent and bad_crc)
    negative = silent or bad_crc
    assert "panicked" not in capture, "firmware panic"
    lines = capture.splitlines()
    native = [(index, line.split("EMBASSY_NATIVE ", 1)[1]) for index, line in enumerate(lines)
              if "EMBASSY_NATIVE " in line]
    assert len(native) == 2 and native[0][1] == "begin", "missing/duplicate/failed startup"
    allocation = re.fullmatch(r"allocation=([0-9a-fA-F]{8})", native[1][1])
    assert allocation and 0x209BAC <= int(allocation[1], 16) <= 0x21DC00
    assert int(allocation[1], 16) % 4 == 0, "unaligned allocation"
    rx = [(index, line.split("EMBASSY_RX ", 1)[1]) for index, line in enumerate(lines)
          if "EMBASSY_RX " in line]
    count = 0 if negative else 500 if stream else 1
    assert len(rx) == count + 2 + int(stream), "missing/duplicate/unexpected RX record"
    ready = READY.replace("interval-ms=250", "interval-ms=0") + " mode=stream" if stream else READY
    assert native[0][0] < native[1][0] < rx[0][0] and rx[0][1] == ready
    if stream:
        assert rx[-2][1] == f"window-ended count={count}", "ISO delivery did not finish before cleanup commands"
    for index, (_, line) in enumerate(rx[1:-2] if stream else rx[1:-1]):
        match = re.fullmatch(r"packet=\[((?:[0-9a-fA-F]{2},\s*){34}[0-9a-fA-F]{2})\] count=([0-9]+)", line)
        assert match and int(match[2]) == index + 1, "wrong packet count or encoding"
        expected = bytearray(PACKET)
        if stream:
            expected[5:7] = index.to_bytes(2, "little")
            expected[9:11] = index.to_bytes(2, "little")
        assert bytes(int(value.strip(), 16) for value in match[1].split(",")) == expected, "wrong H4/ISO envelope, sequence or marker"
    done = (f"done count={count} commands-ok=true stopped=true stock-version=true quiet=true "
            f"pass={'false' if negative else 'true'} restart_required=true")
    assert rx[-1][1] == done, "RX exchange/stop/version/late-packet check failed"
    assert stock.count("RESULT: controller ISO support absent") == 2, "missing stock capability checks"
    assert len(re.findall(r"LMP subversion\s+0x2310", stock)) == 2, "stock version not restored"
    if bad_crc or (stream and not silent):
        assert peer is not None, "CRC/stream control requires independent transmitter log"
    if peer is not None:
        assert not silent, "silent control must not include transmitter evidence"
        checker = Path(__file__).resolve().parents[1] / "cyw43_iso/hardware/nrf54-dtm-rx/check-marker-tx.py"
        kwargs = {"counter": True} if stream else {}
        runpy.run_path(str(checker))["check"](peer, 0, bis=True, length=26, bad_crc=bad_crc, **kwargs)


def self_check():
    stock = "LMP subversion 0x2310\nRESULT: controller ISO support absent\n" * 2
    packet = "EMBASSY_RX packet=[" + ", ".join(f"{value:02x}" for value in PACKET) + "] count=1\n"
    capture = ("EMBASSY_NATIVE begin\nEMBASSY_NATIVE allocation=00217e78\nEMBASSY_RX " + READY + "\n"
               + packet + "EMBASSY_RX done count=1 commands-ok=true stopped=true stock-version=true quiet=true pass=true restart_required=true\n")
    negative = capture.replace(packet, "").replace("done count=1", "done count=0").replace("pass=true", "pass=false")
    peer = ("DTM_TX ready mhz=2404 mode=BLE1M aa=718ebed6 crc_init=123400 whitening=true header=1a00 payload=["
            + ", ".join(f"{value:02x}" for value in range(26)) + "]\nDTM_TX done sent=500 disabled=true\n")
    check(capture, stock)
    check(negative, stock, silent=True)
    check(negative, stock, bad_crc=True, peer=peer)
    invalid = [(capture + packet, stock, False, None), (negative, stock, False, None),
               (capture, stock, True, None), (negative, stock, True, peer.replace("123400", "123401")),
               (negative, stock, True, peer.replace("sent=500", "sent=499"))]
    invalid += [(capture.replace(old, new), stock, False, None) for old, new in (
        ("allocation=00217e78", "allocation=0021de00"), ("0a, 0b", "0a, ff"),
        ("count=1", "count=2"), ("commands-ok=true", "commands-ok=false"),
        ("stopped=true", "stopped=false"), ("stock-version=true", "stock-version=false"),
        ("quiet=true", "quiet=false"), ("restart_required=true", "restart_required=false"))]
    invalid += [(capture.replace(line + "\n", "", 1), stock, False, None) for line in capture.splitlines()]
    invalid += [(negative.replace("commands-ok=true", "commands-ok=false"), stock, True, peer),
                (capture, stock.replace("0x2310", "0x2311"), False, None)]
    for text, restored, bad_crc, transmitter in invalid:
        try:
            check(text, restored, bad_crc=bad_crc, peer=transmitter)
        except AssertionError:
            continue
        raise AssertionError("accepted invalid RX capture")


def stream_self_check():
    stock = "LMP subversion 0x2310\nRESULT: controller ISO support absent\n" * 2
    ready = READY.replace("interval-ms=250", "interval-ms=0") + " mode=stream"
    begin = "EMBASSY_NATIVE begin\nEMBASSY_NATIVE allocation=00217e78\nEMBASSY_RX " + ready + "\n"
    records = []
    for index in range(500):
        packet = bytearray(PACKET)
        packet[5:7] = packet[9:11] = index.to_bytes(2, "little")
        records.append("EMBASSY_RX packet=[" + ", ".join(f"{byte:02x}" for byte in packet)
                       + f"] count={index + 1}\n")
    done = "EMBASSY_RX done count=500 commands-ok=true stopped=true stock-version=true quiet=true pass=true restart_required=true\n"
    payload = bytes(2) + bytes(range(2, 26))
    peer = ("DTM_TX ready mhz=2404 mode=BLE1M aa=718ebed6 crc_init=123401 whitening=true header=1a00 payload=["
            + ", ".join(f"{byte:02x}" for byte in payload) + "]\n"
            + "DTM_TX counter first=0 count=500 payload-offset=0 width=2 endian=little\n"
            + "DTM_TX done sent=500 disabled=true\n")
    capture = begin + "".join(records) + "EMBASSY_RX window-ended count=500\n" + done
    check(capture, stock, peer=peer, stream=True)
    negative = begin + "EMBASSY_RX window-ended count=0\n" + done.replace("count=500", "count=0").replace("pass=true", "pass=false")
    check(negative, stock, silent=True, stream=True)
    check(negative, stock, bad_crc=True, peer=peer.replace("123401", "123400"), stream=True)
    mutations = [
        capture.replace(records[0], ""),
        capture.replace(records[257], records[256].replace("count=257", "count=258")),
        capture.replace("interval-ms=0", "interval-ms=250"),
        capture.replace("count=500 commands-ok", "count=499 commands-ok"),
        capture.replace(records[0], records[0].replace("00, 00, 02", "00, 01, 02")),
        capture.replace("window-ended count=500", "window-ended count=499"),
        capture.replace(records[-1] + "EMBASSY_RX window-ended count=500\n",
                        "EMBASSY_RX window-ended count=500\n" + records[-1]),
        capture + records[0],
    ]
    for invalid in mutations:
        try:
            check(invalid, stock, peer=peer, stream=True)
        except AssertionError:
            continue
        raise AssertionError("accepted invalid stream capture")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", nargs="?", type=Path)
    parser.add_argument("stock", nargs="?", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--silent", action="store_true")
    modes.add_argument("--bad-crc", action="store_true")
    parser.add_argument("--peer", type=Path)
    parser.add_argument("--stream", action="store_true", help="500 unique SDUs with autonomous controller task wake")
    args = parser.parse_args()
    self_check()
    if args.stream:
        stream_self_check()
    if (args.capture is None) != (args.stock is None):
        parser.error("capture and stock paths must be supplied together")
    if args.bad_crc and (args.capture is None or args.peer is None):
        parser.error("--bad-crc requires capture, stock and --peer")
    if args.capture is not None:
        check(args.capture.read_text(), args.stock.read_text(), args.silent, args.bad_crc,
              args.peer.read_text() if args.peer else None, args.stream)
        print("Embassy RX passes: clean no-delivery control and external stock reset."
              if args.silent or args.bad_crc else
              f"Embassy RX passes: {500 if args.stream else 1} exact ISO SDUs through normal Runner and external stock reset.")
    else:
        print("Embassy RX capture checker self-check passes.")
