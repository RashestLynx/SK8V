//! Skate 3's Object Dropper screen (the APT `objectdropper` movie), run by its
//! own script. tools/prepare-hom-hud.py --movie objectdropper merges the
//! controls it imports (button item, menu picker, scrollbar, tabs, swatch
//! renderer). Retail's C++ screen answered the movie's queries
//! (`mScreen.GetItems()` and the like, called on the root clip); here
//! [`DropperState`] does, and [`DropperPlayer`] calls the movie's own
//! `objectdropper` methods (`SetState`, `RefreshItems`, ...) when it changes.
//!
//! The VM has no symbol linkage or clip actions: the controls'
//! `Object.registerClass(symbol, class)` calls are recorded, and every newly
//! placed clip first gets its component parameters (the manifest's
//! `clip_parameters`, retail's `onClipEvent(initialize)` blocks), then, for a
//! registered symbol (the manifest's `symbols`), its class prototype and
//! constructor, before its frame actions run.
use crate::apt_display::Placement;
use crate::apt_scene::{Draw, Vertex as SceneVertex};
use crate::apt_movie::{Instance, Movie};
use crate::apt_vm::{Host, ObjectKind, Value, Vm};
use crate::layout::Layout;
use crate::player::{Assets, Frame};
use std::collections::BTreeMap;

/// Retail's `FEObjectDropperScreenMode_*` engine constants, the argument of
/// the movie's `SetState`: the C++ screen drove it one mode at a time. Their
/// numeric values are SkateV's (only equality with the globals matters).
pub const MODES: [&str; 26] = [
    "Catalog", "Delete", "FreeCam", "GotoItems", "GotoSubCategories", "GroupSelect", "HideSelection", "Manipulate",
    "MoveOnLockedAxis", "NextCategory", "NextItem", "NextSubCategory", "PreviousCategory", "PreviousItem",
    "PreviousSubCategory", "Refresh", "RefreshDuplicate", "RefreshFengShui", "RefreshFineTune", "RefreshInfo",
    "RefreshSize", "RefreshStyle", "ShowLockAxis", "ShowRotate", "ShowSelection", "SnapObject",
];

/// Frame-action budget per drain, a guard against runaway scripts.
const MAX_ACTIONS: u32 = 8192;

/// What the dropper shows (retail's screen queries).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct DropperState {
    /// Top-level tabs (retail: Props, Terrain).
    pub super_categories: Vec<String>,
    pub super_category: usize,
    /// Categories of the current tab, with their descriptions.
    pub categories: Vec<String>,
    pub descriptions: Vec<String>,
    pub category: usize,
    /// Choosing a category (true) or an item in it (false).
    pub selecting_category: bool,
    /// Items of the current category.
    pub items: Vec<String>,
    pub item: usize,
    /// Placing the picked object (retail state `place`), else `catalog`.
    pub placing: bool,
    /// Duplicate mode on (LB in placement).
    pub duplicate: bool,
    /// Fine tune mode on (RB in placement).
    pub fine_tune: bool,
}

pub(crate) struct Bindings {
    pub(crate) movie: Movie,
    pub(crate) state: DropperState,
    /// `Object.registerClass` calls: symbol -> class function.
    classes: BTreeMap<String, usize>,
    /// Exported symbol -> its character (attachMovie).
    exports: BTreeMap<String, i32>,
    /// `symbol -> character ids` from the manifest.
    symbols: BTreeMap<String, Vec<i32>>,
    /// `<parent character>/<depth>` -> component parameters.
    parameters: BTreeMap<String, BTreeMap<String, String>>,
    /// Text clips with inline `<B:name>` button tags: (button, displayed
    /// text before it). The tag itself is shown as room for the icon.
    tags: BTreeMap<usize, Vec<(String, String)>>,
}

/// Marks an initialized clip on the object itself: the VM reuses the ids
/// of collected objects, and a reused object starts without fields.
const BOUND: &str = "__sk8v_bound";

/// A text clip's font, size, bounds and alignment (movie units).
fn text_layout(movie: &Movie, id: usize) -> Option<(&crate::apt_text::Font, f32, [f32; 4], u64)> {
    let character = &movie.characters[&movie.instances.get(&id)?.character];
    let text = character.text.as_ref()?;
    let font = movie.text_assets.fonts.get(&(text["font_id"].as_i64()? as i32))?;
    let height = text["font_height"].as_f64()? as f32;
    Some((font, height, character.bounds?, text["alignment"].as_u64().unwrap_or(0)))
}

