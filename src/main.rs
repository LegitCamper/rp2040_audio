#![no_std]
#![no_main]

#[cfg(all(feature = "rx-hop", feature = "rx-timer-hop"))]
compile_error!("rx-hop and rx-timer-hop select separate controller images");

#[cfg(not(feature = "rx-diagnostic"))]
use bt_hci::FromHciBytes;
#[cfg(feature = "rx-diagnostic")]
use bt_hci::transport::{PacketToController, WithIndicator};
use bt_hci::{ControllerToHostPacket, transport::Transport};
#[cfg(not(feature = "rx-diagnostic"))]
use cyw43::iso::install as install_image;
#[cfg(all(feature = "rx-diagnostic", not(feature = "rx-stream")))]
use cyw43::iso::install_rx_diagnostic as install_image;
#[cfg(feature = "rx-hop")]
use cyw43::iso::install_rx_hop_diagnostic as install_image;
#[cfg(all(
    feature = "rx-stream",
    not(any(feature = "rx-hop", feature = "rx-timer-hop"))
))]
use cyw43::iso::install_rx_stream_diagnostic as install_image;
#[cfg(feature = "rx-timer-hop")]
use cyw43::iso::install_rx_timer_hop_diagnostic as install_image;
#[cfg(feature = "rp2040")]
use cyw43_pio::DEFAULT_CLOCK_DIVIDER as CLOCK_DIVIDER;
use cyw43_pio::PioSpi;
#[cfg(feature = "rp2350")]
use cyw43_pio::RM2_CLOCK_DIVIDER as CLOCK_DIVIDER;
use defmt_rtt as _;
use embassy_executor::Spawner;
use embassy_rp::{
    bind_interrupts, dma,
    gpio::{Level, Output},
    peripherals::{DMA_CH0, DMA_CH1, PIO0},
    pio::{InterruptHandler, Pio},
};
use embassy_time::{Duration, Timer, with_timeout};
use panic_probe as _;
use static_cell::StaticCell;

bind_interrupts!(struct Irqs {
    PIO0_IRQ_0 => InterruptHandler<PIO0>;
    DMA_IRQ_0 => dma::InterruptHandler<DMA_CH0>, dma::InterruptHandler<DMA_CH1>;
});

#[cfg(not(feature = "rx-diagnostic"))]
const AUDIO: &[u8; 520] = include_bytes!("../assets/audio.lc3");

#[embassy_executor::main]
async fn main(_spawner: Spawner) {
    defmt::info!("EMBASSY_NATIVE begin");
    let p = embassy_rp::init(Default::default());
    let pwr = Output::new(p.PIN_23, Level::Low);
    let cs = Output::new(p.PIN_25, Level::High);
    let mut pio = Pio::new(p.PIO0, Irqs);
    let spi = PioSpi::new(
        &mut pio.common,
        pio.sm0,
        CLOCK_DIVIDER,
        pio.irq0,
        cs,
        p.PIN_24,
        p.PIN_29,
        dma::Channel::new(p.DMA_CH0, Irqs),
        dma::Channel::new(p.DMA_CH1, Irqs),
    );
    static STATE: StaticCell<cyw43::State> = StaticCell::new();
    let (_net, driver, _control, mut runner) = cyw43::new_with_bluetooth(
        STATE.init(cyw43::State::new()),
        pwr,
        spi,
        cyw43_firmware::CYW43439_WIFI,
        cyw43::iso::BOOT_FIRMWARE,
        cyw43_firmware::CYW43439_NVRAM_RP2040,
    )
    .await;
    Timer::after_secs(2).await;
    let mut installed = match install_image(&mut runner).await {
        Ok(value) => value,
        Err(error) => {
            defmt::error!(
                "EMBASSY_NATIVE install failed {:?}",
                defmt::Debug2Format(&error)
            );
            halt().await;
        }
    };
    defmt::info!("EMBASSY_NATIVE allocation={=u32:08x}", installed.allocation);
    if let Err(error) = installed.start(&mut runner).await {
        defmt::error!(
            "EMBASSY_NATIVE start failed {:?}",
            defmt::Debug2Format(&error)
        );
        halt().await;
    }
    // Exercise the production runner, not the startup-only diagnostic pump.
    #[cfg(not(feature = "rx-diagnostic"))]
    embassy_futures::select::select(runner.run(), feed(&driver)).await;
    #[cfg(all(feature = "rx-diagnostic", not(feature = "rx-timer-hop")))]
    embassy_futures::select::select(runner.run(), receive(&driver)).await;
    #[cfg(feature = "rx-timer-hop")]
    {
        let stop = core::sync::atomic::AtomicBool::new(false);
        embassy_futures::join::join(runner.run_borrowed_until(&stop), receive(&driver, &stop))
            .await;
    }
    #[cfg(feature = "rx-timer-hop")]
    match installed.read_timer_hop_trace(&mut runner).await {
        Ok(trace) => defmt::info!(
            "EMBASSY_RX timer-trace event-counter={} channel={} configurations={} countdown={} callbacks={} advances={} marker-acquired={} scan-retunes={}",
            trace.event_counter,
            trace.current_channel,
            trace.config_count,
            trace.ticks_remaining,
            trace.accepted_callbacks,
            trace.completed_advances,
            trace.marker_acquired,
            trace.preacquisition_scan_retunes
        ),
        Err(error) => defmt::error!(
            "EMBASSY_RX timer-trace failed {:?}",
            defmt::Debug2Format(&error)
        ),
    }
    // Hardware owner restarts the chip and restores stock after capture.
    halt().await;
}

