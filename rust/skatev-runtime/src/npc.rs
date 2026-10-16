//! NPC skaters: the controller that rides an ambient skater's own Skate
//! session. Skate does all the skating; this only holds a pad, the way
//! `line.rs` does for showcase lines: it pushes up to a cruising speed, steers
//! along sidewalks and plazas, pops flat-ground tricks, and lines up with
//! ledges and rails it finds in the collision area to ollie into a grind.
//! Bails are Skate's own; the controller presses A to get up, like a player.
//!
//! Everything here is GTA space (z up). `Nav` is built from the same area the
//! player's session rides (Skate-space triangles, audio tags and rails).
use crate::line::Obs;
use bevy_math::{Vec2, Vec3};
use skate_host::bridge::Controls;

const A: u16 = 0x1000;
const Y: u16 = 0x8000;
const FULL: i16 = 32000;

/// Rideable ground on a coarse XY grid, walls as height spans, and the
/// grindable segments of the area's rails.
pub struct Nav {
    origin: Vec2,
    side: usize,
    floors: Vec<[Floor; 3]>,
    walls: Vec<[(f32, f32); 3]>,
    pub rails: Vec<Rail>,
    pub center: Vec3,
}

#[derive(Clone, Copy, Default, Debug)]
struct Floor {
    z: f32,
    /// 0 none, 1 road (asphalt), 2 sidewalk / plaza / anything else.
    kind: u8,
}

/// A straight grindable segment and the side the skater rides up on.
#[derive(Clone, Copy, Debug)]
pub struct Rail {
    pub a: Vec3,
    pub b: Vec3,
    /// Unit XY normal toward the low side (where the approach rolls).
    pub out: Vec2,
    /// Lip height above that ground.
    pub height: f32,
}

const CELL: f32 = 1.0;
/// Rail run-up: metres before the lip, metres out from it, speed.
const RUN_UP: f32 = 10.0;
const RUN_OUT: f32 = 1.6;
const APPROACH_SPEED: f32 = 5.5;
const WALKABLE: f32 = 0.85;
const STEP_UP: f32 = 0.22;
const DROP: f32 = 1.3;

fn edge_side(p: Vec2, a: Vec2, b: Vec2) -> f32 {
    (b - a).perp_dot(p - a)
}

