#!/usr/bin/env python3
"""Check normal-runner RX markers, clean negative controls and stock reset."""
import argparse
from pathlib import Path
import re
import runpy

CHECKER = Path(__file__).resolve().parents[1] / "cyw43_iso/hardware/nrf54-dtm-rx/check-marker-tx.py"
csa2_first = runpy.run_path(str(CHECKER))["csa2_first"]

READY = "ready channel=0 mhz=2404 interval-ms=250 window-ms=20000"
HOP_READY = ("ready channel=csa2 channel-id=0xcf58 event-counter=0 interval-ms=0 "
             "window-ms=20000 mode=hop")
TIMER_HOP_READY = ("ready channel=csa2 channel-id=0xcf58 event-counter=0 interval-ms=20 "
                   "window-ms=20000 mode=timer-hop "
                   "preacquire=scan-per-countdown-callback")
PACKET = bytes.fromhex("0542201e0000001a00") + bytes(range(26))


def longest_run(indices):
    """Longest run of consecutive transmitter event indices."""
    best = run = 0
    previous = None
    for index in indices:
        run = run + 1 if previous is not None and index == previous + 1 else 1
        previous = index
        best = max(best, run)
    return best

def check(capture, stock, silent=False, bad_crc=False, peer=None, stream=False, hop=None,
          min_run=None, timer_hop=False):
    """Returns (delivered, longest consecutive run) for hopping captures."""
    assert not (silent and bad_crc)
    assert stream or hop is None, "hop captures are stream captures"
    assert not timer_hop or hop is not None, "timer-hop captures are hopping captures"
    # An open-loop counter that only advances inside the receive callback
    # stalls on a dead channel after any miss, so an offset transmitter still
    # scores chance coincidences. Alignment is therefore judged by the longest
    # run of consecutive event indices, not by the delivered total.
    negative = silent or bad_crc
    if hop:
        assert min_run is None, "the offset control has no aligned run to require"
    assert "panicked" not in capture, "firmware panic"
    lines = capture.splitlines()
    native = [(index, line.split("EMBASSY_NATIVE ", 1)[1]) for index, line in enumerate(lines)
              if "EMBASSY_NATIVE " in line]
    assert len(native) == 2 and native[0][1] == "begin", "missing/duplicate/failed startup"
    allocation = re.fullmatch(r"allocation=([0-9a-fA-F]{8})", native[1][1])
    assert allocation and 0x209BAC <= int(allocation[1], 16) <= 0x21DC00
    assert int(allocation[1], 16) % 4 == 0, "unaligned allocation"
    traces = [(index, line.split("EMBASSY_RX ", 1)[1]) for index, line in enumerate(lines)
              if "EMBASSY_RX timer-trace " in line]
    rx = [(index, line.split("EMBASSY_RX ", 1)[1]) for index, line in enumerate(lines)
          if "EMBASSY_RX " in line and "EMBASSY_RX timer-trace " not in line]
    # Hopping tolerates RF loss, so its delivered total comes from the log and
    # alignment is judged by consecutive runs instead of a fixed count.
    count = None if hop is not None else 0 if negative else 500 if stream else 1
    if count is not None:
        assert len(rx) == count + 2 + int(stream), "missing/duplicate/unexpected RX record"
    timer_trace = None
    if timer_hop:
        ready = TIMER_HOP_READY
    elif hop is not None:
        ready = HOP_READY
    elif stream:
        ready = READY.replace("interval-ms=250", "interval-ms=0") + " mode=stream"
    else:
        ready = READY
    assert native[0][0] < native[1][0] < rx[0][0] and rx[0][1] == ready
    if timer_hop:
        assert len(traces) == 1 and rx[-1][0] < traces[0][0], "missing or early timer trace"
        trace = re.fullmatch(
            r"timer-trace event-counter=([0-9]+) channel=([0-9]+) configurations=([0-9]+) "
            r"countdown=([0-9]+) callbacks=([0-9]+) advances=([0-9]+) marker-acquired=([01]) "
            r"scan-retunes=([0-9]+)",
            traces[0][1])
        assert trace, "malformed timer trace"
        event_counter, channel, configurations, countdown, callbacks, advances, marker, scans = map(int, trace.groups())
        assert 1 <= countdown <= 32 and scans <= callbacks
        assert configurations == 1 + scans + marker + advances, "timer configuration count mismatch"
        assert callbacks == scans + 32 * advances + (32 - countdown), "timer callback accounting mismatch"
        if not marker:
            assert advances == 0 and countdown == 32, "unsynchronized timer advanced"
            assert event_counter == scans & 0xffff, "scan event counter mismatch"
        if negative:
            assert marker == 0, "negative control acquired a marker"
        elif hop == 0:
            assert marker == 1, "aligned timer capture never acquired its marker"
        assert channel == csa2_first(event_counter), "timer channel mismatch"
        timer_trace = (event_counter, advances, marker)
    else:
        assert not traces, "unexpected timer trace"
    if hop is not None:
        ended = re.fullmatch(r"window-ended count=([0-9]+) span=([0-9]+) lost=([0-9]+)", rx[-2][1])
        assert ended, "missing hopping window summary"
        count = int(ended[1])
        assert int(ended[2]) - count == int(ended[3]), "inconsistent loss accounting"
        assert len(rx) == count + 3, "missing/duplicate/unexpected RX record"
        assert (count == 0) == negative, "control delivered or aligned run silent"
    elif stream:
        assert rx[-2][1] == f"window-ended count={count}", "ISO delivery did not finish before cleanup commands"
    indices = []
    for position, (_, line) in enumerate(rx[1:-2] if stream else rx[1:-1]):
        match = re.fullmatch(r"packet=\[((?:[0-9a-fA-F]{2},\s*){34}[0-9a-fA-F]{2})\] count=([0-9]+)"
                             + (r" index=([0-9]+)" if hop is not None else ""), line)
        assert match and int(match[2]) == position + 1, "wrong packet count or encoding"
        index = int(match[3]) if hop is not None else position
        assert index < 500 and (not indices or index > indices[-1]), "duplicate, reordered or out-of-range index"
        indices.append(index)
        expected = bytearray(PACKET)
        if stream:
            # HCI sequence counts accepted SDUs; the payload carries the
            # transmitter's event index. Loss makes them diverge.
            expected[5:7] = position.to_bytes(2, "little")
            expected[9:11] = index.to_bytes(2, "little")
        assert bytes(int(value.strip(), 16) for value in match[1].split(",")) == expected, "wrong H4/ISO envelope, sequence or marker"
    run = None
    if hop is not None:
        assert indices[-1:] == [] or int(ended[2]) == indices[-1] + 1, "span does not match the last index"
        run = longest_run(indices)
        if min_run is not None:
            assert run >= min_run, f"longest consecutive run {run} below required {min_run}"
        if hop:
            assert run <= 1, "offset transmitter produced a consecutive run"
        if timer_trace is not None and timer_trace[2]:
            assert indices, "marker acquisition without delivered packet"
            assert timer_trace[0] == (indices[0] + timer_trace[1]) & 0xffff, \
                "timer event counter does not follow acquired marker"
    done = (f"done count={count} commands-ok=true stopped=true stock-version=true quiet=true "
            f"pass={'false' if count == 0 else 'true'} restart_required=true")
    assert rx[-1][1] == done, "RX exchange/stop/version/late-packet check failed"
    assert stock.count("RESULT: controller ISO support absent") == 2, "missing stock capability checks"
    assert len(re.findall(r"LMP subversion\s+0x2310", stock)) == 2, "stock version not restored"
    if bad_crc or (stream and not silent):
        assert peer is not None, "CRC/stream control requires independent transmitter log"
    result = (count, run) if hop is not None else None
    if peer is not None:
        assert not silent, "silent control must not include transmitter evidence"
        checker = CHECKER
        kwargs = {"counter": True} if stream else {}
        if hop is not None:
            kwargs["hop"] = hop
        runpy.run_path(str(checker))["check"](peer, 0, bis=True, length=26, bad_crc=bad_crc, **kwargs)
    return result


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