/// An inline button's glyph diameter, in text heights. The bank's glyphs
/// (about 26 units across) are centred on (24, 24) of their 64-unit
/// texture, RenderButton's 48-unit cell.
const ICON_HEIGHTS: f32 = 0.95;
const GLYPH_UNITS: f32 = 26.0;
const GLYPH_CENTRE: f32 = 24.0;

impl Bindings {
    /// Replaces `id`'s `<B:name>` tags by room for their icons (the
    /// displayed text only; scripts keep the tagged text).
    fn retag(&mut self, vm: &mut Vm, id: usize) -> Result<(), String> {
        let shown = vm.get(id, "_displayText").text();
        let Some((font, height, ..)) = text_layout(&self.movie, id).filter(|_| shown.contains("<B:")) else {
            self.tags.remove(&id);
            return Ok(());
        };
        let space = font.width(" ", height).max(1e-3);
        let room: String = " ".repeat((height * (ICON_HEIGHTS + 0.25) / space).ceil() as usize);
        let (mut out, mut tags, mut rest) = (String::new(), Vec::new(), shown.as_str());
        while let Some(start) = rest.find("<B:") {
            let Some(end) = rest[start..].find('>') else { break };
            out.push_str(&rest[..start]);
            tags.push((rest[start + 3..start + end].to_string(), out.clone()));
            out.push_str(&room);
            rest = &rest[start + end + 1..];
        }
        out.push_str(rest);
        let width = font.width(&out, height);
        vm.set(id, "_displayText", Value::Text(out))?;
        vm.set(id, "textWidth", Value::Number(width as f64))?;
        self.tags.insert(id, tags);
        Ok(())
    }

    /// `clip.removeMovieClip()`: out of its parent and, with everything
    /// under it, out of the movie (a retired handle stays a hidden object
    /// for scripts that still hold it).
    fn remove(&mut self, vm: &mut Vm, clip: usize) -> Result<(), String> {
        if let Value::Object(parent) = vm.get(clip, "_parent") {
            if let Some(p) = self.movie.instances.get_mut(&parent) {
                p.children.retain(|_, c| *c != clip);
            }
            let name = vm.get(clip, "_name").text();
            if !name.is_empty() && vm.get(parent, &name) == Value::Object(clip) {
                vm.objects[parent].fields.remove(&name);
            }
        }
        let mut stack = vec![clip];
        while let Some(id) = stack.pop() {
            if let Some(i) = self.movie.instances.remove(&id) {
                stack.extend(i.children.values().copied());
            }
            vm.set(id, "_visible", Value::Bool(false))?;
        }
        Ok(())
    }