impl Nav {
    /// `triangles`/`rails` in Skate space as the area holds them; `tags` the
    /// packed audio tag per triangle (low 7 bits: Skate audio row + 1).
    pub fn build(center: Vec3, radius: f32, triangles: &[[[f32; 3]; 3]], tags: &[u32], rails: &[Vec<[f32; 3]>]) -> Self {
        let side = (2.0 * radius / CELL).ceil() as usize;
        let origin = center.truncate() - Vec2::splat(radius);
        let mut nav = Self {
            origin,
            side,
            floors: vec![[Floor::default(); 3]; side * side],
            walls: vec![[(f32::NAN, f32::NAN); 3]; side * side],
            rails: Vec::new(),
            center,
        };
        let gta = |v: [f32; 3]| crate::coords::from_skate(Vec3::from_array(v));
        let mut walkable = Vec::new();
        for (i, t) in triangles.iter().enumerate() {
            let [a, b, c] = t.map(gta);
            let n = (b - a).cross(c - a);
            let len = n.length();
            if !(len > 1e-6) {
                continue;
            }
            let nz = n.z / len;
            let tag = tags.get(i).copied().unwrap_or(0);
            if nz.abs() >= WALKABLE {
                let audio = tag & 0x7f;
                let kind = if audio == 1 || audio == 2 { 1 } else { 2 };
                nav.floor_tri(a, b, c, n, kind);
                walkable.push([a, b, c]);
            } else if nz.abs() < 0.5 && tag & skate_host::bridge::NO_GRIND_TAG == 0 {
                nav.wall_tri(a, b, c);
            }
        }
        // Exact top test at a lip (the grid is too coarse for a narrow ledge),
        // over the walkable triangles bucketed in 4 m cells.
        const BUCKET: f32 = 4.0;
        let bside = (side as f32 * CELL / BUCKET).ceil() as usize;
        let bcell = |p: Vec2| {
            let q = (p - origin) / BUCKET;
            (q.x >= 0.0 && q.y >= 0.0 && (q.x as usize) < bside && (q.y as usize) < bside).then(|| q.y as usize * bside + q.x as usize)
        };
        let mut buckets: Vec<Vec<u32>> = vec![Vec::new(); bside * bside];
        for (k, [a, b, c]) in walkable.iter().enumerate() {
            let lo = a.truncate().min(b.truncate()).min(c.truncate());
            let hi = a.truncate().max(b.truncate()).max(c.truncate());
            let (x0, y0) = (((lo.x - origin.x) / BUCKET).floor().max(0.0) as usize, ((lo.y - origin.y) / BUCKET).floor().max(0.0) as usize);
            let (x1, y1) = (((hi.x - origin.x) / BUCKET).floor() as isize, ((hi.y - origin.y) / BUCKET).floor() as isize);
            for y in y0..=(y1.min(bside as isize - 1).max(-1) as usize) {
                if y as isize > y1 {
                    break;
                }
                for x in x0..=(x1.min(bside as isize - 1).max(-1) as usize) {
                    if x as isize > x1 {
                        break;
                    }
                    buckets[y * bside + x].push(k as u32);
                }
            }
        }
        let top = |p: Vec2, z: f32| {
            let Some(cell) = bcell(p) else { return false };
            buckets[cell].iter().map(|&k| &walkable[k as usize]).any(|[a, b, c]: &[Vec3; 3]| {
                let (a2, b2, c2) = (a.truncate(), b.truncate(), c.truncate());
                if p.x < a2.x.min(b2.x).min(c2.x) || p.x > a2.x.max(b2.x).max(c2.x) || p.y < a2.y.min(b2.y).min(c2.y) || p.y > a2.y.max(b2.y).max(c2.y) {
                    return false;
                }
                let sd = [edge_side(p, a2, b2), edge_side(p, b2, c2), edge_side(p, c2, a2)];
                if !(sd.iter().all(|&v| v >= 0.0) || sd.iter().all(|&v| v <= 0.0)) {
                    return false;
                }
                let n = (*b - *a).cross(*c - *a);
                let zp = a.z - (n.x * (p.x - a.x) + n.y * (p.y - a.y)) / n.z;
                (zp - z).abs() < 0.08
            })
        };
        nav.rails = rails.iter().flat_map(|r| nav.segments(&r.iter().map(|p| gta(*p)).collect::<Vec<_>>(), &top)).collect();
        nav
    }

    fn cell(&self, p: Vec2) -> Option<usize> {
        let q = (p - self.origin) / CELL;
        if q.x < 0.0 || q.y < 0.0 {
            return None;
        }
        let (x, y) = (q.x as usize, q.y as usize);
        (x < self.side && y < self.side).then_some(y * self.side + x)
    }

    fn floor_tri(&mut self, a: Vec3, b: Vec3, c: Vec3, n: Vec3, kind: u8) {
        let (a2, b2, c2) = (a.truncate(), b.truncate(), c.truncate());
        let lo = a2.min(b2).min(c2);
        let hi = a2.max(b2).max(c2);
        let ccw = edge_side(c2, a2, b2) >= 0.0;
        let inside = |p: Vec2| {
            let s = [edge_side(p, a2, b2), edge_side(p, b2, c2), edge_side(p, c2, a2)];
            if ccw { s.iter().all(|&v| v >= 0.0) } else { s.iter().all(|&v| v <= 0.0) }
        };
        let z_at = |p: Vec2| a.z - (n.x * (p.x - a.x) + n.y * (p.y - a.y)) / n.z;
        let last = self.side as isize - 1;
        let index = |v: f32, o: f32| ((v - o) / CELL).floor() as isize;
        let (x0, x1) = (index(lo.x, self.origin.x).max(0), index(hi.x, self.origin.x).min(last));
        let (y0, y1) = (index(lo.y, self.origin.y).max(0), index(hi.y, self.origin.y).min(last));
        let mut hit = false;
        for y in y0..=y1 {
            for x in x0..=x1 {
                let p = self.origin + Vec2::new(x as f32 + 0.5, y as f32 + 0.5) * CELL;
                if inside(p) {
                    hit = true;
                    self.add_floor(y as usize * self.side + x as usize, z_at(p), kind);
                }
            }
        }
        if !hit {
            let p = (a2 + b2 + c2) / 3.0;
            if let Some(i) = self.cell(p) {
                self.add_floor(i, (a.z + b.z + c.z) / 3.0, kind);
            }
        }
    }

