//! Skate 3's session-marker quick menu, shown while LB is held: Return
//! (d-pad up), Place (d-pad down) and Object Dropper (B; the host drops
//! GTA's skate-park props).
//!
//! The menu is the donor's pre-compiled display list
//! (`tools/extract_session_marker.py`, exported by convert-skate-data.py to
//! `<data_root>/private/session-marker`): `hud.json` schema 1, fifteen
//! triangle meshes on the 1280x720 canvas over seven RGBA files. As in the
//! donor (`skate-game/src/session_marker/hud.rs`) it is drawn as authored
//! while the menu is up; no ActionScript runs.
//!
//! SkateV presentation choices (not recovered native behaviour):
//! - Return and Place show at the Object Dropper's unavailable opacity while
//!   the marker would refuse them (no marker set; not placeable here).
//! - The Object Dropper row is authored at that opacity (Skate 3's dropper
//!   is not in the donor); it shows opaque while the host can drop.
//! - The menu keeps its 16:9 distance from the HUD region's left edge
//!   (`Screen_EdgeOffset`) and moves down below the TRAX banner when that
//!   is on screen (both sit top-left; LB + d-pad left/right changes station).
use crate::layout::Layout;
use crate::player::{DrawCmd, Frame, Texture, Vertex};
use serde::Deserialize;
use std::path::{Path, PathBuf};

/// The Object Dropper's authored opacity, used for unavailable rows.
const UNAVAILABLE_ALPHA: f32 = 0.3;

#[derive(Deserialize)]
struct Manifest {
    version: u32,
    canvas: [f32; 2],
    textures: Vec<TextureFile>,
    meshes: Vec<Mesh>,
}

#[derive(Deserialize)]
struct TextureFile {
    file: String,
    width: u32,
    height: u32,
}

#[derive(Deserialize)]
struct Mesh {
    texture: Option<usize>,
    #[serde(default)]
    role: String,
    color: [f32; 4],
    vertices: Vec<MeshVertex>,
}

#[derive(Deserialize)]
struct MeshVertex {
    position: [f32; 2],
    uv: [f32; 2],
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Row {
    /// The panel.
    Art,
    Return,
    Place,
    Dropper,
}

struct Piece {
    row: Row,
    texture: u32,
    color: [f32; 4],
    /// Canvas units and texture coordinates.
    vertices: Vec<Vertex>,
}

/// What the menu shows (Skate's `SessionMarker` after the last tick).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct MarkerState {
    pub visible: bool,
    pub can_place: bool,
    pub can_return: bool,
    pub can_drop: bool,
}

/// The loaded menu.
pub struct MarkerHud {
    /// RGBA8, straight alpha, sRGB colour; a white texel follows the
    /// manifest's files when a mesh is untextured.
    pub textures: Vec<Texture>,
    pieces: Vec<Piece>,
    /// Canvas bounds of every mesh: x0, y0, x1, y1.
    bounds: [f32; 4],
}

impl MarkerHud {
    /// `<data_root>/private/session-marker` (convert-skate-data.py `hud`).
    pub fn dir_for(data_root: &Path) -> PathBuf {
        data_root.join("private").join("session-marker")
    }

    pub fn load(dir: &Path) -> Result<Self, String> {
        let path = dir.join("hud.json");
        let bytes = std::fs::read(&path).map_err(|e| format!("{}: {e}", path.display()))?;
        Self::parse(&bytes, |file| std::fs::read(dir.join(file)).map_err(|e| format!("{file}: {e}")))
    }