#[cfg(not(feature = "rx-diagnostic"))]
async fn feed(driver: &cyw43::bluetooth::BtDriver<'_>) {
    let mut sent = 0u16;
    let mut completed = 0u16;
    let mut bytes = [0; 1024];
    let result = with_timeout(Duration::from_secs(20), async {
        while completed < 1000 {
            while sent < 1000 && sent - completed < 4 {
                if sent == 8 {
                    defmt::info!("EMBASSY_NATIVE pause=80ms");
                    Timer::after_millis(80).await;
                }
                if sent == 500 {
                    defmt::info!("EMBASSY_NATIVE pause=250ms");
                    Timer::after_millis(250).await;
                }
                let mut wire = [0; 34];
                wire[..8].copy_from_slice(&[
                    0x42,
                    0x20,
                    30,
                    0,
                    sent as u8,
                    (sent >> 8) as u8,
                    26,
                    0,
                ]);
                let frame = usize::from(sent % 20) * 26;
                wire[8..].copy_from_slice(&AUDIO[frame..frame + 26]);
                let packet =
                    bt_hci::data::IsoPacket::from_hci_bytes_complete(&wire).map_err(|_| ())?;
                driver.write(&packet).await.map_err(|_| ())?;
                sent += 1;
            }
            match driver
                .read::<ControllerToHostPacket>(&mut bytes)
                .await
                .map_err(|_| ())?
            {
                ControllerToHostPacket::Event(event)
                    if event.kind.0 == 0x13 && event.data.len() == 5 && event.data[0] == 1 =>
                {
                    let handle = u16::from_le_bytes([event.data[1], event.data[2]]);
                    let count = u16::from_le_bytes([event.data[3], event.data[4]]);
                    if handle != 0x42 || count == 0 || count > sent - completed {
                        return Err(());
                    }
                    completed += count;
                    if completed / 100 != (completed - count) / 100 {
                        defmt::info!("EMBASSY_NATIVE sent={} credits={}", sent, completed);
                    }
                }
                _ => return Err(()),
            }
        }
        Ok(())
    })
    .await;
    defmt::info!(
        "EMBASSY_NATIVE done sent={} credits={} pass={} restart_required=true",
        sent,
        completed,
        result == Ok(Ok(())) && sent == 1000 && completed == 1000
    );
}

#[cfg(feature = "rx-diagnostic")]
mod rx_packet;

#[cfg(feature = "rx-diagnostic")]
const RX_COUNT: u32 = if cfg!(feature = "rx-stream") { 500 } else { 1 };