    fn add_floor(&mut self, i: usize, z: f32, kind: u8) {
        let f = &mut self.floors[i];
        if let Some(e) = f.iter_mut().find(|e| e.kind != 0 && (e.z - z).abs() < 0.3) {
            // Same level: keep the higher surface, and sidewalk over road.
            if z > e.z {
                e.z = z;
            }
            e.kind = e.kind.max(kind);
            return;
        }
        if let Some(e) = f.iter_mut().find(|e| e.kind == 0) {
            *e = Floor { z, kind };
        } else if let Some(e) = f.iter_mut().min_by(|x, y| x.z.total_cmp(&y.z)) {
            // Three levels already (bridges, roofs): keep the highest ones.
            if z > e.z {
                *e = Floor { z, kind };
            }
        }
    }

    fn wall_tri(&mut self, a: Vec3, b: Vec3, c: Vec3) {
        let longest = a.distance(b).max(b.distance(c)).max(c.distance(a));
        let n = ((longest / CELL).ceil() as usize).clamp(1, 120);
        for i in 0..=n {
            for j in 0..=(n - i) {
                let (u, v) = (i as f32 / n as f32, j as f32 / n as f32);
                let p = a + (b - a) * u + (c - a) * v;
                if let Some(k) = self.cell(p.truncate()) {
                    let spans = &mut self.walls[k];
                    if let Some(s) = spans.iter_mut().find(|s| !s.0.is_nan() && p.z > s.0 - 0.4 && p.z < s.1 + 0.4) {
                        s.0 = s.0.min(p.z);
                        s.1 = s.1.max(p.z);
                    } else if let Some(s) = spans.iter_mut().find(|s| s.0.is_nan()) {
                        *s = (p.z, p.z);
                    } else {
                        spans[2].0 = spans[2].0.min(p.z);
                        spans[2].1 = spans[2].1.max(p.z);
                    }
                }
            }
        }
    }

    fn blocked(&self, i: usize, z: f32) -> bool {
        self.walls[i].iter().any(|s| !s.0.is_nan() && s.0 < z + 1.6 && s.1 > z + 0.3)
    }

    /// The ground a skater at height `z` rolls onto at `p`: the highest floor
    /// from a drop below up to a curb above, clear of walls.
    pub fn ground(&self, p: Vec2, z: f32) -> Option<(f32, u8)> {
        let i = self.cell(p)?;
        let f = self.floors[i]
            .iter()
            .filter(|f| f.kind != 0 && f.z <= z + STEP_UP && f.z >= z - DROP)
            .max_by(|x, y| x.z.total_cmp(&y.z))?;
        (!self.blocked(i, f.z)).then_some((f.z, f.kind))
    }

    /// How good rolling `len` metres along `dir` is: sidewalk counts fully,
    /// road a third; the walk stops at a wall, a drop or the area's edge.
    pub fn probe(&self, from: Vec3, dir: Vec2, len: f32) -> f32 {
        let mut z = from.z;
        let mut score = 0.0;
        let steps = (len / CELL).ceil() as usize;
        for k in 1..=steps {
            let p = from.truncate() + dir * (k as f32 * CELL);
            match self.ground(p, z) {
                Some((g, kind)) => {
                    z = g;
                    score += if kind == 2 { 1.0 } else { 0.35 };
                }
                None => return score - (steps + 1 - k) as f32 * 1.5,
            }
        }
        score / steps as f32
    }

    /// A sidewalk point `min..max` metres from `around`, or None.
    pub fn pick(&self, rng: &mut u64, around: Vec2, min: f32, max: f32, z_hint: f32) -> Option<Vec3> {
        for _ in 0..80 {
            let a = rand(rng) * std::f32::consts::TAU;
            let d = min + rand(rng) * (max - min);
            let p = around + Vec2::new(a.cos(), a.sin()) * d;
            let Some(i) = self.cell(p) else { continue };
            let best = self.floors[i]
                .iter()
                .filter(|f| f.kind == 2 && (f.z - z_hint).abs() < 12.0 && !self.blocked(i, f.z))
                .min_by(|x, y| (x.z - z_hint).abs().total_cmp(&(y.z - z_hint).abs()));
            if let Some(f) = best {
                // Room to roll: open sidewalk around it.
                let p3 = Vec3::new(p.x, p.y, f.z);
                let open = (0..8).filter(|k| {
                    let a = *k as f32 * std::f32::consts::FRAC_PI_4;
                    self.ground(p + Vec2::new(a.cos(), a.sin()) * 2.0, f.z).is_some_and(|g| g.1 == 2)
                });
                if open.count() >= 6 {
                    return Some(p3);
                }
            }
        }
        None
    }

