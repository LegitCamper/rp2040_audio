# RP2040 / RP2350 audio tests

Pico W / Pico 2 W application tests for the experimental CYW43439 ISO controller patch.
The first application transmits 1000 preencoded 26-byte SDUs through Embassy's
normal runner, with four transmit credits and deliberate 80-ms and 250-ms
refill pauses. It repeats a 20-frame fixture. No encoder or decoder is included.

Sibling repositories have separate roles:

- `../cyw43_iso`: controller blob, radio experiments, fixtures and capture checks.
- `../embassy-cyw43-iso`: native CYW43 driver, installer and PIO transport.
- This repository: RP2040/RP2350 test applications; future codecs belong here or in a
  separate backend, not in the controller blob.

Current TX blob supports one unencrypted BIS transmitter. A separate RX blob
delivers one fixed-channel diagnostic SDU; synchronized BIS receive and CIS
remain unfinished. These tests do not provide LE Audio profiles or playback.
RP2040 Pico W hardware validation passes: 1000 SDUs sent and credited through
the production runner; stock Nordic SDC discovers the broadcast and receives
950 exact SDUs, including recovery across the 250-ms refill pause. Both boards
are restored afterwards. The 80-ms pause precedes receiver acquisition, and
the repeated fixture cannot identify absolute host cycles. Evidence:
`../cyw43_iso/work/embassy-normal-runner-discovery-sdc-probe/` and
`../cyw43_iso/analysis/controller-native-iso.md`.
RP2350 support remains compile-only until tested on Pico 2 W hardware.

## One-shot RX diagnostic

```sh
cd ../cyw43_iso
python3 analysis/check-controller-rx.py --bis --iso --output-blob work/iso-blob-rx
cd ../rp2040_audio
cargo build --release --locked --features rx-diagnostic
python3 check-rx-capture.py
```

`rx-diagnostic` uses the same normal `Runner::run` and public `BtDriver` as TX.
It listens on data channel 0 (2404 MHz), LE1M, access address `0x718ebed6`,
CRC init `0x123401`, with whitening. Start the independent marker transmitter
only after `EMBASSY_RX ready`. Its 26-byte payload must be `00..19`.
The app sends version commands every 250 ms to wake the controller transport
task. The advertised 20000-ms window is the configured wait total, not a
measurement of RF receive duration. It accepts exactly one complete HCI ISO
packet on handle `0x0042`, sequence 0, then checks TestEnd, stock version and
a quiet interval. Hook removal and allocation reclamation require reset.

Live Pico W evidence: `../cyw43_iso/work/embassy-normal-rx-series/`.
The positive run receives the exact 35-byte H4 packet; silent and bad-CRC runs
receive none. All three pass stop/version/quiet checks and two external stock
capability checks after reset. The bad-CRC transmitter reports 500 sends with
only CRC init changed to `0x123400`; its independent log is required by the
checker. Frozen RX ELF SHA-256:
`ad919957749c31dd74dcae84bfdfd74854f8d6e8ab233d2a386fa8d937ac7e97`.
This demonstrates fixed-channel RX and native HCI delivery; it does not
establish BIG synchronization, channel hopping, continuous reception or playback.

```sh
python3 check-rx-capture.py rx.txt stock-restored.txt --peer peer.txt
python3 check-rx-capture.py silent.txt stock-restored.txt --silent
python3 check-rx-capture.py bad-crc.txt stock-restored.txt --bad-crc --peer peer.txt
```

Negative controls deliberately report `pass=false` with count 0; their checker
still requires successful commands, stop, version, quiet interval and stock
restoration. CRC control also requires independent transmitter evidence.
For Pico 2 W, combine `--no-default-features --features rp2350,rx-diagnostic`
with `--target thumbv8m.main-none-eabihf`; this path is compile-only.

## Repeated RX diagnostic

```sh
cd ../cyw43_iso
uv run --with unicorn python analysis/check-controller-rx.py --bis --iso --stream --emulate --output-blob work/iso-blob-rx-stream
cd ../rp2040_audio
cargo build --release --locked --features rx-stream
python3 check-rx-capture.py --stream
```