    /// `parent.attachMovie(symbol, name, depth, init)`: a new clip of the
    /// symbol at `depth` (replacing one there), its init object's
    /// properties set, its first frame placed. The movie keeps no linkage of
    /// its own, so the clip is built here as the timeline would build it.
    fn attach(&mut self, vm: &mut Vm, parent: usize, args: &[Value]) -> Result<Value, String> {
        let arg = |i: usize| args.get(i).cloned().unwrap_or_default();
        let symbol = arg(0).text();
        let name = arg(1).text();
        let depth = arg(2).number() as i32;
        let character = *self.exports.get(&symbol).ok_or_else(|| format!("attachMovie: no symbol {symbol}"))?;
        if !self.movie.instances.contains_key(&parent) {
            return Ok(Value::Undefined);
        }
        if let Some(old) = self.movie.instances.get(&parent).and_then(|p| p.children.get(&depth)).copied() {
            self.remove(vm, old)?;
        }
        let id = vm.object(ObjectKind::Native(format!("movie:{character}")));
        vm.set(id, "_root", Value::Object(self.movie.root))?;
        vm.set(id, "_parent", Value::Object(parent))?;
        vm.set(id, "_name", Value::Text(name.clone()))?;
        for (key, value) in [("_x", 0.0), ("_y", 0.0), ("_alpha", 100.0)] {
            vm.set(id, key, Value::Number(value))?;
        }
        vm.set(id, "_visible", Value::Bool(true))?;
        let c = self.movie.characters.get(&character).ok_or("attachMovie: unknown character")?.clone();
        if let Some(text) = &c.text {
            vm.set(id, "text", Value::Text(text["initial_text"].as_str().unwrap_or("").into()))?;
        }
        let placement = Placement {
            character,
            matrix: [1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            color: [255, 255, 255, 255, 0, 0, 0, 0],
            ratio: 0.0,
            name: name.clone(),
            clip_depth: -1,
            blend_mode: 0,
        };
        self.movie.instances.insert(
            id,
            Instance { character, frame: 0, playing: !c.frames.is_empty(), children: BTreeMap::new(), placement: Some(placement) },
        );
        self.movie.instances.get_mut(&parent).unwrap().children.insert(depth, id);
        vm.set(parent, &name, Value::Object(id))?;
        if let Value::Object(init) = arg(3) {
            let fields: Vec<(String, Value)> = vm.objects[init].fields.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
            for (key, value) in fields {
                vm.set(id, &key, value)?;
            }
        }
        self.movie.text_changed(vm, id)?;
        // The menu picker pre-allocates 13 check-item rows (56 clips each),
        // shown only in checklist mode, which the dropper never enables
        // (`mChecklist` stays false): they stay bare and hidden, keeping the
        // movie inside the runtime's 2048-clip limit. Calls on their absent
        // children are no-ops, as in Flash.
        if symbol == "CHECKITEM" {
            vm.set(id, "_visible", Value::Bool(false))?;
            if let Some(i) = self.movie.instances.get_mut(&id) {
                i.playing = false;
            }
        } else if !c.frames.is_empty() {
            self.movie.seek(vm, id, 0, 1)?;
        }
        self.bind(vm)?;
        Ok(Value::Object(id))
    }

    /// Gives new clips of registered symbols their class (prototype, then
    /// constructor), as Flash's linkage did when they were placed.
    fn bind(&mut self, vm: &mut Vm) -> Result<(), String> {
        let mut by_character: BTreeMap<i32, usize> = BTreeMap::new();
        for (symbol, ids) in &self.symbols {
            if let Some(class) = self.classes.get(symbol) {
                for id in ids {
                    by_character.insert(*id, *class);
                }
            }
        }
        let movie = &self.movie;
        let fresh: Vec<(usize, Option<usize>, Option<String>)> = movie
            .instances
            .iter()
            .filter(|(id, _)| !vm.objects[**id].fields.contains_key(BOUND))
            .map(|(id, i)| {
                // Where it was placed: its parent's character and depth.
                let key = match vm.get(*id, "_parent") {
                    Value::Object(parent) => movie.instances.get(&parent).and_then(|p| {
                        p.children.iter().find(|(_, c)| **c == *id).map(|(depth, _)| format!("{}/{depth}", p.character))
                    }),
                    _ => None,
                };
                (*id, by_character.get(&i.character).copied(), key)
            })
            .collect();
        // Children first (created after their parents, so higher ids): a
        // constructor may already call its child components' methods.
        for (clip, class, key) in fresh.into_iter().rev() {
            vm.objects[clip].fields.insert(BOUND.into(), Value::Bool(true));
            if let Some(params) = key.and_then(|k| self.parameters.get(&k)) {
                for (name, value) in params.clone() {
                    vm.set(clip, &name, Value::Text(value))?;
                }
            }
            let Some(class) = class else { continue };
            if let Value::Object(proto) = vm.get(class, "prototype") {
                vm.objects[clip].prototype = Some(proto);
            }
            vm.set(clip, "__constructor__", Value::Object(class))?;
            vm.call_method(clip, "__constructor__", Vec::new(), self)?;
        }
        Ok(())
    }

}

fn array(vm: &mut Vm, values: impl IntoIterator<Item = Value>) -> Result<Value, String> {
    let object = vm.object(ObjectKind::Plain);
    let mut n = 0;
    for value in values {
        vm.set(object, n.to_string(), value)?;
        n += 1;
    }
    vm.set(object, "length", Value::Number(n as f64))?;
    Ok(Value::Object(object))
}

/// The elements of an array-shaped object (`0..length`).
fn elements(vm: &Vm, object: usize) -> Vec<Value> {
    let n = vm.get(object, "length").number();
    let n = if n.is_finite() && n > 0.0 { (n as usize).min(4096) } else { 0 };
    (0..n).map(|i| vm.get(object, &i.to_string())).collect()
}

fn set_elements(vm: &mut Vm, object: usize, values: &[Value]) -> Result<(), String> {
    let old = elements(vm, object).len();
    for (i, v) in values.iter().enumerate() {
        vm.set(object, i.to_string(), v.clone())?;
    }
    for i in values.len()..old {
        vm.objects[object].fields.remove(&i.to_string());
    }
    vm.set(object, "length", Value::Number(values.len() as f64))
}

/// AS2 `Array` methods on an array-shaped object (the VM's arrays are plain
/// objects with `length`); None when `method` is not one.
fn array_method(vm: &mut Vm, object: usize, method: &str, args: &[Value]) -> Result<Option<Value>, String> {
    let mut a = elements(vm, object);
    let index = |v: Option<&Value>, len: usize| -> usize {
        let n = v.map_or(0.0, |v| v.number());
        let n = if n.is_finite() { n as i64 } else { 0 };
        if n < 0 { (len as i64 + n).max(0) as usize } else { (n as usize).min(len) }
    };
    let result = match method {
        "push" => {
            a.extend(args.iter().cloned());
            set_elements(vm, object, &a)?;
            Value::Number(a.len() as f64)
        }
        "pop" => {
            let v = a.pop().unwrap_or_default();
            set_elements(vm, object, &a)?;
            v
        }
        "shift" => {
            let v = if a.is_empty() { Value::Undefined } else { a.remove(0) };
            set_elements(vm, object, &a)?;
            v
        }
        "unshift" => {
            let mut b = args.to_vec();
            b.extend(a);
            set_elements(vm, object, &b)?;
            Value::Number(b.len() as f64)
        }
        "reverse" => {
            a.reverse();
            set_elements(vm, object, &a)?;
            Value::Object(object)
        }
        "join" => {
            let sep = args.first().map_or(",".to_string(), |v| v.text());
            Value::Text(a.iter().map(|v| v.text()).collect::<Vec<_>>().join(&sep))
        }
        "slice" => {
            let start = index(args.first(), a.len());
            let end = if args.len() > 1 { index(args.get(1), a.len()) } else { a.len() };
            array(vm, a.get(start..end.max(start)).unwrap_or(&[]).to_vec())?
        }
        "concat" => {
            for v in args {
                match v {
                    Value::Object(o) if vm.get(*o, "length") != Value::Undefined => a.extend(elements(vm, *o)),
                    v => a.push(v.clone()),
                }
            }
            array(vm, a)?
        }
        "splice" => {
            let start = index(args.first(), a.len());
            let count = args.get(1).map_or(a.len() - start, |v| (v.number().max(0.0) as usize).min(a.len() - start));
            let removed: Vec<Value> = a.splice(start..start + count, args.iter().skip(2).cloned()).collect();
            set_elements(vm, object, &a)?;
            array(vm, removed)?
        }
        _ => return Ok(None),
    };
    Ok(Some(result))
}

fn texts(vm: &mut Vm, values: &[String]) -> Result<Value, String> {
    array(vm, values.iter().map(|s| Value::Text(s.clone())))
}

impl Host for Bindings {
    fn property_changed(&mut self, vm: &mut Vm, object: usize, key: &str) -> Result<(), String> {
        if key == "text" || key == "autoSize" {
            self.movie.text_changed(vm, object)?;
            self.retag(vm, object)?;
        }
        Ok(())
    }