    /// Straight runs of a rail polyline, kept when they read as a ledge or
    /// rail at a grindable height above an approachable side.
    fn segments(&self, pts: &[Vec3], top: &dyn Fn(Vec2, f32) -> bool) -> Vec<Rail> {
        let mut out = Vec::new();
        let mut start = 0;
        while start + 1 < pts.len() {
            let mut end = start + 1;
            let dir = (pts[end] - pts[start]).truncate().normalize_or_zero();
            while end + 1 < pts.len() && (pts[end + 1] - pts[end]).truncate().normalize_or_zero().dot(dir) > 0.99 {
                end += 1;
            }
            let (a, b) = (pts[start], pts[end]);
            start = end;
            let d = (b - a).truncate();
            let len = d.length();
            if len < 2.0 || (b.z - a.z).abs() / len > 0.35 {
                continue;
            }
            let dir = d / len;
            let perp = Vec2::new(-dir.y, dir.x);
            let mid = (a + b) * 0.5;
            // The low side: ground just past the lip and further out, both
            // clearly below it (a ledge's other side is its own top).
            let below = |side: f32, reach: f32| {
                let p = mid.truncate() + perp * side * reach;
                let i = self.cell(p)?;
                self.floors[i]
                    .iter()
                    .filter(|f| f.kind != 0 && f.z > mid.z - 1.2)
                    .map(|f| f.z)
                    .max_by(f32::total_cmp)
                    .filter(|&z| z < mid.z - 0.1)
            };
            // The low side has no top at the lip's height just past it and
            // ground clearly below further out; the other side is a ledge top
            // (or, for a rail, open too: roll in on the sidewalk side).
            let low = |side: f32| {
                if top(mid.truncate() + perp * side * 0.15, mid.z) {
                    return None;
                }
                let z = below(side, 1.2).or_else(|| below(side, 0.7))?;
                let kind = self.ground(mid.truncate() + perp * side * 1.6, z + 0.1).map_or(0, |g| g.1);
                Some((z, kind))
            };
            let (out_side, ground) = match (low(1.0), low(-1.0)) {
                (Some(l), Some(r)) => if l.1 >= r.1 { (1.0, l.0) } else { (-1.0, r.0) },
                (Some(l), None) => (1.0, l.0),
                (None, Some(r)) => (-1.0, r.0),
                (None, None) => continue,
            };
            let height = mid.z - ground;
            if !(0.12..=1.0).contains(&height) {
                continue;
            }
            out.push(Rail { a, b, out: perp * out_side, height });
        }
        out
    }
}

/// xorshift; 0..1.
pub fn rand(s: &mut u64) -> f32 {
    *s ^= *s << 13;
    *s ^= *s >> 7;
    *s ^= *s << 17;
    (*s >> 40) as f32 / (1u64 << 24) as f32
}

/// What the controller sees of its skater each tick.
#[derive(Clone, Debug)]
pub struct See {
    pub obs: Obs,
    pub velocity: Vec3,
    pub state: String,
}

