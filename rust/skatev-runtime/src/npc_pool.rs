//! NPC skaters' sessions: a few full Skate sessions on their own thread,
//! each ridden by `npc::Brain` near the player and posed onto a GTA ped the
//! host spawns (`sv_npc_*` in lib.rs).
//!
//! Measured offline (`examples/npc_cost.rs`): a session costs about 26 MB and
//! 0.6 ms a tick; building one takes about ten seconds, so they are built one
//! at a time on a helper thread and kept for the process.
//!
//! The sessions ride the player's collision area: the player's builder hands
//! each new area here (`offer_area`), and every NPC session installs its own
//! copy through its collision builder. NPCs ride Skate's stock tuning (the
//! tuning tool's `skater-collections.original.json` when present), so a
//! player's pop or speed multipliers stay theirs.
use crate::coords;
use crate::npc::{Ask, Brain, Nav, See};
use crate::ped;
use bevy_math::{Mat4, Vec2, Vec3};
use skate_host::bridge::{CollisionBuilder, PreparedCollision, Session};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, mpsc};

pub const MAX: usize = 4;

/// GTA ped models NPC skaters wear, tried in a shuffled order; the ones the
/// ped cache has with a human skeleton are used.
const MODELS: &[&str] = &[
    "a_m_y_skater_01", "a_m_y_skater_02", "a_f_y_skater_01", "a_m_m_skater_01",
    "a_m_y_hipster_01", "a_m_y_hipster_02", "a_m_y_hipster_03", "a_f_y_hipster_01",
    "a_f_y_hipster_02", "a_f_y_hipster_03", "a_f_y_hipster_04", "a_m_y_beach_01",
    "a_m_y_beach_02", "a_m_y_beach_03", "a_m_y_stbla_01", "a_m_y_stbla_02",
    "a_m_y_stwhi_01", "a_m_y_stwhi_02", "a_m_y_genstreet_01", "a_m_y_genstreet_02",
    "a_m_y_soucent_01", "a_m_y_soucent_02", "a_m_y_soucent_04", "a_m_y_latino_01",
    "a_m_y_surfer_01", "a_m_y_vinewood_01", "a_m_y_vinewood_03", "a_f_y_vinewood_04",
    "a_m_y_downtown_01", "a_m_y_eastsa_02", "a_f_y_genhot_01", "a_m_y_polynesian_01",
];

/// The area the player's session last built: its GTA centre, and the
/// Skate-space buffers it was built from.
pub struct AreaData {
    pub centre: Vec3,
    pub triangles: Vec<[[f32; 3]; 3]>,
    pub tags: Vec<u32>,
    pub rails: Vec<Vec<[f32; 3]>>,
}

static WANTED: AtomicBool = AtomicBool::new(false);
static AREA_GEN: AtomicU64 = AtomicU64::new(0);
static AREA: Mutex<Option<Arc<AreaData>>> = Mutex::new(None);

/// The player's collision builder, after building an area: keeps a copy for
/// the NPC sessions when any are wanted (a clone of the area's buffers).
pub fn offer_area(centre: Vec3, triangles: &[[[f32; 3]; 3]], tags: &[u32], rails: &[Vec<[f32; 3]>]) {
    if !WANTED.load(Ordering::Relaxed) || triangles.is_empty() {
        return;
    }
    let area = Arc::new(AreaData { centre, triangles: triangles.to_vec(), tags: tags.to_vec(), rails: rails.to_vec() });
    *AREA.lock().unwrap() = Some(area);
    AREA_GEN.fetch_add(1, Ordering::Release);
}

/// What the host reads of one NPC.
#[derive(Clone, Default)]
pub struct View {
    pub active: bool,
    pub model: u32,
    /// Bumped on every (re)spawn: the host snaps its ped instead of moving it.
    pub generation: u32,
    pub tick: u64,
    pub position: Vec3,
    pub heading: f32,
    pub velocity: Vec3,
    pub state: String,
    /// GTA bone world matrices in the model's bone order.
    pub pose: Arc<Vec<Mat4>>,
    pub board: Option<([Mat4; 7], Mat4)>,
}

enum Msg {
    Configure { count: usize, cache_root: PathBuf },
    Frame { dt: f32, player: Vec3, active: bool },
}

pub struct Pool {
    jobs: mpsc::Sender<Msg>,
    pub views: Arc<Mutex<Vec<View>>>,
}

type Log = Arc<dyn Fn(&str) + Send + Sync>;

