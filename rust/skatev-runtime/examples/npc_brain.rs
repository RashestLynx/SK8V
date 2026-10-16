//! NPC brain bench: one real Skate session on a synthetic plaza (road,
//! curb, sidewalk, a ledge, a flat bar), driven by `npc::Brain`. Prints the
//! brain's events and Skate's state changes.
//! Usage: npc_brain <skate-data/assets> [seconds] [seed]
use SkateVRuntime::coords::to_skate;
use SkateVRuntime::npc::{Ask, Brain, Nav, See};
use bevy_math::Vec3;
use skate_host::bridge::Session;

struct Scene {
    tris: Vec<[Vec3; 3]>,
    tags: Vec<u32>,
}
impl Scene {
    fn quad(&mut self, a: Vec3, b: Vec3, c: Vec3, d: Vec3, tag: u32) {
        // Counterclockwise seen from outside (z up for floors).
        self.tris.push([a, b, c]);
        self.tris.push([a, c, d]);
        self.tags.extend([tag, tag]);
    }
    /// Axis-aligned box from `lo` to `hi`, outward faces.
    fn cuboid(&mut self, lo: Vec3, hi: Vec3, tag: u32) {
        let p = |x: f32, y: f32, z: f32| Vec3::new(x, y, z);
        let (x0, y0, z0, x1, y1, z1) = (lo.x, lo.y, lo.z, hi.x, hi.y, hi.z);
        self.quad(p(x0, y0, z1), p(x1, y0, z1), p(x1, y1, z1), p(x0, y1, z1), tag); // top
        self.quad(p(x0, y0, z0), p(x0, y0, z1), p(x0, y1, z1), p(x0, y1, z0), tag); // -x
        self.quad(p(x1, y0, z0), p(x1, y1, z0), p(x1, y1, z1), p(x1, y0, z1), tag); // +x
        self.quad(p(x0, y0, z0), p(x1, y0, z0), p(x1, y0, z1), p(x0, y0, z1), tag); // -y
        self.quad(p(x0, y1, z0), p(x0, y1, z1), p(x1, y1, z1), p(x1, y1, z0), tag); // +y
    }
}

fn main() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    let root = std::path::PathBuf::from(args.get(1).ok_or("data root")?);
    let seconds: f32 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(120.0);
    let seed: u64 = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(7);
    let mut sc = Scene { tris: Vec::new(), tags: Vec::new() };
    let p = |x: f32, y: f32, z: f32| Vec3::new(x, y, z);
    // Road x in -60..-10, curb 0.15 m, plaza/sidewalk x in -10..60, y in -60..60.
    sc.quad(p(-60., -60., 0.), p(-10., -60., 0.), p(-10., 60., 0.), p(-60., 60., 0.), 1);
    sc.quad(p(-10., -60., 0.15), p(60., -60., 0.15), p(60., 60., 0.15), p(-10., 60., 0.15), 3);
    sc.quad(p(-10., -60., 0.), p(-10., 60., 0.), p(-10., 60., 0.15), p(-10., -60., 0.15), 3);
    // Outer walls so the bench stays bounded.
    for (a, b) in [(p(60., -60., 0.), p(60., 60., 0.)), (p(60., 60., 0.), p(-60., 60., 0.)), (p(-60., 60., 0.), p(-60., -60., 0.)), (p(-60., -60., 0.), p(60., -60., 0.))] {
        sc.quad(a, b, b + Vec3::Z * 3.0, a + Vec3::Z * 3.0, 3);
    }
    // A ledge (0.45 m) and a low box (0.3 m) in the plaza.
    sc.cuboid(p(10., -4., 0.15), p(10.6, 6., 0.6), 3);
    sc.cuboid(p(30., 10., 0.15), p(30.5, 22., 0.45), 3);
    let (rails, census) = SkateVRuntime::rails::find_metres(&sc.tris);
    println!("rails: {} ({:?})", rails.len(), census);
    let skate_tris: Vec<[[f32; 3]; 3]> = sc.tris.iter().map(|t| t.map(|v| to_skate(v).to_array())).collect();
    let skate_rails: Vec<Vec<[f32; 3]>> = rails.iter().map(|r| r.iter().map(|v| to_skate(*v).to_array()).collect()).collect();
    let nav = Nav::build(Vec3::ZERO, 64.0, &skate_tris, &sc.tags, &skate_rails);
    for r in &nav.rails {
        println!("rail {:.1?} -> {:.1?} height {:.2} out {:.1?}", r.a, r.b, r.height, r.out);
    }
    if args.get(4).map(String::as_str) == Some("sweep") {
        return sweep(&root, skate_tris, sc.tags.clone(), skate_rails);
    }
    let spawn = p(20., -20., 0.15);
    skate_data::collections::set_thread_override(Some(root.join("private/stock/skater-collections.original.json")).filter(|p| p.exists()));
    let mut s = Session::new_tagged(&root, skate_tris.clone(), sc.tags.clone(), skate_rails.clone(), to_skate(spawn).to_array(), 0.0)?;
    let prepared = s.collision_builder().build_tagged(skate_tris, sc.tags.clone(), skate_rails)?;
    s.install_collision(prepared)?;
    s.activate(to_skate(spawn).to_array(), SkateVRuntime::coords::skate_heading(0.0))?;
    let mut brain = Brain::new(seed);
    let player = p(20., 0., 0.15);
    let ticks = (seconds * 60.0) as usize;
    let mut last = String::new();
    let mut grinds = 0;
    for k in 0..ticks {
        let pose = s.pose();
        let see = See::from_pose(&pose);
        if see.state != last {
            if see.state.starts_with("Grind") {
                grinds += 1;
            }
            println!("{:7.2}s state {} at {:.1?} {:.1} m/s", k as f32 / 60.0, see.state, see.obs.pos, see.velocity.truncate().length());
            last = see.state.clone();
        }
        let (c, ask) = brain.next(&see, &nav, player);
        for l in brain.log.drain(..) {
            println!("{:7.2}s brain: {l}", k as f32 / 60.0);
        }
        if ask == Ask::Respawn {
            println!("{:7.2}s respawn at {:.1?}", k as f32 / 60.0, see.obs.pos);
            s.activate(to_skate(spawn).to_array(), 0.0)?;
            brain.reset();
            continue;
        }
        s.tick(c)?;
        if k % 600 == 0 {
            println!("{:7.2}s pos {:.1?} speed {:.1}", k as f32 / 60.0, see.obs.pos, see.velocity.truncate().length());
        }
    }
    println!("grinds: {grinds}");
    Ok(())
}