    fn parse(json: &[u8], read: impl Fn(&str) -> Result<Vec<u8>, String>) -> Result<Self, String> {
        let m: Manifest = serde_json::from_slice(json).map_err(|e| format!("session-marker hud.json: {e}"))?;
        if m.version != 1 || m.canvas != [1280.0, 720.0] {
            return Err("unsupported session-marker hud.json".into());
        }
        let mut textures = Vec::with_capacity(m.textures.len() + 1);
        for t in &m.textures {
            let file = Path::new(&t.file);
            if file.components().count() != 1 || !matches!(file.components().next(), Some(std::path::Component::Normal(_))) {
                return Err(format!("session-marker texture path {}", t.file));
            }
            let rgba = read(&t.file)?;
            if t.width == 0 || t.height == 0 || rgba.len() != t.width as usize * t.height as usize * 4 {
                return Err(format!("invalid session-marker texture size {}", t.file));
            }
            textures.push(Texture { name: format!("session-marker/{}", t.file), width: t.width, height: t.height, rgba });
        }
        let white = textures.len() as u32;
        let mut pieces = Vec::with_capacity(m.meshes.len());
        let mut bounds = [f32::MAX, f32::MAX, f32::MIN, f32::MIN];
        for mesh in m.meshes {
            let finite = mesh.vertices.iter().all(|v| v.position.iter().chain(&v.uv).all(|x| x.is_finite()));
            if mesh.vertices.len() % 3 != 0 || !finite || mesh.color.iter().any(|c| !c.is_finite()) {
                return Err("invalid session-marker geometry".into());
            }
            let texture = match mesh.texture {
                Some(t) if t < m.textures.len() => t as u32,
                Some(_) => return Err("session-marker mesh texture out of range".into()),
                None => white,
            };
            let row = match mesh.role.as_str() {
                "return" => Row::Return,
                "place" => Row::Place,
                "dropper" => Row::Dropper,
                _ => Row::Art,
            };
            let vertices: Vec<Vertex> = mesh
                .vertices
                .iter()
                .map(|v| Vertex { x: v.position[0], y: v.position[1], u: v.uv[0], v: v.uv[1] })
                .collect();
            for v in &vertices {
                bounds = [bounds[0].min(v.x), bounds[1].min(v.y), bounds[2].max(v.x), bounds[3].max(v.y)];
            }
            pieces.push(Piece { row, texture, color: mesh.color, vertices });
        }
        if pieces.is_empty() {
            return Err("session-marker hud.json has no meshes".into());
        }
        if pieces.iter().any(|p| p.texture == white) {
            textures.push(Texture { name: "session-marker/white".into(), width: 1, height: 1, rgba: vec![255; 4] });
        }
        Ok(Self { textures, pieces, bounds })
    }

    /// The menu in `layout`'s pixels; nothing unless `state.visible`. Its top
    /// moves down to `below` (output pixels) when it would start above it.
    pub fn frame(&self, state: MarkerState, layout: &Layout, below: Option<f32>) -> Frame {
        let mut frame = Frame { layout: Some(*layout), ..Default::default() };
        if !state.visible {
            return frame;
        }
        let place = |x: f32, y: f32| layout.to_pixels([x + layout.edge_offset, y]);
        let top = place(self.bounds[0], self.bounds[1])[1];
        let dy = below.map_or(0.0, |b| (b - top).max(0.0));
        for p in &self.pieces {
            let available = match p.row {
                Row::Return => state.can_return,
                Row::Place => state.can_place,
                Row::Dropper => state.can_drop,
                Row::Art => true,
            };
            let mut multiply = p.color;
            if !available {
                multiply[3] = multiply[3].min(UNAVAILABLE_ALPHA);
            } else if p.row == Row::Dropper {
                multiply[3] = 1.0;
            }
            let first = frame.vertices.len() as u32;
            frame.vertices.extend(p.vertices.iter().map(|v| {
                let [x, y] = place(v.x, v.y);
                Vertex { x, y: y + dy, u: v.u, v: v.v }
            }));
            frame.draws.push(DrawCmd { texture: p.texture, first, count: p.vertices.len() as u32, multiply, add: [0.0; 4] });
        }
        frame
    }
}

