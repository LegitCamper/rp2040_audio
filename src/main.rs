#![no_std]
#![no_main]

#[cfg(not(feature = "rx-diagnostic"))]
use bt_hci::FromHciBytes;
#[cfg(feature = "rx-diagnostic")]
use bt_hci::WriteHci;
use bt_hci::{ControllerToHostPacket, transport::Transport};
#[cfg(not(feature = "rx-diagnostic"))]
use cyw43::iso::install as install_image;
#[cfg(all(feature = "rx-diagnostic", not(feature = "rx-stream")))]
use cyw43::iso::install_rx_diagnostic as install_image;
#[cfg(feature = "rx-stream")]
use cyw43::iso::install_rx_stream_diagnostic as install_image;
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
    peripherals::{DMA_CH0, PIO0},
    pio::{InterruptHandler, Pio},
};
use embassy_time::{Duration, Timer, with_timeout};
use panic_probe as _;
use static_cell::StaticCell;

bind_interrupts!(struct Irqs {
    PIO0_IRQ_0 => InterruptHandler<PIO0>;
    DMA_IRQ_0 => dma::InterruptHandler<DMA_CH0>;
});

#[cfg(not(feature = "rx-diagnostic"))]
const AUDIO: &[u8; 520] = include_bytes!("../../cyw43_iso/work/iso-blob-continuous/audio.lc3");

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
    );
    static STATE: StaticCell<cyw43::State> = StaticCell::new();
    let (_net, driver, _control, mut runner) = cyw43::new_with_bluetooth(
        STATE.init(cyw43::State::new()),
        pwr,
        spi,
        cyw43::aligned_bytes!("../../cyw43_iso/work/embassy-runtime/wifi.bin"),
        cyw43::aligned_bytes!("../../cyw43_iso/work/sram-exec-probe/zero-append.bin"),
        cyw43::aligned_bytes!("../../cyw43_iso/work/embassy-runtime/nvram.bin"),
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
    #[cfg(feature = "rx-diagnostic")]
    embassy_futures::select::select(runner.run(), receive(&driver)).await;
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
            match driver.read(&mut bytes).await.map_err(|_| ())? {
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

#[cfg(feature = "rx-diagnostic")]
fn accept_rx(packet_size: usize, bytes: &[u8], count: &mut u32) -> Result<(), ()> {
    if packet_size != 34
        || *count >= RX_COUNT
        || !rx_packet::valid(bytes, *count as u16, cfg!(feature = "rx-stream"))
    {
        #[cfg(feature = "rx-stream")]
        defmt::error!(
            "EMBASSY_RX invalid packet={=[u8]:02x} expected-count={}",
            bytes,
            count
        );
        return Err(());
    }
    *count += 1;
    defmt::info!("EMBASSY_RX packet={=[u8]:02x} count={}", bytes, count);
    Ok(())
}

#[cfg(feature = "rx-diagnostic")]
async fn rx_command(
    driver: &cyw43::bluetooth::BtDriver<'_>,
    stop: bool,
    count: &mut u32,
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
        match driver.read(&mut bytes).await.map_err(|_| ())? {
            ControllerToHostPacket::Iso(packet) => {
                accept_rx(packet.size(), &bytes, count)?;
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
async fn receive(driver: &cyw43::bluetooth::BtDriver<'_>) {
    // ponytail: bounded fixed-channel diagnostics; add BIG synchronization and
    // hopping before treating this as a negotiated broadcast receiver.
    #[cfg(not(feature = "rx-stream"))]
    defmt::info!("EMBASSY_RX ready channel=0 mhz=2404 interval-ms=250 window-ms=20000");
    #[cfg(feature = "rx-stream")]
    defmt::info!("EMBASSY_RX ready channel=0 mhz=2404 interval-ms=0 window-ms=20000 mode=stream");
    let mut count = 0;
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
            match select(Timer::at(deadline), driver.read(&mut bytes)).await {
                Either::First(()) => return Ok::<_, ()>(()),
                Either::Second(Ok(ControllerToHostPacket::Iso(packet))) => {
                    accept_rx(packet.size(), &bytes, &mut count)?;
                }
                _ => return Err(()),
            }
        }
    }
    .await;
    #[cfg(feature = "rx-stream")]
    let window_ok = {
        defmt::info!("EMBASSY_RX window-ended count={}", count);
        count == RX_COUNT
    };
    #[cfg(not(feature = "rx-stream"))]
    let window_ok = true;
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
    let quiet = with_timeout(Duration::from_millis(250), driver.read(&mut bytes))
        .await
        .is_err();
    defmt::info!(
        "EMBASSY_RX done count={} commands-ok={} stopped={} stock-version={} quiet={} pass={} restart_required=true",
        count,
        result.is_ok(),
        stopped,
        stock_version,
        quiet,
        result.is_ok() && window_ok && count == RX_COUNT && stopped && stock_version && quiet
    );
}

async fn halt() -> ! {
    loop {
        Timer::after_secs(1).await;
    }
}
