//! NPC skater cost probe: builds N full Skate sessions on a flat floor and
//! reports build time, memory and per-tick cost while each one pushes.
//! Usage: npc_cost <skate-data/assets> [sessions] [ticks]
use skate_host::bridge::{Controls, Session};
use std::time::Instant;

#[repr(C)]
#[derive(Default)]
struct Counters {
    cb: u32,
    page_faults: u32,
    peak_ws: usize,
    ws: usize,
    quota: [usize; 4],
    pagefile: usize,
    peak_pagefile: usize,
    private: usize,
}
unsafe extern "system" {
    fn GetCurrentProcess() -> isize;
    fn K32GetProcessMemoryInfo(p: isize, c: *mut Counters, cb: u32) -> i32;
}
fn private_mb() -> f64 {
    let mut c = Counters { cb: size_of::<Counters>() as u32, ..Default::default() };
    unsafe { K32GetProcessMemoryInfo(GetCurrentProcess(), &mut c, c.cb) };
    c.private as f64 / 1048576.0
}

fn main() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    let root = std::path::PathBuf::from(args.get(1).ok_or("data root")?);
    let n: usize = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(3);
    let ticks: usize = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(600);
    let base = private_mb();
    let t = Instant::now();
    Session::preload(&root)?;
    println!("animation banks: {:.0} ms, +{:.0} MB", t.elapsed().as_secs_f64() * 1e3, private_mb() - base);
    let h = 400.0;
    let floor = vec![
        [[-h, 0.0, -h], [-h, 0.0, h], [h, 0.0, h]],
        [[-h, 0.0, -h], [h, 0.0, h], [h, 0.0, -h]],
    ];
    let mut sessions = Vec::new();
    for i in 0..n {
        let before = private_mb();
        let t = Instant::now();
        let spawn = [i as f32 * 4.0, 0.2, 0.0];
        let mut s = Session::new_tagged(&root, floor.clone(), vec![0; 2], Vec::new(), spawn, 0.0)?;
        let built = t.elapsed().as_secs_f64() * 1e3;
        s.activate(spawn, 0.0)?;
        println!(
            "session {i}: built {built:.0} ms, activated {:.0} ms, +{:.0} MB (total private {:.0} MB)",
            t.elapsed().as_secs_f64() * 1e3,
            private_mb() - before,
            private_mb()
        );
        sessions.push(s);
    }
    let period = sessions[0].period();
    let mut cost = vec![0.0f64; n];
    let mut worst = vec![0.0f64; n];
    for k in 0..ticks {
        // Tap A (push) every 40 ticks, slight carve.
        let push = k % 40 < 6;
        let c = Controls {
            buttons: if push { 0x1000 } else { 0 },
            triggers: [0, 0],
            left: [6000, 0],
            right: [0, 0],
        };
        for (i, s) in sessions.iter_mut().enumerate() {
            let t = Instant::now();
            s.tick(c)?;
            let _ = s.pose();
            let ms = t.elapsed().as_secs_f64() * 1e3;
            cost[i] += ms;
            worst[i] = worst[i].max(ms);
        }
    }
    for (i, s) in sessions.iter().enumerate() {
        let p = s.pose();
        println!(
            "session {i}: tick avg {:.3} ms, max {:.2} ms (period {:.1} ms), speed {:.2} m/s, state {}, {} bones",
            cost[i] / ticks as f64,
            worst[i],
            period * 1e3,
            p.velocity.length(),
            p.state,
            p.bones.len()
        );
    }
    Ok(())
}