impl Pool {
    pub fn start(data_root: PathBuf, log: Log) -> Result<Self, String> {
        let (jobs, rx) = mpsc::channel();
        let views = Arc::new(Mutex::new(vec![View::default(); MAX]));
        let out = Arc::clone(&views);
        std::thread::Builder::new()
            .name("skatev-npc".into())
            .stack_size(16 * 1024 * 1024)
            .spawn(move || {
                let mut w = Worker::new(data_root, out, log.clone());
                let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| w.run(rx)));
                if r.is_err() {
                    log("npc skaters: worker panicked; NPC skaters are off for this session");
                }
                WANTED.store(false, Ordering::Relaxed);
            })
            .map_err(|e| e.to_string())?;
        Ok(Self { jobs, views })
    }
    pub fn configure(&self, count: usize, cache_root: PathBuf) -> bool {
        WANTED.store(count > 0, Ordering::Relaxed);
        self.jobs.send(Msg::Configure { count: count.min(MAX), cache_root }).is_ok()
    }
    pub fn frame(&self, dt: f32, player: Vec3, active: bool) -> bool {
        self.jobs.send(Msg::Frame { dt, player, active }).is_ok()
    }
}

/// Stock tuning for NPC sessions, on the calling thread.
fn stock_tuning(root: &Path) {
    let original = root.join("private/stock/skater-collections.original.json");
    skate_data::collections::set_thread_override(original.exists().then_some(original));
}

struct Character {
    model: ped::PedModel,
    hash: u32,
}

struct Slot {
    session: Session,
    builder: CollisionBuilder,
    brain: Brain,
    /// AREA_GEN of the collision installed (0: none yet).
    area: u64,
    spawned: bool,
    generation: u32,
    character: Option<Character>,
}

struct Worker {
    root: PathBuf,
    views: Arc<Mutex<Vec<View>>>,
    log: Log,
    count: usize,
    cache_root: Option<PathBuf>,
    slots: Vec<Slot>,
    building: Option<mpsc::Receiver<Result<Session, String>>>,
    build_failed: bool,
    installs: mpsc::Sender<(usize, u64, Arc<AreaData>, CollisionBuilder)>,
    installed: mpsc::Receiver<(usize, u64, Result<PreparedCollision, String>)>,
    pending_install: Vec<bool>,
    area: Option<(u64, Arc<AreaData>)>,
    nav: Option<Nav>,
    /// A street map being built off the riding thread.
    mapping: Option<mpsc::Receiver<(u64, Arc<AreaData>, Nav, u128)>>,
    /// AREA_GEN of the map in progress or last tried.
    mapped: u64,
    mesh: Option<Arc<crate::skin::SkinMesh>>,
    joints: Option<Vec<Option<usize>>>,
    rng: u64,
    acc: f32,
    player: Vec3,
    active: bool,
    models: Vec<(u32, PathBuf)>,
}