/// Bottom edge (output pixels) of the draws in `frame` that show at all:
/// not transparent and at least partly on screen.
pub fn visible_bottom(frame: &Frame) -> Option<f32> {
    let (w, h) = frame.layout.map_or((f32::MAX, f32::MAX), |l| (l.width as f32, l.height as f32));
    frame
        .draws
        .iter()
        .filter(|d| d.multiply[3] > 0.004 || d.add[3] > 0.004)
        .map(|d| &frame.vertices[d.first as usize..(d.first + d.count) as usize])
        .filter(|vs| {
            let (x0, x1) = vs.iter().fold((f32::MAX, f32::MIN), |(a, b), v| (a.min(v.x), b.max(v.x)));
            let (y0, y1) = vs.iter().fold((f32::MAX, f32::MIN), |(a, b), v| (a.min(v.y), b.max(v.y)));
            x1 > 0.0 && x0 < w && y1 > 0.0 && y0 < h
        })
        .flat_map(|vs| vs.iter().map(|v| v.y))
        .reduce(f32::max)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::layout::DEFAULT_MAX_ASPECT;

    fn quad(x0: f32, y0: f32, x1: f32, y1: f32) -> String {
        let v = |x: f32, y: f32| format!(r#"{{"position":[{x},{y}],"uv":[0,0]}}"#);
        [v(x0, y0), v(x1, y0), v(x1, y1), v(x0, y0), v(x1, y1), v(x0, y1)].join(",")
    }

    fn sample() -> MarkerHud {
        let json = format!(
            r#"{{"version":1,"canvas":[1280,720],"textures":[{{"file":"t.rgba","width":1,"height":1}}],
            "meshes":[{{"texture":0,"role":"art","color":[1,1,1,0.75],"vertices":[{}]}},
                      {{"texture":0,"role":"return","color":[1,1,1,1],"vertices":[{}]}},
                      {{"texture":0,"role":"place","color":[1,1,1,1],"vertices":[{}]}},
                      {{"texture":0,"role":"dropper","color":[1,1,1,0.3],"vertices":[{}]}}]}}"#,
            quad(91.0, 48.0, 155.0, 183.0),
            quad(99.0, 54.0, 163.0, 118.0),
            quad(99.0, 91.0, 163.0, 155.0),
            quad(99.0, 130.0, 163.0, 194.0)
        );
        MarkerHud::parse(json.as_bytes(), |_| Ok(vec![0; 4])).unwrap()
    }

    #[test]
    fn hidden_draws_nothing() {
        let l = Layout::compute(1920, 1080, 1.0, DEFAULT_MAX_ASPECT);
        let f = sample().frame(MarkerState::default(), &l, None);
        assert!(f.draws.is_empty() && f.layout.is_some());
    }

    #[test]
    fn authored_position_at_sixteen_by_nine() {
        let l = Layout::compute(1920, 1080, 1.0, DEFAULT_MAX_ASPECT);
        let f = sample().frame(MarkerState { visible: true, can_place: true, can_return: true, can_drop: false }, &l, None);
        assert_eq!(f.draws.len(), 4);
        assert_eq!((f.vertices[0].x, f.vertices[0].y), (136.5, 72.0));
        assert_eq!(f.draws.iter().map(|d| d.multiply[3]).collect::<Vec<_>>(), [0.75, 1.0, 1.0, 0.3]);
        // With the host's dropper the authored 0.3 row shows opaque.
        let f = sample().frame(MarkerState { visible: true, can_place: true, can_return: true, can_drop: true }, &l, None);
        assert_eq!(f.draws.iter().map(|d| d.multiply[3]).collect::<Vec<_>>(), [0.75, 1.0, 1.0, 1.0]);
    }

    #[test]
    fn unavailable_rows_dim_and_banner_pushes_down() {
        let l = Layout::compute(1920, 1080, 1.0, DEFAULT_MAX_ASPECT);
        let f = sample().frame(MarkerState { visible: true, can_place: false, can_return: false, can_drop: false }, &l, Some(200.0));
        assert_eq!(f.draws.iter().map(|d| d.multiply[3]).collect::<Vec<_>>(), [0.75, 0.3, 0.3, 0.3]);
        let top = f.vertices.iter().map(|v| v.y).fold(f32::MAX, f32::min);
        assert_eq!(top, 200.0);
        // A banner above the menu leaves it where it is.
        let f = sample().frame(MarkerState { visible: true, ..Default::default() }, &l, Some(10.0));
        assert_eq!(f.vertices[0].y, 72.0);
    }

    #[test]
    fn keeps_its_distance_from_the_region_edge_on_ultrawide() {
        let l = Layout::compute(2560, 1080, 1.0, DEFAULT_MAX_ASPECT);
        let f = sample().frame(MarkerState { visible: true, ..Default::default() }, &l, None);
        assert!((f.vertices[0].x - (l.region[0] + 91.0 * l.scale)).abs() < 1e-3, "{}", f.vertices[0].x);
    }

    #[test]
    fn rejects_paths_and_bad_sizes() {
        let bad = br#"{"version":1,"canvas":[1280,720],"textures":[{"file":"../x.rgba","width":1,"height":1}],"meshes":[]}"#;
        assert!(MarkerHud::parse(bad, |_| Ok(vec![0; 4])).is_err());
        let short = br#"{"version":1,"canvas":[1280,720],"textures":[{"file":"x.rgba","width":2,"height":1}],"meshes":[]}"#;
        assert!(MarkerHud::parse(short, |_| Ok(vec![0; 4])).is_err());
    }

    #[test]
    fn bottom_ignores_transparent_draws() {
        let mut f = Frame::default();
        for (x, y) in [(0.0, 10.0), (1.0, 10.0), (1.0, 50.0), (0.0, 0.0), (1.0, 0.0), (1.0, 90.0)] {
            f.vertices.push(Vertex { x, y, u: 0.0, v: 0.0 });
        }
        f.draws.push(DrawCmd { first: 0, count: 3, multiply: [1.0; 4], ..Default::default() });
        f.draws.push(DrawCmd { first: 3, count: 3, multiply: [1.0, 1.0, 1.0, 0.0], ..Default::default() });
        assert_eq!(visible_bottom(&f), Some(50.0));
        // Slid off screen: nothing shows.
        f.layout = Some(Layout::compute(1920, 1080, 1.0, DEFAULT_MAX_ASPECT));
        for v in &mut f.vertices {
            v.x -= 500.0;
        }
        assert_eq!(visible_bottom(&f), None);
    }

    /// Converted retail data (local only, or `SKATEV_DATA_ROOT`): fifteen
    /// meshes over seven files. With `SKATEV_HUD_PNG_DIR`, writes
    /// marker.png (nothing placed yet) and marker-radio.png (a marker set,
    /// under the radio banner) at 1920x1080.
    #[test]
    fn loads_the_converted_menu() {
        let root = std::env::var_os("SKATEV_DATA_ROOT")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join("../../local/skate-data/assets"));
        let Ok(m) = MarkerHud::load(&MarkerHud::dir_for(&root)) else { return };
        assert_eq!((m.pieces.len(), m.textures.len()), (15, 7));
        let Some(dir) = std::env::var_os("SKATEV_HUD_PNG_DIR") else { return };
        let l = Layout::compute(1920, 1080, 1.0, DEFAULT_MAX_ASPECT);
        let write = |name: &str, frames: &[(&Frame, &[crate::player::Texture])]| {
            let mut img = crate::raster::Image::new(1920, 1080, [0.3, 0.35, 0.3, 1.0]);
            for (f, textures) in frames {
                let t: Vec<_> = textures.iter().map(crate::raster::LinearTexture::from).collect();
                crate::raster::composite(&mut img, &crate::raster::render_target(f, &t));
            }
            let file = std::fs::File::create(Path::new(&dir).join(name)).unwrap();
            let mut e = png::Encoder::new(std::io::BufWriter::new(file), 1920, 1080);
            e.set_color(png::ColorType::Rgba);
            e.set_depth(png::BitDepth::Eight);
            e.write_header().unwrap().write_image_data(&img.to_rgba8()).unwrap();
        };
        let f = m.frame(MarkerState { visible: true, can_place: true, can_return: false, can_drop: true }, &l, None);
        write("marker.png", &[(&f, &m.textures)]);
        let Ok(a) = crate::Assets::load_chyron(&crate::Assets::chyron_dir_for(&root)) else { return };
        let mut p = crate::ChyronPlayer::new(&a).unwrap();
        p.show("Radio Los Santos", "Station 3 of 19", "").unwrap();
        for _ in 0..30 {
            p.advance().unwrap();
        }
        let mut c = p.frame(&a, &l).unwrap();
        crate::chyron::close_seams(&mut c);
        let below = visible_bottom(&c).map(|b| b + 0.01 * l.height as f32);
        let f = m.frame(MarkerState { visible: true, can_place: true, can_return: true, can_drop: true }, &l, below);
        write("marker-radio.png", &[(&c, &a.textures), (&f, &m.textures)]);
        // After the outro the banner is gone and the menu is back in place.
        p.hide().unwrap();
        for _ in 0..120 {
            p.advance().unwrap();
        }
        assert_eq!(visible_bottom(&p.frame(&a, &l).unwrap()), None);
    }
}