    fn call(&mut self, vm: &mut Vm, object: usize, method: &str, args: Vec<Value>) -> Result<Value, String> {
        // A goto to an undefined frame is a no-op in Flash (the district
        // backdrop asks for retail's current district, which GTA has none of).
        if matches!(method, "gotoAndPlay" | "gotoAndStop") && matches!(args.first(), None | Some(Value::Undefined)) {
            return Ok(Value::Undefined);
        }
        match self.movie.method(vm, object, method, &args) {
            // Clips a goto placed are constructed at once, as in Flash.
            Ok(true) => {
                self.bind(vm)?;
                return Ok(Value::Undefined);
            }
            Ok(false) => {}
            Err(e) if e.contains("lacks label") => return Ok(Value::Undefined),
            Err(e) => return Err(e),
        }
        let native = match vm.objects.get(object).map(|o| &o.kind) {
            _ if object == vm.global => "global".into(),
            Some(ObjectKind::Native(name)) if !name.starts_with("movie:") => name.clone(),
            _ => "clip".into(),
        };
        let arg = |i: usize| args.get(i).cloned().unwrap_or_default();
        let s = &self.state;
        let item_count = s.items.len();
        Ok(match (native.as_str(), method) {
            ("Object", "registerClass") => {
                if let Value::Object(class) = arg(1) {
                    self.classes.insert(arg(0).text(), class);
                }
                Value::Bool(true)
            }
            // InitObject, rewritten by tools/prepare-hom-hud.py: (value, name) pairs.
            ("global", "__InitObject") => {
                let o = vm.object(ObjectKind::Plain);
                for pair in args.chunks(2) {
                    if let [value, name] = pair {
                        vm.set(o, name.text(), value.clone())?;
                    }
                }
                Value::Object(o)
            }
            (_, "attachMovie") => return self.attach(vm, object, &args),
            ("clip", m) if !self.movie.instances.contains_key(&object) => match array_method(vm, object, m, &args)? {
                Some(v) => v,
                None => return Err(format!("Unimplemented Object Dropper array method {m}")),
            },
            (_, "removeMovieClip") => {
                self.remove(vm, object)?;
                Value::Undefined
            }
            ("Math", "floor") => Value::Number(arg(0).number().floor()),
            ("Math", "ceil") => Value::Number(arg(0).number().ceil()),
            ("Math", "round") => Value::Number(arg(0).number().round()),
            ("Math", "abs") => Value::Number(arg(0).number().abs()),
            ("Math", "min") => Value::Number(arg(0).number().min(arg(1).number())),
            ("Math", "max") => Value::Number(arg(0).number().max(arg(1).number())),
            ("global", "ASSetPropFlags") | ("global", "trace") => Value::Undefined,
            ("FELanguage", "GetLanguage") => Value::Text("english".into()),
            ("FELanguage", "GetLocaleText") => {
                let id = arg(0).text();
                Value::Text(self.movie.text_assets.language.get(&id).cloned().unwrap_or(id))
            }
            ("Audio", _) | ("ScreenManager", _) => Value::Undefined,
            // Retail's screen (the root clip): the catalogue and its cursor.
            (_, "GetSuperCategories") => texts(vm, &s.super_categories.clone())?,
            (_, "GetCurrentSuperCategoryIndex") => Value::Number(s.super_category as f64),
            (_, "GetSubCategories") => texts(vm, &s.categories.clone())?,
            (_, "GetCurrentSubCategoryIndex") => Value::Number(s.category as f64),
            (_, "GetSubCategoryName") => Value::Text(s.categories.get(s.category).cloned().unwrap_or_default()),
            (_, "GetSubCategoryDescription") => Value::Text(s.descriptions.get(s.category).cloned().unwrap_or_default()),
            (_, "GetItems") => texts(vm, &s.items.clone())?,
            (_, "GetItemsUnlocked") => array(vm, (0..item_count).map(|_| Value::Bool(true)))?,
            (_, "GetItemsNewlyUnlocked") => array(vm, (0..item_count).map(|_| Value::Bool(false)))?,
            (_, "GetCurrentItemIndex") => Value::Number(s.item as f64),
            (_, "GetUnlockString") => Value::Text(String::new()),
            (_, "GetCurrentItemSizeInfo") => Value::Text(String::new()),
            // No sizes, styles or colours for GTA's props: retail's "none"
            // answers hide those panels (size 0; style "invalid"; colour is
            // read only for terrain).
            (_, "GetSize") => Value::Number(0.0),
            (_, "GetStyle") => Value::Text("invalid".into()),
            (_, "GetColour") => array(vm, [255.0, 255.0, 255.0].map(Value::Number))?,
            (_, "IsSelectingSubCategory") => Value::Bool(s.selecting_category),
            (_, "IsCurrentItemTerrain") => Value::Bool(false),
            (_, "IsItemSelected") => Value::Bool(s.placing),
            // The in-world placement cursor (retail's object ghost).
            (_, "IsCursorVisible") => Value::Bool(s.placing),
            (_, "IsSpawnerPresent") => Value::Bool(false),
            (_, "IsWorldSpawner") => Value::Bool(false),
            (_, "IsUIEnabled") => Value::Bool(true),
            (_, "IsDuplicateModeAllowed") => Value::Bool(true),
            (_, "IsDuplicateModeEnabled") => Value::Bool(s.duplicate),
            (_, "IsFineTuneModeEnabled") => Value::Bool(s.fine_tune),
            (_, "IsFengShuiShowingWarning") => Value::Bool(false),
            (_, "EnableDimmer" | "IntroComplete" | "OutroComplete" | "RegisterScreen" | "PlaySound") => Value::Undefined,
            // Retail's menu callbacks: every catalogue row is enabled; hilight
            // and select notifications go to SkateV's host, not the movie.
            ("Game", "OnMenuGetItemEnabled") => Value::Bool(true),
            // Image slots for the colour swatch (drawn by SkateV, not retail's
            // renderer): any slot will do.
            ("Game", m) if m.contains("Reserve") => Value::Number(0.0),
            ("Game", _) => Value::Undefined,
            // The movie telling retail's engine something (ItemSelectionReady
            // and the like): SkateV's host drives the dropper itself.
            ("SkatePark", m) if !m.starts_with("Get") && !m.starts_with("Is") => Value::Undefined,
            _ => {
                let character = self.movie.instances.get(&object).map(|i| i.character);
                return Err(format!(
                    "Unimplemented Object Dropper binding {native}.{method} (character {character:?}; classes {:?})",
                    self.classes.keys().collect::<Vec<_>>()
                ));
            }
        })
    }
}

pub struct DropperPlayer {
    vm: Vm,
    bindings: Bindings,
    /// The movie's `objectdropper` instance.
    controller: Option<usize>,
}

impl DropperPlayer {
    pub fn new(assets: &Assets) -> Result<Self, String> {
        let mut vm = Vm::new();
        for name in ["MovieClip", "FELanguage", "HUDComponents", "Math", "Audio", "ScreenManager", "Object", "SkatePark", "Game"] {
            let object = vm.object(ObjectKind::Native(name.into()));
            let prototype = vm.object(ObjectKind::Plain);
            vm.set(object, "prototype", Value::Object(prototype))?;
            vm.set(vm.global, name, Value::Object(object))?;
        }
        vm.set(vm.global, "Screen_EdgeOffset", Value::Number(0.0))?;
        for (i, mode) in MODES.iter().enumerate() {
            vm.set(vm.global, format!("FEObjectDropperScreenMode_{mode}"), Value::Number(i as f64))?;
        }
        for (i, sound) in ["FE_SOUND_CORE_A_BUTTON", "FE_SOUND_CORE_NAV_DOWN", "FE_SOUND_CORE_NAV_UP"].iter().enumerate() {
            vm.set(vm.global, *sound, Value::Number(i as f64))?;
        }
        let source = assets.source();
        let symbols: BTreeMap<String, Vec<i32>> =
            serde_json::from_value(source["symbols"].clone()).map_err(|e| format!("Object Dropper symbols: {e}"))?;
        let parameters: BTreeMap<String, BTreeMap<String, String>> = serde_json::from_value(source["clip_parameters"].clone())
            .map_err(|e| format!("Object Dropper clip parameters: {e}"))?;
        let exports = symbols.iter().filter_map(|(name, ids)| ids.first().map(|id| (name.clone(), *id))).collect();
        let mut bindings = Bindings {
            movie: Movie::load(source)?,
            state: DropperState::default(),
            classes: BTreeMap::new(),
            exports,
            symbols,
            parameters,
            tags: BTreeMap::new(),
        };
        let initial: Vec<_> = bindings
            .movie
            .characters
            .values()
            .flat_map(|c| &c.frames)
            .flat_map(|f| &f.controls)
            .filter(|c| c.type_name == "do_init_action")
            .map(|c| c.actions_offset)
            .collect();
        for offset in initial {
            let code = bindings.movie.actions.get(&offset.to_string()).cloned().ok_or("Missing Object Dropper init block")?;
            vm.run(&code, &mut bindings)?;
        }
        vm.begin_update();
        bindings.movie.initialize(&mut vm)?;
        let mut p = Self { vm, bindings, controller: None };
        p.drain()?;
        if let Value::Object(c) = p.vm.get(p.bindings.movie.root, "screen") {
            p.controller = Some(c);
        }
        Ok(p)
    }