def hop_self_check():
    stock = "LMP subversion 0x2310\nRESULT: controller ISO support absent\n" * 2

    def record(index, delivered):
        packet = bytearray(PACKET)
        packet[5:7] = (delivered - 1).to_bytes(2, "little")
        packet[9:11] = index.to_bytes(2, "little")
        return ("EMBASSY_RX packet=[" + ", ".join(f"{byte:02x}" for byte in packet)
                + f"] count={delivered} index={index}\n")

    def window(indices, timer=False):
        ready = TIMER_HOP_READY if timer else HOP_READY
        begin = "EMBASSY_NATIVE begin\nEMBASSY_NATIVE allocation=00217e78\nEMBASSY_RX " + ready + "\n"
        records = [record(index, position + 1) for position, index in enumerate(indices)]
        span = indices[-1] + 1 if indices else 0
        delivered = len(indices)
        done = (f"EMBASSY_RX done count={delivered} commands-ok=true stopped=true stock-version=true "
                f"quiet=true pass={'true' if delivered else 'false'} restart_required=true\n")
        trace = ""
        if timer:
            marker = int(bool(indices))
            if marker:
                trace = ("EMBASSY_RX timer-trace event-counter=1001 channel=23 configurations=1003 "
                         "countdown=16 callbacks=32048 advances=1001 marker-acquired=1 "
                         "scan-retunes=0\n")
            else:
                trace = ("EMBASSY_RX timer-trace event-counter=32030 channel=35 configurations=32031 "
                         "countdown=32 callbacks=32030 advances=0 marker-acquired=0 "
                         "scan-retunes=32030\n")
        return (begin + "".join(records)
                + f"EMBASSY_RX window-ended count={delivered} span={span} lost={span - delivered}\n"
                + done + trace, records)

    payload = bytes(2) + bytes(range(2, 26))

    def transmitter(offset):
        first = csa2_first(offset)
        return ("DTM_TX ready mhz=2404 mode=BLE1M aa=718ebed6 crc_init=123401 whitening=true header=1a00 payload=["
                + ", ".join(f"{byte:02x}" for byte in payload) + "]\n"
                + "DTM_TX counter first=0 count=500 payload-offset=0 width=2 endian=little\n"
                + f"DTM_TX hop channel-id=cf58 map=full counter-offset={offset} "
                + f"first-channel={first} first-mhz={2402 + 2 * first}\n"
                + "DTM_TX done sent=500 disabled=true\n")

    assert longest_run([]) == 0 and longest_run([5]) == 1
    assert longest_run([0, 1, 2, 44, 102]) == 3
    assert longest_run([0, 2, 4, 5, 6, 7, 20]) == 4

    complete, _ = window(list(range(500)))
    # Loss is expected on air; the span must still reach the lost index.
    lossy, lossy_records = window([index for index in range(500) if index not in (11, 300)])
    # An open-loop receiver that stalls after a miss scores scattered hits only.
    stalled, _ = window([0, 1, 2, 44, 102, 110, 161, 476])
    scattered, _ = window([9, 18, 34, 43, 101, 109, 160, 475])
    empty, _ = window([])
    check(complete, stock, peer=transmitter(0), stream=True, hop=0, min_run=500)
    check(lossy, stock, peer=transmitter(0), stream=True, hop=0, min_run=288)
    check(stalled, stock, peer=transmitter(0), stream=True, hop=0, min_run=3)
    check(scattered, stock, peer=transmitter(1), stream=True, hop=1)
    check(empty, stock, silent=True, stream=True, hop=0)
    check(empty, stock, bad_crc=True, peer=transmitter(0).replace("123401", "123400"), stream=True, hop=0)
    timer_complete, _ = window(list(range(500)), timer=True)
    check(timer_complete, stock, peer=transmitter(0), stream=True, hop=0, min_run=500,
          timer_hop=True)
    timer_empty, _ = window([], timer=True)
    check(timer_empty, stock, silent=True, stream=True, hop=0, timer_hop=True)
    invalid = [
        # Requiring a longer aligned run than the capture contains must fail.
        (stalled, transmitter(0), 0, 4),
        (scattered, transmitter(0), 0, 2),
        # A consecutive run under the offset transmitter contradicts the control.
        (complete, transmitter(1), 1, None),
        (stalled, transmitter(1), 1, None),
        (empty, transmitter(0), 0, None),
        (complete, transmitter(0).replace("map=full", "map=partial"), 0, None),
        (complete.replace("event-counter=0", "event-counter=1"), transmitter(0), 0, None),
        # Duplicate, reordered, miscounted and inconsistent logs.
        (complete + record(499, 500), transmitter(0), 0, None),
        (complete.replace(record(7, 8), record(6, 8)), transmitter(0), 0, None),
        (complete.replace(record(7, 8), record(7, 9)), transmitter(0), 0, None),
        (complete.replace("span=500 lost=0", "span=500 lost=1"), transmitter(0), 0, None),
        (lossy.replace("count=498 span=500 lost=2", "count=498 span=498 lost=0"), transmitter(0), 0, None),
        (lossy.replace(lossy_records[-1], ""), transmitter(0), 0, None),
        (complete.replace(record(0, 1), record(500, 1)), transmitter(0), 0, None),
    ]
    for text, peer, offset, run in invalid:
        try:
            check(text, stock, peer=peer, stream=True, hop=offset, min_run=run)
        except AssertionError:
            continue
        raise AssertionError("accepted invalid hop capture")
    for mismatched in (complete, lossy):
        try:
            check(mismatched, stock, peer=transmitter(0), stream=True)
        except AssertionError:
            continue
        raise AssertionError("hop capture accepted as fixed channel")
    try:
        check(complete.replace(HOP_READY, READY.replace("interval-ms=250", "interval-ms=0") + " mode=stream"),
              stock, peer=transmitter(0), stream=True, hop=0)
    except AssertionError:
        pass
    else:
        raise AssertionError("fixed-channel ready line accepted as hop")
    for invalid_trace in (
        timer_complete.replace("callbacks=32048", "callbacks=32047"),
        timer_complete.replace("event-counter=1001", "event-counter=1000"),
        timer_complete.replace("channel=23", "channel=22"),
        timer_complete.replace(" configurations=1003", " configurations=1002"),
        timer_complete.replace("countdown=16", "countdown=0"),
        timer_complete.replace("marker-acquired=1", "marker-acquired=0"),
        timer_complete.replace("scan-retunes=0", "scan-retunes=1"),
    ):
        try:
            check(invalid_trace, stock, peer=transmitter(0), stream=True, hop=0,
                  min_run=500, timer_hop=True)
        except AssertionError:
            continue
        raise AssertionError("accepted invalid timer trace")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", nargs="?", type=Path)
    parser.add_argument("stock", nargs="?", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--silent", action="store_true")
    modes.add_argument("--bad-crc", action="store_true")
    parser.add_argument("--peer", type=Path)
    parser.add_argument("--stream", action="store_true", help="500 unique SDUs with autonomous controller task wake")
    parser.add_argument("--hop", type=int, choices=(0, 1), default=None,
                        help="open-loop CSA#2 receive window; 1 is the offset negative control")
    parser.add_argument("--timer-hop", action="store_true",
                        help="expect timer-driven hopping ready line and controller cadence trace")
    parser.add_argument("--min-run", type=int, default=None,
                        help="shortest acceptable run of consecutive transmitter event indices")
    args = parser.parse_args()
    self_check()
    if args.stream:
        stream_self_check()
    if args.hop is not None:
        hop_self_check()
    if (args.capture is None) != (args.stock is None):
        parser.error("capture and stock paths must be supplied together")
    if args.hop is not None and not args.stream:
        parser.error("--hop requires --stream")
    if args.min_run is not None and args.hop is None:
        parser.error("--min-run requires --hop")
    if args.timer_hop and args.hop is None:
        parser.error("--timer-hop requires --hop")
    if args.bad_crc and (args.capture is None or args.peer is None):
        parser.error("--bad-crc requires capture, stock and --peer")
    if args.capture is not None:
        outcome = check(args.capture.read_text(), args.stock.read_text(), args.silent, args.bad_crc,
                        args.peer.read_text() if args.peer else None, args.stream, args.hop,
                        args.min_run, args.timer_hop)
        if args.silent or args.bad_crc:
            print("Embassy RX passes: clean no-delivery control and external stock reset.")
        elif args.hop == 1:
            delivered, run = outcome
            print(f"Embassy RX passes: offset transmitter produced {delivered} chance "
                  f"coincidences and no consecutive run, with external stock reset.")
        elif args.hop == 0:
            delivered, run = outcome
            mode = "timer-hopping" if args.timer_hop else "hopping"
            print(f"Embassy RX passes: {delivered} {mode} SDUs with a longest aligned run of "
                  f"{run} consecutive events, and external stock reset.")
        else:
            print(f"Embassy RX passes: {500 if args.stream else 1} exact ISO SDUs through normal "
                  "Runner and external stock reset.")
    else:
        print("Embassy RX capture checker self-check passes.")