impl See {
    pub fn from_pose(p: &skate_host::bridge::Pose) -> Self {
        Self {
            obs: Obs::from_pose(p),
            velocity: crate::coords::from_skate(p.velocity),
            state: p.state.clone(),
        }
    }
    fn speed(&self) -> f32 {
        self.velocity.truncate().length()
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum Mode {
    Cruise,
    /// Rolling to the run-up of rail `rail`, entering at end `a` (else `b`).
    /// `stage` 0: getting behind the run-up, 1: onto it, 2: on the line in.
    Approach { rail: usize, from_a: bool, since: u64, stage: u8 },
    /// Popped for the rail; waiting to lock on.
    Popped { rail: usize, at: u64 },
    Grind { rail: usize },
}

/// The controller's ask of its owner.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Ask {
    None,
    /// Put the skater somewhere new (stuck, lost, or too far from the player).
    Respawn,
}

pub struct Brain {
    rng: u64,
    tick: u64,
    mode: Mode,
    queue: std::collections::VecDeque<Controls>,
    goal: Option<Vec2>,
    goal_until: u64,
    heading: Vec2,
    cruise: f32,
    next_trick: u64,
    next_spot: u64,
    last_push: u64,
    slow: u32,
    state_ticks: u32,
    last_state: String,
    used: Vec<(usize, u64)>,
    /// Recent events for the log (drained by the owner).
    pub log: Vec<String>,
}

impl Brain {
    pub fn new(seed: u64) -> Self {
        let mut rng = seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1;
        let cruise = 4.5 + rand(&mut rng) * 2.5;
        Self {
            rng,
            tick: 0,
            mode: Mode::Cruise,
            queue: Default::default(),
            goal: None,
            goal_until: 0,
            heading: Vec2::ZERO,
            cruise,
            next_trick: 300,
            next_spot: 120,
            last_push: 0,
            slow: 0,
            state_ticks: 0,
            last_state: String::new(),
            used: Vec::new(),
            log: Vec::new(),
        }
    }

    /// After a respawn: forget the run in progress.
    pub fn reset(&mut self) {
        self.mode = Mode::Cruise;
        self.queue.clear();
        self.goal = None;
        self.slow = 0;
        self.heading = Vec2::ZERO;
    }

    fn rnd(&mut self) -> f32 {
        rand(&mut self.rng)
    }

    /// One pad frame for the next Skate tick.
    pub fn next(&mut self, see: &See, nav: &Nav, player: Vec3) -> (Controls, Ask) {
        self.tick += 1;
        if see.state == self.last_state {
            self.state_ticks += 1;
        } else {
            self.last_state = see.state.clone();
            self.state_ticks = 0;
        }
        let pos = see.obs.pos;
        if pos.truncate().distance(player.truncate()) > 115.0 || (pos.z - player.z).abs() > 60.0 {
            return (Controls::default(), Ask::Respawn);
        }
        let st = see.state.as_str();
        // Bails and walking: get up and back on like a player.
        if st.starts_with("Wipeout") {
            self.reset_run();
            let press = self.state_ticks > 140 && self.state_ticks % 40 < 4;
            return (Controls { buttons: if press { A } else { 0 }, ..Default::default() }, Ask::None);
        }
        if st.starts_with("Biped") || st == "OffBoardPushing" {
            self.reset_run();
            if self.state_ticks > 600 {
                return (Controls::default(), Ask::Respawn);
            }
            let press = self.state_ticks > 45 && self.state_ticks % 60 < 4;
            return (Controls { buttons: if press { Y } else { 0 }, ..Default::default() }, Ask::None);
        }
        if st == "Teleporting" || st == "Sleeping" || st == "Nonspecific" {
            return (Controls::default(), Ask::None);
        }
        self.track_grind(st);
        if let Some(c) = self.queue.pop_front() {
            return (c, Ask::None);
        }
        let rolling = see.obs.rolling;
        // Stuck against something: back off to a new goal, then respawn.
        if rolling && see.speed() < 0.6 {
            self.slow += 1;
            if self.slow > 420 {
                return (Controls::default(), Ask::Respawn);
            }
            if self.slow == 150 {
                self.goal = None;
                self.mode = Mode::Cruise;
            }
        } else {
            self.slow = 0;
        }
        if !matches!(self.mode, Mode::Approach { .. } | Mode::Cruise) {
            return (Controls::default(), Ask::None);
        }
        if self.mode == Mode::Cruise && self.tick >= self.next_spot {
            self.pick_spot(see, nav);
        }
        let (dir, want_speed) = match self.mode {
            Mode::Approach { rail, from_a, since, stage } => match self.approach(see, nav, rail, from_a, since, stage) {
                Some(v) => v,
                None => {
                    self.mode = Mode::Cruise;
                    self.next_spot = self.tick + 240;
                    (self.cruise_dir(see, nav, player), self.cruise)
                }
            },
            _ => (self.cruise_dir(see, nav, player), self.cruise),
        };
        let mut c = Controls::default();
        if rolling {
            c.left[0] = steer(see.obs.forward.truncate(), dir);
        }
        // Push: Skate's A, a tap at a time, below the wanted speed.
        let speed = see.speed();
        if rolling && speed < want_speed - 0.4 && self.tick - self.last_push > 34 && c.left[0].abs() < 20000 {
            self.last_push = self.tick;
            for _ in 0..5 {
                self.queue.push_back(Controls { buttons: A, left: c.left, ..Default::default() });
            }
            c.buttons = A;
        }
        // Flat-ground tricks now and then, with room ahead.
        if self.mode == Mode::Cruise && rolling && speed > 3.0 && self.tick >= self.next_trick && self.queue.is_empty() {
            if nav.probe(pos, see.obs.forward.truncate().normalize_or_zero(), 7.0) > 0.6 {
                self.trick();
            }
            self.next_trick = self.tick + 300 + (self.rnd() * 700.0) as u64;
        }
        (c, Ask::None)
    }

