#!/usr/bin/env python3
"""Check the app's actual log condition against legal batched ISO credits."""
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
source = (ROOT / "src/main.rs").read_text()
condition = re.search(r'if ([^\n]+) \{\s*defmt::info!\("EMBASSY_NATIVE sent=', source)[1]
program = r'''
fn main() {
    for seed in 0..256u32 {
        let mut random = seed;
        let mut completed = 0u16;
        let mut checkpoints = Vec::new();
        while completed < 1000 {
            random = random.wrapping_mul(1664525).wrapping_add(1013904223);
            let count = if seed == 0 {
                if completed == 0 { 3 } else { 4 }
            } else { (random >> 16) as u16 % 4 + 1 }.min(1000 - completed);
            completed += count;
            if CONDITION { checkpoints.push(completed); }
        }
        assert_eq!(checkpoints.len(), 10, "lost progress with seed {seed}");
        for (index, value) in checkpoints.iter().enumerate() {
            assert_eq!(usize::from(*value / 100), index + 1);
        }
        assert_eq!(checkpoints.last(), Some(&1000));
        if seed == 0 { for value in checkpoints { println!("{value}"); } }
    }
}
'''.replace("CONDITION", condition)
with tempfile.TemporaryDirectory(prefix="iso-progress-") as directory:
    rust = Path(directory) / "check.rs"
    binary = Path(directory) / "check"
    rust.write_text(program)
    subprocess.run(["rustc", "--edition=2024", str(rust), "-o", str(binary)], check=True)
    checkpoints = list(map(int, subprocess.check_output([str(binary)], text=True).split()))

lines = ["begin", "allocation=00217e78", "pause=80ms"]
for completed in checkpoints:
    if completed // 100 == 5:
        lines.append("pause=250ms")
    lines.append(f"sent={completed} credits={completed}")
lines.append("done sent=1000 credits=1000 pass=true restart_required=true")
capture = "\n".join("EMBASSY_NATIVE " + line for line in lines)
stock = "LMP subversion 0x2310\nRESULT: controller ISO support absent\n" * 2
checker = runpy.run_path(str(ROOT.parent / "cyw43_iso/analysis/check-embassy-capture.py"))
checker["check_normal_runner"](capture, stock)
print("256 legal credit-batch schedules preserve all progress checkpoints; capture checker passes")