`rx-stream` selects a separate 1024-byte patch. Radio callbacks wake the stock
transport task; the application sends no HCI commands during its 20-second
receive window. One payload mailbox remains owned until HCI copying succeeds.
New packets can be dropped while it is occupied, and a full transport ring
needs a later radio callback to retry. This is not synchronized BIS reception.

Use `hardware/nrf54-dtm-rx`'s `marker-counter` transmitter after the ready log.
It sends 500 distinct 26-byte payloads: bytes 0..1 contain the little-endian
packet index 0..499; bytes 2..25 remain `02..19`. The app requires both the
payload index and HCI sequence to match, then logs `window-ended count=500`
before issuing TestEnd or version commands. Cleanup commands cannot rescue a
failed receive-window check. HCI sequences count accepted SDUs; they are not
derived from synchronized radio events.

```sh
python3 check-rx-capture.py rx.txt stock-restored.txt --stream --peer peer.txt
python3 check-rx-capture.py silent.txt stock-restored.txt --stream --silent
python3 check-rx-capture.py bad-crc.txt stock-restored.txt --stream --bad-crc --peer peer.txt
```

Live Pico W evidence: `../cyw43_iso/work/embassy-normal-rx-stream-series/`.
The counter marker run received 500 of 500 packets with matching payload
indices and HCI sequences, no duplicates and no gaps; silent and CRC-mismatch
controls received none. All three passed stop/version/quiet checks, two
external stock capability checks and Nordic RRAM/UICR restoration. Frozen
stream ELF SHA-256:
`4e52daa158e94948e6e94fb950cdd2fe7d0dd92278e48196b4cb96f44145cd43`.
Host log spacing was 19.9..20.13 ms, which reflects transmitter cadence and
logging, not a controller ISO interval. This run was lossless; the checker
would report any missing or duplicate packet, and lossless RF reception is not
guaranteed in general.

### Hopping receive window

```sh
cd ../cyw43_iso
uv run --with unicorn python analysis/check-controller-rx.py --bis --iso --stream --hop --emulate --output-blob work/iso-blob-rx-hop
cd ../rp2040_audio
cargo build --release --locked --features rx-hop
python3 check-rx-capture.py --stream --hop 0
```

`rx-hop` selects a separate 2048-byte patch with its own bootstrap, because
allocators encode their reservation size. Every matching radio callback,
including CRC failures, advances a 16-bit event counter once and retunes to the
next CSA#2 channel after the stock callback has rearmed the radio. The counter
starts at zero with no BIGInfo anchor, so this follows the sequence without
synchronising to a live BIG.

Drive it with `hardware/nrf54-dtm-rx`'s `marker-hop` transmitter, which walks
the same sequence from channel identifier `0xcf58`. `marker-hop-offset` shifts
the transmitter one event ahead onto a different channel and must deliver
nothing:

```sh
python3 check-rx-capture.py hop.txt stock-restored.txt --stream --hop 0 --peer peer.txt --min-run 3
python3 check-rx-capture.py offset.txt stock-restored.txt --stream --hop 1 --peer offset-peer.txt
```

Hopping is judged by the longest run of consecutive transmitter event indices,
not by the delivered total, because the open-loop counter only advances inside
the receive callback. One missed event therefore leaves the receiver parked on
a channel the transmitter has already left, and it only recovers when the
sequences happen to coincide again. Delivered totals alone cannot separate that
from chance: the offset control scored a comparable total with no consecutive
run at all.

Live Pico W evidence: `../cyw43_iso/work/embassy-normal-rx-hop-series/` and
`-series-v2/`. A strict build that stopped at the first gap received 11
consecutive events, 0..10, spanning nine distinct CSA#2 channels (1, 5, 7, 8,
16, 25, 28, 29, 30), which a fixed-channel receiver cannot produce. The
loss-tolerant build ran the full 20-second window and delivered 15 SDUs with a
longest aligned run of 3; the one-event-offset transmitter delivered 16 with no
run longer than 1, and the silent control delivered none. All three restored
stock and passed both capability checks.