/// Delivery accounting. `next` is the lowest payload index still acceptable,
/// so duplicates and reordering are rejected in every mode. Hopping tolerates
/// gaps because a missed event is normal RF loss, not desynchronisation;
/// fixed-channel modes require `next` to advance one at a time. `delivered`
/// doubles as the expected HCI sequence number, which counts accepted SDUs.
#[cfg(feature = "rx-diagnostic")]
#[derive(Default)]
struct Progress {
    delivered: u32,
    next: u32,
}

// `Transport::read` hands back a parsed packet whose data load sits in the
// caller's buffer, so the H4 wire form is rebuilt for the strict checker. The
// round trip also proves the envelope the controller framed survives parsing.
#[cfg(feature = "rx-diagnostic")]
fn accept_rx(packet: &bt_hci::data::IsoPacket<'_>, progress: &mut Progress) -> Result<(), ()> {
    let mut wire = [0; 35];
    let written = {
        let mut tail = &mut wire[..];
        WithIndicator::new(packet)
            .write_hci(&mut tail)
            .map_err(|_| ())?;
        35 - tail.len()
    };
    let bytes = &wire[..written];
    // The marker's own event index is the only way to tell loss from
    // desynchronisation, so it is read from the payload rather than assumed.
    #[cfg(any(feature = "rx-hop", feature = "rx-timer-hop"))]
    let index = if written == 35 {
        u32::from(u16::from_le_bytes([bytes[9], bytes[10]]))
    } else {
        RX_COUNT
    };
    #[cfg(not(any(feature = "rx-hop", feature = "rx-timer-hop")))]
    let index = progress.next;
    let payload = if cfg!(feature = "rx-stream") {
        index as u16
    } else {
        0x0100
    };
    if index >= RX_COUNT
        || index < progress.next
        || !rx_packet::valid(bytes, progress.delivered as u16, payload)
    {
        #[cfg(feature = "rx-stream")]
        defmt::error!(
            "EMBASSY_RX invalid packet={=[u8]:02x} expected-count={}",
            bytes,
            progress.next
        );
        return Err(());
    }
    progress.next = index + 1;
    progress.delivered += 1;
    defmt::info!(
        "EMBASSY_RX packet={=[u8]:02x} count={} index={}",
        bytes,
        progress.delivered,
        index
    );
    Ok(())
}

#[cfg(feature = "rx-diagnostic")]
async fn rx_command(
    driver: &cyw43::bluetooth::BtDriver<'_>,
    stop: bool,
    progress: &mut Progress,
) -> Result<(), ()> {
    use bt_hci::{cmd::info::ReadLocalVersionInformation, cmd::le::LeTestEnd};
    if stop {
        driver.write(&LeTestEnd::from(())).await.map_err(|_| ())?;
    } else {
        driver
            .write(&ReadLocalVersionInformation::from(()))
            .await
            .map_err(|_| ())?;
    }
    let mut bytes = [0; 35];
    loop {
        match driver
            .read::<ControllerToHostPacket>(&mut bytes)
            .await
            .map_err(|_| ())?
        {
            ControllerToHostPacket::Iso(packet) => {
                accept_rx(&packet, progress)?;
            }
            ControllerToHostPacket::Event(event) if event.kind.0 == 0x0e => {
                let valid = if stop {
                    event.data.len() == 6 && event.data[1..4] == [0x1f, 0x20, 0]
                } else {
                    event.data.len() == 12
                        && event.data[1..] == [1, 0x10, 0, 0x0b, 0, 0, 0x0b, 0x31, 1, 0x10, 0x23]
                };
                return if valid && event.data[0] != 0 {
                    Ok(())
                } else {
                    Err(())
                };
            }
            _ => return Err(()),
        }
    }
}