    fn drain(&mut self) -> Result<(), String> {
        let mut calls = 0;
        loop {
            self.bindings.bind(&mut self.vm)?;
            let Some((object, offset)) = self.bindings.movie.pending.pop_front() else { break };
            calls += 1;
            if calls > MAX_ACTIONS {
                return Err("Object Dropper frame action limit".into());
            }
            if !self.bindings.movie.instances.contains_key(&object) {
                continue;
            }
            let key = offset.to_string();
            let block = self.bindings.movie.actions.get_mut(&key).ok_or("Missing Object Dropper action block")?;
            let code = std::mem::take(block);
            let result = self.vm.run_on(object, &code, &mut self.bindings);
            self.bindings.movie.actions.insert(key, code);
            result?;
        }
        Ok(())
    }

    /// Calls the movie's `objectdropper.<method>(args)`.
    pub fn call(&mut self, method: &str, args: Vec<Value>) -> Result<(), String> {
        let Some(controller) = self.controller else { return Err("Object Dropper controller was not constructed".into()) };
        self.vm.begin_update();
        self.vm.call_method(controller, method, args, &mut self.bindings)?;
        self.drain()
    }

    /// The screen's data (what `_global.SkatePark` answers); takes effect
    /// on the next [`mode`](Self::mode) call, as retail's refreshes did.
    pub fn set_data(&mut self, state: &DropperState) {
        self.bindings.state = state.clone();
    }