    fn reset_run(&mut self) {
        self.queue.clear();
        if !matches!(self.mode, Mode::Cruise) {
            self.mode = Mode::Cruise;
            self.next_spot = self.tick + 600;
        }
    }

    fn track_grind(&mut self, st: &str) {
        let grinding = st.starts_with("Grind");
        match self.mode {
            Mode::Popped { rail, at } => {
                if grinding {
                    self.mode = Mode::Grind { rail };
                    self.log.push(format!("grind {st} on rail {rail}"));
                } else if self.tick - at > 75 {
                    self.mode = Mode::Cruise;
                    self.used.push((rail, self.tick + 1800));
                    self.next_spot = self.tick + 180;
                    self.log.push(format!("missed rail {rail}"));
                }
            }
            Mode::Grind { rail } if !grinding => {
                self.mode = Mode::Cruise;
                self.used.push((rail, self.tick + 3600));
                self.next_spot = self.tick + 360 + (self.rnd() * 600.0) as u64;
                self.next_trick = self.next_trick.max(self.tick + 90);
            }
            _ => {}
        }
    }

    fn trick(&mut self) {
        // Flick-It: crouch (right stick down), then flick.
        let pick = self.rnd();
        let (name, flick): (&str, [i16; 2]) = if pick < 0.35 {
            ("ollie", [0, FULL])
        } else if pick < 0.6 {
            ("kickflip", [-23000, 23000])
        } else if pick < 0.8 {
            ("heelflip", [23000, 23000])
        } else {
            ("shuvit", [-FULL, 0])
        };
        if self.rnd() < 0.2 {
            // Manual: a gentle hold of the right stick.
            let n = 40 + (self.rnd() * 60.0) as usize;
            for _ in 0..n {
                self.queue.push_back(Controls { right: [0, 13000], ..Default::default() });
            }
            self.log.push("manual".into());
            return;
        }
        self.pop(flick);
        self.log.push(name.into());
    }

    fn pop(&mut self, flick: [i16; 2]) {
        for _ in 0..7 {
            self.queue.push_back(Controls { right: [0, -FULL], ..Default::default() });
        }
        for _ in 0..4 {
            self.queue.push_back(Controls { right: flick, ..Default::default() });
        }
    }

    fn cruise_dir(&mut self, see: &See, nav: &Nav, player: Vec3) -> Vec2 {
        let pos = see.obs.pos;
        let reached = self.goal.is_some_and(|g| g.distance(pos.truncate()) < 6.0);
        if self.goal.is_none() || reached || self.tick > self.goal_until {
            let around = player.truncate() * 0.6 + pos.truncate() * 0.4;
            self.goal = nav.pick(&mut self.rng, around, 15.0, 60.0, pos.z).map(|p| p.truncate());
            self.goal_until = self.tick + 1800;
        }
        let goal = self.goal;
        self.route(see, nav, goal, 0.7)
    }

    /// The heading of a fan of rolls ahead that stays on rideable ground
    /// and leans toward `goal` (by `pull`).
    fn route(&mut self, see: &See, nav: &Nav, goal: Option<Vec2>, pull: f32) -> Vec2 {
        let pos = see.obs.pos;
        let fwd = see.obs.forward.truncate().normalize_or_zero();
        if self.heading == Vec2::ZERO {
            self.heading = fwd;
        }
        if self.tick % 6 == 0 || self.heading == Vec2::ZERO {
            let to_goal = goal.map(|g| (g - pos.truncate()).normalize_or_zero()).unwrap_or(fwd);
            let reach = (see.speed() * 1.4).clamp(6.0, 12.0);
            let mut best = (f32::MIN, fwd);
            for k in -6..=6 {
                let a = k as f32 * 15f32.to_radians();
                let d = Vec2::from_angle(a).rotate(fwd);
                let score = nav.probe(pos, d, reach) * 1.6 + pull * d.dot(to_goal) - 0.25 * (k as f32).abs() / 6.0
                    + 0.3 * d.dot(self.heading);
                if score > best.0 {
                    best = (score, d);
                }
            }
            self.heading = best.1;
        }
        self.heading
    }