#[cfg(feature = "rx-diagnostic")]
async fn receive(
    driver: &cyw43::bluetooth::BtDriver<'_>,
    #[cfg(feature = "rx-timer-hop")] stop: &core::sync::atomic::AtomicBool,
) {
    // ponytail: bounded fixed-channel diagnostics; add BIG synchronization and
    // hopping before treating this as a negotiated broadcast receiver.
    #[cfg(not(feature = "rx-stream"))]
    defmt::info!("EMBASSY_RX ready channel=0 mhz=2404 interval-ms=250 window-ms=20000");
    #[cfg(all(
        feature = "rx-stream",
        not(any(feature = "rx-hop", feature = "rx-timer-hop"))
    ))]
    defmt::info!("EMBASSY_RX ready channel=0 mhz=2404 interval-ms=0 window-ms=20000 mode=stream");
    #[cfg(feature = "rx-hop")]
    defmt::info!(
        "EMBASSY_RX ready channel=csa2 channel-id=0xcf58 event-counter=0 interval-ms=0 window-ms=20000 mode=hop"
    );
    #[cfg(feature = "rx-timer-hop")]
    defmt::info!(
        "EMBASSY_RX ready channel=csa2 channel-id=0xcf58 event-counter=0 interval-ms=20 window-ms=20000 mode=timer-hop preacquire=scan-per-countdown-callback"
    );
    let mut count = Progress::default();
    #[cfg(not(feature = "rx-stream"))]
    let result = async {
        for _ in 0..80 {
            with_timeout(
                Duration::from_secs(1),
                rx_command(driver, false, &mut count),
            )
            .await
            .map_err(|_| ())??;
            Timer::after_millis(250).await;
        }
        Ok::<_, ()>(())
    }
    .await;
    #[cfg(feature = "rx-stream")]
    let result = async {
        use embassy_futures::select::{Either, select};
        let deadline = embassy_time::Instant::now() + Duration::from_secs(20);
        let mut bytes = [0; 35];
        loop {
            match select(
                Timer::at(deadline),
                driver.read::<ControllerToHostPacket>(&mut bytes),
            )
            .await
            {
                Either::First(()) => return Ok::<_, ()>(()),
                Either::Second(Ok(ControllerToHostPacket::Iso(packet))) => {
                    accept_rx(&packet, &mut count)?;
                }
                _ => return Err(()),
            }
        }
    }
    .await;
    #[cfg(all(
        feature = "rx-stream",
        not(any(feature = "rx-hop", feature = "rx-timer-hop"))
    ))]
    let window_ok = {
        defmt::info!("EMBASSY_RX window-ended count={}", count.delivered);
        count.delivered == RX_COUNT
    };
    // Hopping reports span and loss instead of demanding every index: the host
    // checker decides, and a silent or misaligned transmitter still yields zero.
    #[cfg(any(feature = "rx-hop", feature = "rx-timer-hop"))]
    let window_ok = {
        defmt::info!(
            "EMBASSY_RX window-ended count={} span={} lost={}",
            count.delivered,
            count.next,
            count.next - count.delivered
        );
        count.delivered > 0
    };
    #[cfg(not(feature = "rx-stream"))]
    let window_ok = count.delivered == RX_COUNT;
    let stopped = with_timeout(Duration::from_secs(1), rx_command(driver, true, &mut count)).await
        == Ok(Ok(()));
    let stock_version = stopped
        && with_timeout(
            Duration::from_secs(1),
            rx_command(driver, false, &mut count),
        )
        .await
            == Ok(Ok(()));
    // Drain late packets after stop so duplicates/hardware errors cannot pass.
    let mut bytes = [0; 35];
    let quiet = with_timeout(
        Duration::from_millis(250),
        driver.read::<ControllerToHostPacket>(&mut bytes),
    )
    .await
    .is_err();
    defmt::info!(
        "EMBASSY_RX done count={} commands-ok={} stopped={} stock-version={} quiet={} pass={} restart_required=true",
        count.delivered,
        result.is_ok(),
        stopped,
        stock_version,
        quiet,
        result.is_ok() && window_ok && stopped && stock_version && quiet
    );
    #[cfg(feature = "rx-timer-hop")]
    stop.store(true, core::sync::atomic::Ordering::Release);
}

async fn halt() -> ! {
    loop {
        Timer::after_secs(1).await;
    }
}