    /// `objectdropper.SetState(FEObjectDropperScreenMode_<mode>)` (see [`MODES`]).
    pub fn mode(&mut self, mode: &str) -> Result<(), String> {
        let index = MODES.iter().position(|m| *m == mode).ok_or_else(|| format!("unknown Object Dropper mode {mode}"))?;
        self.call("SetState", vec![Value::Number(index as f64)])
    }

    /// One timeline frame.
    pub fn advance(&mut self) -> Result<(), String> {
        self.vm.begin_update();
        self.bindings.movie.advance(&mut self.vm)?;
        self.drain()?;
        self.vm.collect(self.bindings.movie.instances.keys().copied())?;
        Ok(())
    }

    /// The movie's scene in `layout`'s pixels, with retail's button icons.
    pub fn frame(&self, assets: &Assets, layout: &Layout) -> Result<Frame, String> {
        let mut draws = crate::apt_scene::draw(&self.bindings.movie, &self.vm, assets.shapes())?;
        let bank = buttons(assets.source());
        let mut icons = Vec::new();
        self.icons(self.bindings.movie.root, [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], 1.0, &bank, &mut icons);
        draws.extend(icons);
        Ok(crate::player::flatten(draws, assets, layout))
    }

    /// Button icons under `id` (the scene's own transform and alpha rules):
    /// a Button Item's `mButtonImage` at its `mButtonRender` placeholder,
    /// with RenderButton's offsets (the texture's full size, its 48-unit
    /// glyph centred on the placeholder), and the icons of text tags.
    fn icons(&self, id: usize, parent: [f32; 6], alpha: f32, bank: &BTreeMap<String, (String, [u32; 2])>, out: &mut Vec<Draw>) {
        let (movie, vm) = (&self.bindings.movie, &self.vm);
        let Some(instance) = movie.instances.get(&id) else { return };
        if !vm.get(id, "_visible").truth() {
            return;
        }
        let mut local = instance.placement.as_ref().map_or([1.0, 0.0, 0.0, 1.0, 0.0, 0.0], |p| p.matrix);
        local[4] = vm.get(id, "_x").number() as f32;
        local[5] = vm.get(id, "_y").number() as f32;
        let m = compose(parent, local);
        let colour = instance.placement.as_ref().map_or(255, |p| p.color[0]) as f32 / 255.0;
        let alpha = alpha * colour * (vm.get(id, "_alpha").number() as f32 / 100.0).clamp(0.0, 1.0);
        if alpha <= 0.0 || m.iter().any(|x| !x.is_finite()) {
            return;
        }
        let mut quad = |name: &str, x0: f32, y0: f32, x1: f32, y1: f32| {
            let hud = if name.ends_with("_hud") { "" } else { "_hud" };
            let found = bank.get(&format!("button_{name}{hud}.Texture")).or_else(|| bank.get(&format!("button_{name}.Texture")));
            let Some((texture, size)) = found else { return };
            let corners = [[x0, y0, 0.0, 0.0], [x1, y0, 1.0, 0.0], [x1, y1, 1.0, 1.0], [x0, y1, 0.0, 1.0]];
            let vertices = [0, 1, 2, 0, 2, 3]
                .map(|i| {
                    let [x, y, u, v] = corners[i];
                    SceneVertex { position: [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]], uv: [u, v] }
                })
                .to_vec();
            out.push(Draw { texture: texture.clone(), size: *size, vertices, multiply: [1.0, 1.0, 1.0, alpha], add: [0.0; 4] });
        };
        if instance.placement.as_ref().is_some_and(|p| p.name == "mButtonRender") {
            if let Value::Object(item) = vm.get(id, "_parent") {
                let name = vm.get(item, "mButtonImage").text();
                if !name.is_empty() && name != "undefined" {
                    quad(&name, -24.0, -24.0, 40.0, 40.0);
                }
            }
        }
        if let (Some(tags), Some((font, height, bounds, alignment))) = (self.bindings.tags.get(&id), text_layout(movie, id)) {
            let (sx, sy) = (font.scale[0] * height, font.scale[1] * height);
            let shown = vm.get(id, "_displayText").text();
            let width = font.width(&shown, height);
            let alignment = if vm.get(id, "autoSize").text() == "left" { 0 } else { alignment };
            let x = bounds[0]
                + font.offset[0] * sx
                + match alignment {
                    1 => bounds[2] - bounds[0] - width,
                    2 => (bounds[2] - bounds[0] - width) * 0.5,
                    _ => 0.0,
                };
            // The line's middle: half the ascent below the glyph tops.
            let centre = bounds[1] + font.offset[1] * sy + font.ascent * sy * 0.5;
            let k = height * ICON_HEIGHTS / GLYPH_UNITS;
            for (name, before) in tags {
                let cx = x + font.width(before, height) + height * (ICON_HEIGHTS * 0.5 + 0.1);
                let (x0, y0) = (cx - GLYPH_CENTRE * k, centre - GLYPH_CENTRE * k);
                quad(name, x0, y0, x0 + 64.0 * k, y0 + 64.0 * k);
            }
        }
        for child in instance.children.values() {
            self.icons(*child, m, alpha, bank, out);
        }
    }