impl Worker {
    fn new(root: PathBuf, views: Arc<Mutex<Vec<View>>>, log: Log) -> Self {
        // Collision installs run beside the ticks (one area build per session).
        let (installs, jobs) = mpsc::channel::<(usize, u64, Arc<AreaData>, CollisionBuilder)>();
        let (done, installed) = mpsc::channel();
        let _ = std::thread::Builder::new()
            .name("skatev-npc-collision".into())
            .stack_size(16 * 1024 * 1024)
            .spawn(move || {
                while let Ok((slot, generation, area, builder)) = jobs.recv() {
                    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        builder.build_tagged(area.triangles.clone(), area.tags.clone(), area.rails.clone())
                    }))
                    .unwrap_or_else(|_| Err("npc collision build panicked".into()));
                    if done.send((slot, generation, r)).is_err() {
                        break;
                    }
                }
            });
        let seed = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0x5eed, |d| d.as_nanos() as u64)
            | 1;
        Self {
            root,
            views,
            log,
            count: 0,
            cache_root: None,
            slots: Vec::new(),
            building: None,
            build_failed: false,
            installs,
            installed,
            pending_install: Vec::new(),
            area: None,
            nav: None,
            mapping: None,
            mapped: 0,
            mesh: None,
            joints: None,
            rng: seed,
            acc: 0.0,
            player: Vec3::ZERO,
            active: false,
            models: Vec::new(),
        }
    }

    fn run(&mut self, rx: mpsc::Receiver<Msg>) {
        stock_tuning(&self.root);
        loop {
            match rx.recv_timeout(std::time::Duration::from_millis(100)) {
                Ok(m) => self.handle(m),
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => return,
            }
            while let Ok(m) = rx.try_recv() {
                self.handle(m);
            }
            self.update();
        }
    }

    fn handle(&mut self, m: Msg) {
        match m {
            Msg::Configure { count, cache_root } => {
                if count != self.count {
                    (self.log)(&format!("npc skaters: {count} wanted"));
                }
                self.count = count;
                if self.cache_root.as_ref() != Some(&cache_root) {
                    self.models = MODELS
                        .iter()
                        .filter_map(|m| {
                            let dir = cache_root.join(m);
                            dir.join("skeleton.json").is_file().then(|| (ped::joaat(m), dir))
                        })
                        .collect();
                    (self.log)(&format!("npc skaters: {} of {} ped models in the ped cache", self.models.len(), MODELS.len()));
                    self.cache_root = Some(cache_root);
                }
            }
            Msg::Frame { dt, player, active } => {
                self.player = player;
                self.active = active && self.count > 0;
                if self.active && dt.is_finite() {
                    self.acc = (self.acc + dt.clamp(0.0, 0.25)).min(0.1);
                }
            }
        }
    }

    fn update(&mut self) {
        self.take_area();
        self.grow();
        self.take_installs();
        if !self.active {
            for s in &mut self.slots {
                s.spawned = false;
            }
            self.acc = 0.0;
            self.publish();
            return;
        }
        let Some(period) = self.slots.first().map(|s| s.session.period()) else {
            self.publish();
            return;
        };
        let mut ticks = 0;
        while self.acc >= period && ticks < 4 {
            self.acc -= period;
            ticks += 1;
            for i in 0..self.slots.len().min(self.count) {
                self.step(i);
            }
        }
        if ticks > 0 {
            self.publish();
        }
    }

    fn take_area(&mut self) {
        if let Some(rx) = &self.mapping {
            match rx.try_recv() {
                Ok((generation, area, nav, ms)) => {
                    (self.log)(&format!(
                        "npc skaters: area around ({:.0}, {:.0}): {} grind spots, map built in {ms}ms",
                        area.centre.x,
                        area.centre.y,
                        nav.rails.len()
                    ));
                    self.mapping = None;
                    self.nav = Some(nav);
                    self.area = Some((generation, area));
                    for i in 0..self.slots.len() {
                        self.request_install(i);
                    }
                }
                Err(mpsc::TryRecvError::Empty) => return,
                Err(mpsc::TryRecvError::Disconnected) => self.mapping = None,
            }
        }
        let generation = AREA_GEN.load(Ordering::Acquire);
        if generation == 0 || generation == self.mapped {
            return;
        }
        let Some(area) = AREA.lock().unwrap().clone() else { return };
        self.mapped = generation;
        let (tx, rx) = mpsc::channel();
        let started = std::thread::Builder::new()
            .name("skatev-npc-map".into())
            .spawn(move || {
                let t = std::time::Instant::now();
                let nav = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    Nav::build(area.centre, crate::world::RADIUS, &area.triangles, &area.tags, &area.rails)
                }));
                if let Ok(nav) = nav {
                    let _ = tx.send((generation, area, nav, t.elapsed().as_millis()));
                }
            });
        if started.is_ok() {
            self.mapping = Some(rx);
        }
    }

    fn request_install(&mut self, i: usize) {
        let Some((generation, area)) = self.area.clone() else { return };
        if self.pending_install.get(i).copied().unwrap_or(false) || self.slots[i].area == generation {
            return;
        }
        if self.installs.send((i, generation, area, self.slots[i].builder)).is_ok() {
            self.pending_install[i] = true;
        }
    }

    fn take_installs(&mut self) {
        while let Ok((i, generation, r)) = self.installed.try_recv() {
            self.pending_install[i] = false;
            match r {
                Ok(prepared) => match self.slots[i].session.install_collision(prepared) {
                    Ok(()) => self.slots[i].area = generation,
                    Err(e) => (self.log)(&format!("npc skaters: collision install failed: {e}")),
                },
                Err(e) => (self.log)(&format!("npc skaters: collision build failed: {e}")),
            }
            // A newer area arrived meanwhile.
            self.request_install(i);
        }
    }

    /// Builds sessions one at a time up to the wanted count.
    fn grow(&mut self) {
        if let Some(rx) = &self.building {
            match rx.try_recv() {
                Ok(Ok(session)) => {
                    let builder = session.collision_builder();
                    let i = self.slots.len();
                    self.slots.push(Slot {
                        session,
                        builder,
                        brain: Brain::new(self.rng.wrapping_add(i as u64 * 7919)),
                        area: 0,
                        spawned: false,
                        generation: 0,
                        character: None,
                    });
                    self.pending_install.push(false);
                    self.building = None;
                    (self.log)(&format!("npc skaters: session {} ready", i + 1));
                    self.request_install(i);
                }
                Ok(Err(e)) => {
                    (self.log)(&format!("npc skaters: session build failed: {e}"));
                    self.building = None;
                    self.build_failed = true;
                }
                Err(mpsc::TryRecvError::Empty) => {}
                Err(mpsc::TryRecvError::Disconnected) => {
                    self.building = None;
                    self.build_failed = true;
                }
            }
            return;
        }
        if self.build_failed || self.slots.len() >= self.count || self.area.is_none() {
            return;
        }
        if self.mesh.is_none() {
            let path = self.root.join("private").join("skater.glb");
            match crate::skin::SkinMesh::load(&path, 0) {
                Ok(m) => self.mesh = Some(Arc::new(m)),
                Err(e) => {
                    (self.log)(&format!("npc skaters: skater mesh unavailable ({e}); off"));
                    self.build_failed = true;
                    return;
                }
            }
        }
        let (tx, rx) = mpsc::channel();
        let root = self.root.clone();
        let started = std::thread::Builder::new()
            .name("skatev-npc-build".into())
            .stack_size(16 * 1024 * 1024)
            .spawn(move || {
                stock_tuning(&root);
                let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| build_session(&root)))
                    .unwrap_or_else(|_| Err("session build panicked".into()));
                let _ = tx.send(r);
            });
        match started {
            Ok(_) => self.building = Some(rx),
            Err(e) => {
                (self.log)(&format!("npc skaters: no build thread ({e})"));
                self.build_failed = true;
            }
        }
    }

    fn step(&mut self, i: usize) {
        if self.nav.is_none() || self.slots[i].area == 0 {
            return;
        }
        if !self.slots[i].spawned {
            self.spawn(i);
            return;
        }
        let nav = self.nav.as_ref().unwrap();
        let slot = &mut self.slots[i];
        let pose = slot.session.pose();
        let see = See::from_pose(&pose);
        let (c, mut ask) = slot.brain.next(&see, nav, self.player);
        // Off the installed area (the player moved on): start again near them.
        if see.obs.pos.truncate().distance(nav.center.truncate()) > crate::world::RADIUS - 20.0 {
            ask = Ask::Respawn;
        }
        for l in slot.brain.log.drain(..) {
            if crate::worker::VERBOSE_LOG.load(Ordering::Relaxed) {
                (self.log)(&format!("npc skater {}: {l}", i + 1));
            }
        }
        if ask == Ask::Respawn {
            slot.spawned = false;
            return;
        }
        if let Err(e) = slot.session.tick(c) {
            (self.log)(&format!("npc skater {}: tick failed ({e}); respawning", i + 1));
            slot.spawned = false;
        }
    }

    /// Puts slot `i`'s skater on a sidewalk 30-70 m from the player, wearing
    /// a model the host can create.
    fn spawn(&mut self, i: usize) {
        let Some(nav) = self.nav.as_ref() else { return };
        let others: Vec<Vec2> = self
            .slots
            .iter()
            .enumerate()
            .filter(|(j, s)| *j != i && s.spawned)
            .map(|(_, s)| crate::line::Obs::from_pose(&s.session.pose()).pos.truncate())
            .collect();
        let mut at = None;
        for _ in 0..6 {
            if let Some(p) = nav.pick(&mut self.rng, self.player.truncate(), 30.0, 70.0, self.player.z) {
                if others.iter().all(|o| o.distance(p.truncate()) > 12.0) {
                    at = Some(p);
                    break;
                }
            }
        }
        let Some(at) = at else { return };
        // Face the most open way.
        let mut best = (f32::MIN, Vec2::X);
        for k in 0..12 {
            let d = Vec2::from_angle(k as f32 * std::f32::consts::TAU / 12.0);
            let s = nav.probe(at, d, 12.0);
            if s > best.0 {
                best = (s, d);
            }
        }
        let heading = coords::gta_heading(Vec3::new(best.1.x, best.1.y, 0.0));
        if self.slots[i].character.is_none() || crate::npc::rand(&mut self.rng) < 0.5 {
            self.slots[i].character = self.pick_character();
        }
        if self.slots[i].character.is_none() {
            return;
        }
        let slot = &mut self.slots[i];
        let spawn = coords::to_skate(at + Vec3::Z * 0.05).to_array();
        match slot.session.activate(spawn, coords::skate_heading(heading)) {
            Ok(_) => {
                slot.spawned = true;
                slot.generation = slot.generation.wrapping_add(1);
                slot.brain.reset();
            }
            Err(e) => (self.log)(&format!("npc skater {}: spawn failed ({e})", i + 1)),
        }
    }

    fn pick_character(&mut self) -> Option<Character> {
        let mesh = self.mesh.clone()?;
        for _ in 0..6 {
            if self.models.is_empty() {
                return None;
            }
            let k = (crate::npc::rand(&mut self.rng) * self.models.len() as f32) as usize % self.models.len();
            let (hash, dir) = self.models[k].clone();
            match ped::PedModel::load(&dir, &ped::Variation::default(), 0).and_then(|mut m| m.calibrate_to(&mesh).map(|_| m)) {
                Ok(model) => return Some(Character { model, hash }),
                Err(e) => {
                    (self.log)(&format!("npc skaters: {} unusable ({e})", dir.display()));
                    self.models.remove(k);
                }
            }
        }
        None
    }

    fn publish(&mut self) {
        let mut views = vec![View::default(); MAX];
        let mesh = self.mesh.clone();
        for (i, slot) in self.slots.iter().enumerate().take(MAX) {
            if !(self.active && slot.spawned && i < self.count) {
                views[i].generation = slot.generation;
                continue;
            }
            let (Some(mesh), Some(ch)) = (mesh.as_ref(), slot.character.as_ref()) else { continue };
            let p = slot.session.pose();
            if self.joints.is_none() {
                self.joints = Some(mesh.joint_names.iter().map(|n| p.names.iter().position(|x| x == n)).collect());
            }
            let pose = ped_pose(mesh, self.joints.as_ref().unwrap(), &ch.model, &p);
            let board = crate::board_pose::world(&p.names, &p.bones, p.root).and_then(|mut w| {
                crate::board_pose::present_wheels(&mut w, p.tick);
                let inverse = mesh.inverse_bind_for("SKATEBOARD_ROOT")?;
                let c = Mat4::from_mat3(coords::basis());
                Some((w, w[0] * c * inverse * c.transpose()))
            });
            let obs = crate::line::Obs::from_pose(&p);
            let (root, _) = coords::transform_from_skate(p.root);
            views[i] = View {
                active: pose.is_some(),
                model: ch.hash,
                generation: slot.generation,
                tick: p.tick,
                position: root,
                heading: coords::gta_heading(obs.forward),
                velocity: coords::from_skate(p.velocity),
                state: p.state.clone(),
                pose: Arc::new(pose.unwrap_or_default()),
                board,
            };
        }
        *self.views.lock().unwrap() = views;
    }
}

