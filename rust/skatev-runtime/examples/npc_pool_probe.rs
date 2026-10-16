//! NPC pool end to end, offline: the pool's thread, sessions, spawning, ped
//! retarget and board pose on a synthetic plaza, read back as the host would.
//! Usage: npc_pool_probe <skate-data/assets> <ped cache> [count] [seconds]
use SkateVRuntime::coords::to_skate;
use SkateVRuntime::npc_pool::{offer_area, Pool};
use bevy_math::Vec3;
use std::sync::Arc;

fn main() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    let root = std::path::PathBuf::from(args.get(1).ok_or("data root")?);
    let peds = std::path::PathBuf::from(args.get(2).ok_or("ped cache")?);
    let count: usize = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(3);
    let seconds: f32 = args.get(4).and_then(|s| s.parse().ok()).unwrap_or(60.0);
    let p = |x: f32, y: f32, z: f32| Vec3::new(x, y, z);
    let mut tris: Vec<[Vec3; 3]> = Vec::new();
    let mut tags = Vec::new();
    let mut quad = |a: Vec3, b: Vec3, c: Vec3, d: Vec3, tag: u32| {
        tris.push([a, b, c]);
        tris.push([a, c, d]);
        tags.extend([tag, tag]);
    };
    quad(p(-100., -100., 0.), p(100., -100., 0.), p(100., 100., 0.), p(-100., 100., 0.), 3);
    // A ledge.
    let (x0, y0, x1, y1, z) = (10.0, -4.0, 10.6, 6.0, 0.45);
    quad(p(x0, y0, z), p(x1, y0, z), p(x1, y1, z), p(x0, y1, z), 3);
    quad(p(x0, y0, 0.), p(x0, y0, z), p(x0, y1, z), p(x0, y1, 0.), 3);
    quad(p(x1, y0, 0.), p(x1, y1, 0.), p(x1, y1, z), p(x1, y0, z), 3);
    quad(p(x0, y0, 0.), p(x1, y0, 0.), p(x1, y0, z), p(x0, y0, z), 3);
    quad(p(x0, y1, 0.), p(x0, y1, z), p(x1, y1, z), p(x1, y1, 0.), 3);
    let (rails, _) = SkateVRuntime::rails::find_metres(&tris);
    let skate_tris: Vec<[[f32; 3]; 3]> = tris.iter().map(|t| t.map(|v| to_skate(v).to_array())).collect();
    let skate_rails: Vec<Vec<[f32; 3]>> = rails.iter().map(|r| r.iter().map(|v| to_skate(*v).to_array()).collect()).collect();
    let log: Arc<dyn Fn(&str) + Send + Sync> = Arc::new(|m: &str| println!("log: {m}"));
    let pool = Pool::start(root, log)?;
    pool.configure(count, peds);
    offer_area(Vec3::ZERO, &skate_tris, &tags, &skate_rails);
    let player = p(0., 0., 0.);
    let start = std::time::Instant::now();
    let mut last_report = 0.0;
    let mut gens = vec![0u32; 4];
    while start.elapsed().as_secs_f32() < seconds {
        std::thread::sleep(std::time::Duration::from_millis(16));
        pool.frame(0.016, player, true);
        let t = start.elapsed().as_secs_f32();
        let views = pool.views.lock().unwrap().clone();
        for (i, v) in views.iter().enumerate() {
            if v.generation != gens[i] {
                gens[i] = v.generation;
                println!("{t:6.1}s npc {i}: spawn #{} model {:#010x} at {:.1?}", v.generation, v.model, v.position);
            }
        }
        if t - last_report > 5.0 {
            last_report = t;
            for (i, v) in views.iter().enumerate().filter(|(_, v)| v.active) {
                let finite = v.pose.iter().all(|m| m.is_finite());
                println!(
                    "{t:6.1}s npc {i}: {} at {:.1?} {:.1} m/s heading {:.0}, {} bones (finite {finite}), board {}",
                    v.state,
                    v.position,
                    v.velocity.truncate().length(),
                    v.heading,
                    v.pose.len(),
                    v.board.is_some()
                );
            }
        }
    }
    Ok(())
}