/// Grind recipe sweep on the 0.45 m ledge (x 10..10.6, y -4..6): approach
/// along +y from the -x side at angle `theta` toward the ledge, aimed to meet
/// its edge `hit` metres in, pop `d` metres before its start at speed `v`.
fn sweep(root: &std::path::Path, tris: Vec<[[f32; 3]; 3]>, tags: Vec<u32>, rails: Vec<Vec<[f32; 3]>>) -> Result<(), String> {
    use bevy_math::Vec2;
    use skate_host::bridge::Controls;
    if std::env::var("NPC_STOCK").is_ok() {
        let stock = root.join("private/stock/skater-collections.original.json");
        println!("stock tuning: {}", stock.exists());
        skate_data::collections::set_thread_override(Some(stock));
    }
    let mut s = Session::new_tagged(root, tris.clone(), tags.clone(), rails.clone(), to_skate(Vec3::new(0., -30., 0.15)).to_array(), 0.0)?;
    if std::env::var("NPC_INSTALL").is_ok() {
        let prepared = s.collision_builder().build_tagged(tris, tags, rails)?;
        s.install_collision(prepared)?;
        println!("collision installed through the builder");
    }
    let (edge_x, start_y) = (10.0f32, -4.0f32);
    for v in [4.5f32, 6.0, 7.5] {
        for theta in [4.0f32, 8.0, 12.0, 18.0] {
            for hit in [0.5f32, 2.0, 4.0] {
                for d in [0.6f32, 1.2, 1.8, 2.6, 3.4] {
                    if let Ok(only) = std::env::var("NPC_ONLY") {
                        if only != format!("{v} {theta} {hit} {d}") {
                            continue;
                        }
                    }
                    let dir = Vec2::new(theta.to_radians().sin(), theta.to_radians().cos());
                    let through = Vec2::new(edge_x, start_y + hit);
                    let from = through - dir * 32.0;
                    let heading = SkateVRuntime::coords::gta_heading(Vec3::new(dir.x, dir.y, 0.));
                    s.activate(to_skate(Vec3::new(from.x, from.y, 0.15)).to_array(), SkateVRuntime::coords::skate_heading(heading))?;
                    let mut popped: Option<usize> = None;
                    let mut result = String::from("-");
                    let mut last_push = 0usize;
                    let mut at_pop = (0.0f32, 0.0f32, 0.0f32);
                    for k in 0..900usize {
                        let pose = s.pose();
                        let see = See::from_pose(&pose);
                        let pos = see.obs.pos.truncate();
                        let speed = see.velocity.truncate().length();
                        if see.state.starts_with("Grind") {
                            result = format!("{} at y {:.1}", see.state, pos.y);
                            break;
                        }
                        if see.state.starts_with("Wipeout") {
                            result = format!("bail at y {:.1}", pos.y);
                            break;
                        }
                        if popped.is_some_and(|p| k > p + 150) {
                            result = format!("miss ({})", see.state);
                            break;
                        }
                        let mut c = Controls::default();
                        if popped.is_none() {
                            let along = (pos - from).dot(dir);
                            let target = from + dir * (along + 4.0);
                            if see.obs.rolling {
                                c.left[0] = SkateVRuntime::npc::steer(see.obs.forward.truncate(), target - pos);
                                if speed < v - 0.3 && k - last_push > 34 {
                                    last_push = k;
                                }
                                if k - last_push < 5 && k >= 5 {
                                    c.buttons = 0x1000;
                                }
                            }
                            // Pop: crouch 7 ticks then flick, timed so the flick lands at the mark.
                            let lead = speed * 7.0 / 60.0;
                            if pos.y > start_y - d - lead && see.obs.rolling {
                                popped = Some(k);
                                at_pop = (pos.y, edge_x - pos.x, speed);
                            }
                        }
                        if let Some(p) = popped {
                            if std::env::var("NPC_TRACE").is_ok() && (k - p) % 4 == 0 && k < p + 140 {
                                let (deck, _) = s.deck();
                                let d = SkateVRuntime::coords::from_skate(Vec3::from_array(deck));
                                println!("   +{:3} {} root {:.2?} deck {:.2?}", k - p, see.state, see.obs.pos, d);
                            }
                            c.left = [0, 0];
                            c.buttons = 0;
                            c.right = if k < p + 7 { [0, -32000] } else if k < p + 11 { [0, 32000] } else { [0, 0] };
                        }
                        s.tick(c)?;
                    }
                    println!("v {v:.1} theta {theta:4.1} hit {hit:.1} d {d:4.1}: pop y {:.2} out {:.2} at {:.1} m/s -> {result}", at_pop.0, at_pop.1, at_pop.2);
                }
            }
        }
    }
    Ok(())
}