Retuning works, but staying synchronised does not: there is no anchor, so loss
is unrecoverable. A timer-driven event counter is required before this is a
broadcast receiver.

### Timer-driven hopping diagnostic

`rx-timer-hop` uses the controller countdown callback as a persistent clock.
Before marker acquisition, each accepted callback advances one CSA#2 candidate
and retunes. A valid marker freezes that scan at the marker's event counter,
resets a 32-callback phase, and starts one synchronized event advance per phase.
This fast scan is diagnostic acquisition only; it is not BIGInfo-derived anchor
synchronization, and the callback period is static firmware evidence rather than
a controller-measured interval.

The final trace reports accepted callbacks `A`, synchronized advances `V`,
pre-acquisition scan retunes `S`, countdown `T`, marker state `M`, and channel
configuration count `Q`. The checker requires:

```text
Q = 1 + S + M + V
A = S + 32*V + (32-T)
```

Before acquisition it additionally requires `M=0`, `V=0`, `T=32`, `A=S`, and
the 16-bit event counter equal to `S`. After acquisition the counter must equal
the first delivered marker index plus `V`, modulo 65536. In both states the
reported channel must equal CSA#2 for the reported counter. Live trace reads are
not supported because callback writes can tear; the app stops the diagnostic
before reading it.

```sh
cargo build --release --locked --features rx-timer-hop
python3 check-rx-capture.py timer.txt stock-restored.txt \
  --stream --hop 0 --timer-hop --peer peer.txt --min-run 3
```

## Build

Keep the sibling repositories at the paths above. The build references existing
`cyw43_iso/work/iso-blob-continuous/audio.lc3`,
`cyw43_iso/work/embassy-runtime/{wifi.bin,nvram.bin}` and
`cyw43_iso/work/sram-exec-probe/zero-append.bin` assets. The Embassy installer
references the checked controller blobs maintained by `cyw43_iso`. To refresh
the exported fixture and prepare hash-checked Wi-Fi/NVRAM assets, with the
captured ROM and validated zero-append firmware already present:

```sh
cd ../cyw43_iso
python3 analysis/check-controller-tx.py --continuous --output-blob work/iso-blob-continuous
python3 examples/embassy/prepare-runtime.py --firmware-dir ../embassy-cyw43-iso/cyw43-firmware
cd ../rp2040_audio
```

```sh
cargo build --release --locked
python3 check-capture.py
python3 check-progress.py
```

`rp2040` is the default board feature. Select exactly one board:

```sh
cargo build --release --locked --no-default-features --features rp2350 \
  --target thumbv8m.main-none-eabihf
```

RP2040 ELF: `target/thumbv6m-none-eabi/release/rp2040-audio-tx`.
RP2350 ELF: `target/thumbv8m.main-none-eabihf/release/rp2040-audio-tx`.
RP2350 uses Embassy's `examples/rp235x/memory.x` boot-block layout and the
`RM2_CLOCK_DIVIDER` used by its Pico 2 W Wi-Fi example; both boards use the same
CYW43 pin wiring and test fixture. Board/target mismatches fail the build.

Hardware owner flashes the ELF, captures RTT, then resets and restores stock.
Cargo defaults to info-level logs and a 32768-byte RTT buffer for capture.
Preserved `EMBASSY_NATIVE` logs report allocation, pauses, progress and final
`sent=1000 credits=1000 pass=true restart_required=true`.

```sh
python3 check-capture.py tx.txt stock-restored.txt
python3 ../cyw43_iso/analysis/check-sdc-capture.py receiver.txt \
  --fixture ../cyw43_iso/work/iso-blob-continuous/audio.lc3 \
  --stream-fixture --allow-underrun --require-discovery
```

Install and start occur before `Runner::run`. The application uses raw
`BtDriver`; do not send HCI Reset or start a normal Trouble host afterward,
because that stops the experimental radio. No Wi-Fi traffic or other Bluetooth
radio procedures may run concurrently. Completion events validate controller
credit accounting; independent receiver capture validates radio delivery.

Reset is mandatory after completion, failure or cancellation. The application
does not reclaim controller RAM or claim hook restoration while the runner owns
the bus.