    pub fn visible_text(&self) -> Vec<String> {
        crate::movie::visible_text(&self.bindings.movie, &self.vm)
    }
}

fn compose(a: [f32; 6], b: [f32; 6]) -> [f32; 6] {
    [
        a[0] * b[0] + a[2] * b[1],
        a[1] * b[0] + a[3] * b[1],
        a[0] * b[2] + a[2] * b[3],
        a[1] * b[2] + a[3] * b[3],
        a[0] * b[4] + a[2] * b[5] + a[4],
        a[1] * b[4] + a[3] * b[5] + a[5],
    ]
}

/// The manifest's button bank: texture name -> (file, size).
fn buttons(source: &serde_json::Value) -> BTreeMap<String, (String, [u32; 2])> {
    source["buttons"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|b| {
            let size = [b["width"].as_u64()? as u32, b["height"].as_u64()? as u32];
            Some((b["name"].as_str()?.to_string(), (b["rgba"].as_str()?.to_string(), size)))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn sample() -> DropperState {
        DropperState {
            super_categories: vec!["Props".into(), "Terrain".into()],
            categories: vec!["Ramps".into(), "Rails".into(), "Spots".into()],
            descriptions: vec!["Kickers, banks and quarter pipes".into(), "Rails and bars".into(), "Benches and barriers".into()],
            items: vec!["Kicker".into(), "Launch Ramp".into(), "Flat Bank".into(), "Quarter Pipe".into()],
            item: 1,
            ..Default::default()
        }
    }

    /// Converted retail data (`SKATEV_DROPPER_ROOT`, a `private/hud-dropper`
    /// folder); with `SKATEV_HUD_PNG_DIR`, writes dropper-catalog.png and
    /// dropper-place.png at 1280x720.
    #[test]
    fn runs_the_converted_screen() {
        let Some(root) = std::env::var_os("SKATEV_DROPPER_ROOT") else { return };
        let assets = Assets::load_dropper(Path::new(&root)).unwrap();
        let mut p = DropperPlayer::new(&assets).unwrap();
        let mut s = sample();
        p.set_data(&s);
        p.mode("Catalog").unwrap();
        for _ in 0..60 {
            p.advance().unwrap();
        }
        let text = p.visible_text();
        let png = std::env::var_os("SKATEV_HUD_PNG_DIR");
        let l = Layout::compute(1280, 720, 1.0, crate::layout::DEFAULT_MAX_ASPECT);
        let write = |name: &str, f: &Frame| {
            let Some(dir) = png.as_ref() else { return };
            let mut img = crate::raster::Image::new(1280, 720, [0.3, 0.35, 0.3, 1.0]);
            let t: Vec<_> = assets.textures.iter().map(crate::raster::LinearTexture::from).collect();
            crate::raster::composite(&mut img, &crate::raster::render_target(f, &t));
            let file = std::fs::File::create(Path::new(&dir).join(name)).unwrap();
            let mut e = png::Encoder::new(std::io::BufWriter::new(file), 1280, 720);
            e.set_color(png::ColorType::Rgba);
            e.set_depth(png::BitDepth::Eight);
            e.write_header().unwrap().write_image_data(&img.to_rgba8()).unwrap();
        };
        write("dropper-catalog.png", &p.frame(&assets, &l).unwrap());
        p.mode("GotoItems").unwrap();
        for _ in 0..30 {
            p.advance().unwrap();
        }
        write("dropper-items.png", &p.frame(&assets, &l).unwrap());
        let mut c = sample();
        c.selecting_category = true;
        c.category = 1;
        p.set_data(&c);
        p.mode("GotoSubCategories").unwrap();
        for _ in 0..30 {
            p.advance().unwrap();
        }
        write("dropper-categories.png", &p.frame(&assets, &l).unwrap());
        assert!(text.iter().any(|t| t.contains("Launch Ramp")), "{text:?}");
        s.placing = true;
        p.set_data(&s);
        p.mode("Manipulate").unwrap();
        for _ in 0..60 {
            p.advance().unwrap();
        }
        write("dropper-place.png", &p.frame(&assets, &l).unwrap());
    }
}
