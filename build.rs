use std::{env, fs, path::PathBuf};
fn main() {
    let rp2040 = env::var_os("CARGO_FEATURE_RP2040").is_some();
    let rp2350 = env::var_os("CARGO_FEATURE_RP2350").is_some();
    assert!(
        rp2040 ^ rp2350,
        "select exactly one board: rp2040 or rp2350"
    );
    let (memory, target) = if rp2040 {
        ("memory-rp2040.x", "thumbv6m-none-eabi")
    } else {
        (
            "../embassy-cyw43-iso/examples/rp235x/memory.x",
            "thumbv8m.main-none-eabihf",
        )
    };
    assert_eq!(env::var("TARGET").unwrap(), target, "wrong board target");
    let output = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    fs::copy(memory, output.join("memory.x")).unwrap();
    println!("cargo:rustc-link-search={}", output.display());
    println!("cargo:rustc-link-arg=--nmagic");
    println!("cargo:rustc-link-arg=-Tlink.x");
    println!("cargo:rustc-link-arg=-Tdefmt.x");
    if rp2040 {
        println!("cargo:rustc-link-arg=-Tlink-rp.x");
    }
    println!("cargo:rerun-if-changed={memory}");
}