/// A session on a floor far from the map, warmed like the player's preload;
/// the area goes in through its collision builder before it spawns.
fn build_session(root: &Path) -> Result<Session, String> {
    let at = coords::to_skate(Vec3::new(1.0e6, 1.0e6, 0.0));
    let h = 2.0;
    let corner = |x: f32, z: f32| [at.x + x, at.y, at.z + z];
    let floor = vec![[corner(-h, -h), corner(-h, h), corner(h, h)], [corner(-h, -h), corner(h, h), corner(h, -h)]];
    let mut s = Session::new_tagged(root, floor, vec![0; 2], Vec::new(), at.to_array(), 0.0)?;
    s.activate(at.to_array(), 0.0)?;
    for _ in 0..30 {
        s.tick(Default::default())?;
    }
    Ok(s)
}

/// Skate's pose on a GTA ped model (as worker::Presenter::draw does for the
/// player): skinning joints from Skate's bones, then the ped retarget.
fn ped_pose(mesh: &crate::skin::SkinMesh, joints: &[Option<usize>], model: &ped::PedModel, p: &skate_host::bridge::Pose) -> Option<Vec<Mat4>> {
    // The GLB's bone-local axes are rotated -90 deg about X from Skate's.
    let blender = Mat4::from_cols(bevy_math::Vec4::X, -bevy_math::Vec4::Z, bevy_math::Vec4::Y, bevy_math::Vec4::W);
    let world: Vec<Option<Mat4>> = joints
        .iter()
        .map(|i| i.and_then(|i| p.bones.get(i).map(|b| p.root * *b * blender)))
        .collect();
    let skate = model.skate_pose(mesh, &world);
    model.pose_with_foot_support(&skate)
}