    fn pick_spot(&mut self, see: &See, nav: &Nav) {
        self.next_spot = self.tick + 120;
        self.used.retain(|u| u.1 > self.tick);
        let pos = see.obs.pos;
        let fwd = see.obs.forward.truncate().normalize_or_zero();
        let mut best: Option<(f32, usize, bool)> = None;
        for (i, r) in nav.rails.iter().enumerate() {
            if self.used.iter().any(|u| u.0 == i) {
                continue;
            }
            for from_a in [true, false] {
                let (s, e) = if from_a { (r.a, r.b) } else { (r.b, r.a) };
                let dir = (e - s).truncate().normalize_or_zero();
                let runup = s.truncate() + r.out * RUN_OUT - dir * RUN_UP;
                let d = runup.distance(pos.truncate());
                if !(4.0..40.0).contains(&d) || (s.z - pos.z).abs() > 2.5 {
                    continue;
                }
                // Ahead of the skater, and a clear run-up on the ground.
                let toward = (runup - pos.truncate()) / d;
                if toward.dot(fwd) < 0.2 {
                    continue;
                }
                let ground = s.z - r.height;
                if nav.ground(runup, ground + 0.1).is_none() || nav.probe(Vec3::new(runup.x, runup.y, ground), dir, RUN_UP - 1.0) < 0.3 {
                    continue;
                }
                let score = d + 8.0 * (1.0 - toward.dot(fwd));
                if best.is_none_or(|b| score < b.0) {
                    best = Some((score, i, from_a));
                }
            }
        }
        if let Some((_, rail, from_a)) = best {
            if self.rnd() < 0.8 {
                self.mode = Mode::Approach { rail, from_a, since: self.tick, stage: 0 };
                self.log.push(format!("going for rail {rail} ({:.1} m high)", nav.rails[rail].height));
            } else {
                self.used.push((rail, self.tick + 900));
            }
        }
    }

    /// Heading and speed toward the rail's run-up, along its line, and the
    /// pop at the lip. None: the run went wrong (give up).
    fn approach(&mut self, see: &See, nav: &Nav, rail: usize, from_a: bool, since: u64, stage: u8) -> Option<(Vec2, f32)> {
        let r = *nav.rails.get(rail)?;
        let give_up = |b: &mut Self, why: String| {
            b.used.push((rail, b.tick + 1800));
            b.log.push(format!("rail {rail}: {why}"));
            None
        };
        if self.tick - since > 1500 {
            return give_up(self, "run-up timed out".into());
        }
        let (s, e) = if from_a { (r.a, r.b) } else { (r.b, r.a) };
        let len = (e - s).truncate().length();
        let dir = (e - s).truncate() / len;
        let pos = see.obs.pos.truncate();
        let along = (pos - s.truncate()).dot(dir);
        let lateral = (pos - s.truncate()).dot(r.out);
        // The line in (measured on stock Skate, `examples/npc_brain.rs sweep`):
        // about 8 degrees toward the lip from 1.6 m out, meeting it 2 m in;
        // popped 1.2-3.4 m before the lip at 4.5-6.5 m/s, Skate's grind lock
        // catches it almost every time.
        let (lane_from, lane_to) = (s.truncate() + r.out * RUN_OUT - dir * RUN_UP, s.truncate() + dir * 2.0);
        let lane = (lane_to - lane_from).normalize_or_zero();
        let set = |b: &mut Self, stage: u8| b.mode = Mode::Approach { rail, from_a, since, stage };
        match stage {
            0 => {
                // Behind the run-up first, so the skater arrives facing down the line.
                let pre = lane_from - lane * 5.0;
                if pos.distance(pre) < 2.5 {
                    set(self, 1);
                }
                Some((self.route(see, nav, Some(pre), 1.6), APPROACH_SPEED))
            }
            1 => {
                let fwd = see.obs.forward.truncate().normalize_or_zero();
                if (pos - lane_from).dot(lane) > -1.0 {
                    let lined_up = fwd.dot(lane) > 0.8 && pos.distance(lane_from) < 2.5;
                    set(self, if lined_up { 2 } else { 0 });
                }
                Some(((lane_from + lane * 1.0 - pos).normalize_or_zero(), APPROACH_SPEED))
            }
            _ => {
                if along > -0.5 || lateral < 0.0 || lateral > 2.5 {
                    return give_up(self, format!("off the line ({along:.1} m along, {lateral:.1} m out)"));
                }
                let speed = see.speed();
                // The flick comes 7 ticks after the crouch starts.
                let lead = 1.8 + speed * 7.0 / 60.0;
                if along > -lead && see.obs.rolling && speed > 3.0 && (0.05..1.4).contains(&lateral) {
                    self.pop([0, FULL]);
                    self.mode = Mode::Popped { rail, at: self.tick };
                    self.log.push(format!("pop at {along:.2} m, {lateral:.2} m out, {speed:.1} m/s"));
                }
                let ahead = (pos - lane_from).dot(lane) + 3.0;
                let target = lane_from + lane * ahead;
                Some(((target - pos).normalize_or_zero(), APPROACH_SPEED))
            }
        }
    }
}

/// Left stick X steering `forward` toward `want` (positive steers right).
pub fn steer(forward: Vec2, want: Vec2) -> i16 {
    if forward.length_squared() < 1e-6 || want.length_squared() < 1e-6 {
        return 0;
    }
    let f = forward.normalize();
    let right = Vec2::new(f.y, -f.x);
    let error = want.dot(right).atan2(want.dot(f)).to_degrees();
    if error.abs() < 2.0 {
        return 0;
    }
    let lx = (error.abs() * 1100.0).clamp(17000.0, 30000.0);
    (lx * error.signum()) as i16
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::coords::to_skate;

    fn quad(tris: &mut Vec<[[f32; 3]; 3]>, tags: &mut Vec<u32>, a: Vec3, b: Vec3, c: Vec3, d: Vec3, tag: u32) {
        let s = |v: Vec3| to_skate(v).to_array();
        tris.push([s(a), s(b), s(c)]);
        tris.push([s(a), s(c), s(d)]);
        tags.extend([tag, tag]);
    }

    #[test]
    fn sidewalk_beats_road_and_walls_block() {
        let (mut t, mut g) = (Vec::new(), Vec::new());
        // Road x < 0, sidewalk x >= 0 (0.15 m curb), a wall at y = 10 on the sidewalk.
        quad(&mut t, &mut g, Vec3::new(-20., -20., 0.), Vec3::new(0., -20., 0.), Vec3::new(0., 20., 0.), Vec3::new(-20., 20., 0.), 1);
        quad(&mut t, &mut g, Vec3::new(0., -20., 0.15), Vec3::new(20., -20., 0.15), Vec3::new(20., 20., 0.15), Vec3::new(0., 20., 0.15), 3);
        quad(&mut t, &mut g, Vec3::new(0., 10., 0.15), Vec3::new(20., 10., 0.15), Vec3::new(20., 10., 3.), Vec3::new(0., 10., 3.), 3);
        let nav = Nav::build(Vec3::ZERO, 30.0, &t, &g, &[]);
        assert_eq!(nav.ground(Vec2::new(5., 0.), 0.15).unwrap().1, 2);
        assert_eq!(nav.ground(Vec2::new(-5., 0.), 0.0).unwrap().1, 1);
        let from = Vec3::new(5., 0., 0.15);
        assert!(nav.probe(from, Vec2::Y, 8.0) > 0.9);
        assert!(nav.probe(Vec3::new(1., 0., 0.15), -Vec2::X, 8.0) < 0.6);
        // The wall at y = 10 stops a run toward it.
        assert!(nav.probe(Vec3::new(5., 6., 0.15), Vec2::Y, 8.0) < 0.);
    }

    #[test]
    fn steering_signs() {
        assert!(steer(Vec2::Y, Vec2::X) > 0);
        assert!(steer(Vec2::Y, -Vec2::X) < 0);
        assert_eq!(steer(Vec2::Y, Vec2::Y), 0);
    }
}
