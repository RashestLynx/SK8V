"""Builds Skate 3's Object Dropper objects as GTA models, from the player's own
disc (data/content/parkassets.big). Nothing here is shipped: setup runs it on
the player's PC.

    python tools/prepare-dropper.py --park <disc>/data/content/parkassets.big
        --shader-reference <owned normal_spec .ydr> --decal-reference <owned normal_spec_decal .ydr>
        --rage build/tools/rage.exe --out <assets>/private/dropper --world <world>/sk8v.svwc

Source (owned disc): the dynamic recipe (recipe/dynamic/dynamic_db_*.xml) lists
the dropper's objects in its <comp> groups, the static recipe (recipe/static/
static_db_*.xml) Create-a-Park's pieces (quarter pipes, mega ramps, funboxes,
ledges, stairs; each in its Downtown, Industrial and University look); each
<mod> names its model RX2 (lod arenaid). A model carries its render meshes
(mdl_parser) and Skate's own collision (RenderWare ClusteredMesh,
retail_collision_mesh); a mesh's name ends in its recipe <mat> id, whose <sp>
channels name the texture RX2s.

Output, per object (model sk3_<recipe name>):
- <out>/<model>.ydr: render meshes (owned normal_spec shader schema, embedded
  textures) and Skate's collision as an embedded GTA bound (peds, vehicles).
  Create-a-Park's environmentParkDecal materials lay a second texture (painted
  stripes, metal copings) over the first with its own UVs (TEXCOORD0's second
  half): those meshes are drawn twice, the second time a few millimetres out
  with the decal texture in GTA's decal pass (owned normal_spec_decal schema).
- With --layouts (data/big/miscload.big): the colours Skate 3's own parks
  (data/skatepark/*.park) give their pieces: one tinted copy per piece and
  colour, <model>_<rrggbb>.ydr, its base textures recoloured (the colour, with
  the texture's light and dark), the decal layer as it is. Only the skate.Park
  lots spawn them (prepare-park-lots.py); their collision is the piece's.
- <out>/sk3_dropper.ytyp: one archetype per object; the host registers them.
- <world>.prop-models/<hash>.svwc and the index line: the exact template the
  runtime builds into the static area (Skate collision, rails, lips).
Space: Skate's Y-up metres -> GTA C=(x,-z,y) (as prepare-board-native.py),
turned in quarter steps so a ramp faces local -Y and a long object lies along
local Y, then centred on X/Y (Skate's ground stays z=0). The host drops every
object at yaw 0.
"""
import argparse
import copy
import glob
import hashlib
import json
import re
import shutil
import struct
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from _common import DONOR, REPO, joaat, run_rage, sha256, sha256_file, verify_pins

sys.path.insert(0, str(DONOR))
sys.path.insert(0, str(DONOR / 'tools/vendor/utt'))
sys.path.insert(0, str(DONOR / 'tools/vendor/university/tools/vanilla_map_extraction/tools'))
from tools.owned_game.big import BigArchive  # noqa: E402
import rx2_parser  # noqa: E402
from mdl_parser import parse_rx2  # noqa: E402
from retail_collision_mesh import decode_rx2_clustered_meshes  # noqa: E402

C = np.array([[1., 0, 0], [0, 0, -1], [0, 1, 0]])
YTYP = 'sk3_dropper'
ARCHETYPE_FLAGS = 0x20020  # as the native board: Legacy CREATE_OBJECT needs source bit17
LOD_DISTANCE = 250
# The recipe's placeholder (16x16) bound to channels a material does not use.
PLACEHOLDER = 0x2F8315D75F82C2F1
# X360 vertex element of Create-a-Park's decal meshes: TEXCOORD0 as four halves (base UV, decal UV).
HALF4_TEXCOORD0 = bytes.fromhex('001a236000050006')
DECAL_LIFT = 0.004  # metres the decal copy sits out from its surface (with GTA's decal depth bias)

# GTA materials.dat indices (rust/skatev-runtime/src/materials.rs rows):
# the template's material picks the rolling/grind sounds.
CONCRETE, METAL, WOOD, PLASTIC = 1, 59, 70, 84
CATEGORIES = {  # recipe group -> (host category, default material), in the host's order
    'DMO_Ramps': ('Ramps', WOOD), 'DMO_Rails': ('Rails', METAL), 'DMO_Barriers': ('Benches & Barriers', CONCRETE),
    'DMO_Tables': ('Tables', WOOD), 'DMO_Bins': ('Bins', METAL), 'DMO_Boxes': ('Boxes', METAL), 'DMO_Misc': ('Misc', WOOD)}
MATERIAL = {
    'am_DMO_Glbl_safetyBarrierLong': METAL, 'ae_DMO_Glbl_BarrierPlastic': PLASTIC, 'ah_DMO_Glbl_BarrierTraffic': PLASTIC,
    'ab_DMO_GLBL_ConstructionBarrier': WOOD, 'an_DMO_glbl_SafetyBarrier': WOOD, 'af_dmo_Ind_bentTrafficBarrier': METAL,
    'ac_dmo_Ind_bench': METAL, 'ao_DMO_UN_CommuntiySkateParkBench': METAL, 'ad_DMO_UN_ADBench': WOOD,
    'aj_DMO_DT_benchReclaimed': WOOD, 'al_DMO_DT_benchZShapedMetal': METAL,
    'am_DMO_GLBL_GarbageBag': PLASTIC, 'al_DMO_glbl_RecycleBin': PLASTIC, 'ak_DMO_UN_RecyclingBin': PLASTIC,
    'ad_DMO_DT_garbageBinBlue': PLASTIC,
    'ga_DMO_GLBL_MiracleWhipBalloon': PLASTIC, 'gb_DMO_GLBL_TMobileManualPad': CONCRETE, 'gc_DMO_GLBL_ZumiezCouch': PLASTIC,
    'gd_DMO_GLBL_DrPepperVendingMachine': METAL, 'ge_DMO_GLBL_monsterVendingMachine': METAL, 'ar_DMO_UN_VendingMachine': METAL,
    'ea_DMO_glbl_SoccerBall': PLASTIC, 'eb_DMO_glbl_BeachBall': PLASTIC, 'aq_DMO_UN_SoccerNet': METAL,
    'ec_DMO_DT_ballStainlessSteel': METAL, 'ez_DMO_UN_BasketballStantion': METAL, 'ej_DMO_GLBL_Shoppingcart': METAL,
    'eo_DMO_GLBL_MetalKeg': METAL, 'en_DMO_GLBL_CardBoardBox': PLASTIC, 'aj_dmo_Ind_portablePotty': PLASTIC,
    'ba_DMO_Ind_container1': METAL, 'bc_DMO_Ind_container2': METAL, 'ai_DMO_Glbl_ConstructionSignStand': METAL,
    'af_DMO_glbl_PylonPole': PLASTIC, 'ag_DMO_glbl_Pylon': PLASTIC, 'ah_DMO_glbl_Pylon_Drum': PLASTIC,
    'cn_DMO_Glbl_SlideWithPlatformStair': METAL, 'cm_DMO_glbl_LoadingRamp': METAL, 'co_DMO_Glbl_StaircaseMoveable': METAL,
    'ck_dmo_Ind_ramp': METAL, 'cf_DMO_UN_CrackedGroundJump': CONCRETE, 'cr_DMO_DT_TileBroken': CONCRETE,
    'cb_DMO_Glbl_patioTable': METAL, 'ao_dmo_Ind_table': METAL, 'aa_DMO_UN_PicnicTable': METAL}
# Create-a-Park's groups (static recipe) -> (host category, default material), in
# the host's order. Its teleports and spawner are markers, not models.
PARK_CATEGORIES = {
    'Vert': ('Vert', CONCRETE), 'Street': ('Street', CONCRETE), 'Mega': ('Mega', WOOD), 'PreMade': ('Pre-made', CONCRETE),
    'Hubbas': ('Ledges', CONCRETE), 'Rails': ('Park Rails', METAL), 'Plaza': ('Plaza', CONCRETE),
    'Misc': ('Catwalks & Misc', METAL), 'DownTown': ('Street Furniture', METAL), 'OTS': ('Landmarks', CONCRETE),
    'Branded': ('Branded', METAL), 'Foliage': ('Foliage', WOOD)}
PARK_MARKERS = {'Teleports', 'Spawner'}
# Skate 3's own parks the skate.Park lots hold (prepare-park-lots.py's LAYOUTS): their pieces get tinted copies.
PARK_LAYOUTS = ('data/skatepark/industrial_fullpark.park', 'data/skatepark/megapark_fullpark.park',
                'data/skatepark/downtown_fullpark.park', 'data/skatepark/starter_fullpark.park')
PARK_SINK = 0.015  # metres a dropped Create-a-Park piece sits into the ground (dropper_items.inc's floor)
# Objects whose sloped faces set their facing (a ramp faces local -Y).
SLOPED = {'an_DMO_UN_PicnicTableRamp'}
SLOPED_GROUPS = {'DMO_Ramps', 'Vert', 'Street', 'Mega', 'PreMade'}
# Create-a-Park names run words together: the words they use, longest first.
PARK_WORDS = sorted('''big stairs round invert bowed in out banked grass corner hip box ledge planter long short curve cap
    plaza pad water bush fall tree hedge palm shrub yucca down hubba square rail variation fat fence kink bend mega
    rainbow up branded pole flag media tower tent billboard wide wall mount stand ad board awning lamp post blue floating
    bench diamond metal shelter gazebo with clean organic waterfront hole reclaimed park filler roll flat lower launch
    ramp quarter quater pipe landing full straight catwalk support chain link pepper can balloon tri pod camera jumbo
    tron measuring stick grand the sign logging vehicle tractor yellow trailer brewery cubes mall store island hotel
    spillway street ledges legdes university shark stadium trans industrial guard propeller stacks skylight electric end
    mid tanker preset funbox folded steep escalator extension mirror mini kicker wave deck bridge clip ind svm lampost'''.split(),
                    key=len, reverse=True)
PARK_SIZES = {'lrg': 'Large', 'lg': 'Large', 'md': 'Medium', 'med': 'Medium', 'sm': 'Small', 'sml': 'Small'}
PARK_SPELLING = {'Quater': 'Quarter', 'Legdes': 'Ledges', 'Lampost': 'Lamp Post', 'Svm': 'SVM', 'Ind': 'Industrial', 'Dr': 'Dr'}
PARK_NAMES = {'qd_roundraiHlilokink': 'Round Rail Hi-Lo Kink', 'ja_benchWrainbowRail': 'Bench With Rainbow Rail',
              'de_TriPodCamera': 'Tripod Camera'}
NAMES = {  # display names the recipe name does not spell out
    'ao_DMO_UN_CommuntiySkateParkBench': 'Skate Park Bench', 'ad_DMO_UN_ADBench': 'Old Bench',
    'ez_DMO_UN_BasketballStantion': 'Basketball Stand', 'ap_DMO_glbl_WoodenPalette': 'Wooden Pallet',
    'ch_dmo_Ind_Ibeam': 'I-Beam', 'ci_dmo_Ind_kinkedTbar': 'Kinked T-Bar', 'eg_DMO_Glbl_lampole': 'Lamp Pole',
    'ej_DMO_GLBL_Shoppingcart': 'Shopping Cart', 'gb_DMO_GLBL_TMobileManualPad': 'T-Mobile Manual Pad',
    'bu_DMO_Glbl_manualPadrail1': 'Manual Pad Rail', 'bh_DMO_Glbl_RailLong_HiLow': 'Rail Long Hi-Low',
    'bi_DMO_GLBL_RailLongHiLoKink': 'Rail Long Hi-Lo Kink', 'as_DMO_UN_oldNewspaperbin2': 'Old Newspaper Bin',
    'ca_dmo_lnd_Dumpster': 'Industrial Dumpster', 'cn_DMO_Glbl_SlideWithPlatformStair': 'Slide With Stairs',
    'mp_LW_glbl_Skateboard': 'Skateboard', 'gc_DMO_GLBL_ZumiezCouch': 'Zumiez Couch',
    'aq_DMO_glbl_PJBench': 'PJ Bench', 'ar_DMO_glbl_PJBenchKinked': 'PJ Bench Kinked', 'ba_DMO_Ind_container1': 'Container 1',
    'bc_DMO_Ind_container2': 'Container 2', 'aa_DMO_glbl_USMailBox': 'US Mailbox', 'ab_DMO_DT_MailBoxLarge': 'Large Mailbox',
    'gd_DMO_GLBL_DrPepperVendingMachine': 'Dr Pepper Vending Machine'}


def model_name(recipe):
    return 'sk3_' + recipe.lower()


# Rounded tops too shallow for Skate's rail finder (the drop past the crown is under its 3 inches,
# the shoulders lie under the crown): a flat strip along the crown gives them the two lips Skate 3
# grinds them on. Recipe name part -> strip half width along the crown (the collision rises at most
# 3.5 cm), or None: one flat top over the whole upper band (the guard rail's two beams, 7 cm).
GRIND_CAPS = {'fatroundrail': 0.066, 'canadianbarrier': 0.094, 'guardrailgrind': None}


def grind_cap(recipe, tris):
    part = next((p for p in GRIND_CAPS if p in recipe.lower()), None)
    if part is None:
        return tris
    v = tris.reshape(-1, 3)
    x0 = (v[:, 0].min() + v[:, 0].max()) / 2
    if GRIND_CAPS[part] is None:  # lying along Y, level
        band = v[v[:, 2] > v[:, 2].max() - 0.08]
        half, x0 = (band[:, 0].max() - band[:, 0].min()) / 2, (band[:, 0].max() + band[:, 0].min()) / 2
        crown = np.array([[band[:, 1].min(), v[:, 2].max()], [band[:, 1].max(), v[:, 2].max()]])
    else:
        half = GRIND_CAPS[part]
        centre = np.round(v[np.abs(v[:, 0] - x0) < 0.01][:, 1:], 4)
        crown = np.array([[y, centre[centre[:, 0] == y, 1].max()] for y in np.unique(centre[:, 0])])  # (y, top z) by y
    cap = []
    for (y0, z0), (y1, z1) in zip(crown[:-1], crown[1:]):
        if y1 - y0 < 1e-3:
            continue
        a, b = [x0 - half, y0, z0], [x0 + half, y0, z0]
        c, d = [x0 + half, y1, z1], [x0 - half, y1, z1]
        cap += [[a, b, c], [a, c, d]]
    cap = np.array(cap, np.float64)
    if not len(cap):
        raise ValueError(f'{recipe}: no crown for its grind cap')
    n = np.cross(cap[:, 1] - cap[:, 0], cap[:, 2] - cap[:, 0])
    cap[n[:, 2] < 0] = cap[n[:, 2] < 0][:, ::-1]  # facing up
    return np.concatenate([tris, cap])


def display_name(recipe):
    if recipe in NAMES:
        return NAMES[recipe]
    body = re.sub(r'^[a-z]{2}_(dmo|lw)_[a-z]+_', '', recipe, flags=re.I)
    words = re.findall(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+', body)
    return ' '.join(w if w.isupper() and len(w) > 1 else w.capitalize() for w in words)


def split_words(run):
    """A lower-case run of PARK_WORDS ('quarterpiperollin') as its words; unknown letters stay one word."""
    out, at, other = [], 0, ''
    while at < len(run):
        word = next((w for w in PARK_WORDS if run.startswith(w, at)), None)
        if word:
            if other: out.append(other); other = ''
            out.append(word); at += len(word)
        else:
            other += run[at]; at += 1
    return out + ([other] if other else [])


def park_name(recipe):
    """'ba_quarterpipe90corner_lrg_Downtown' -> 'Quarter Pipe 90 Corner Large (Downtown)'."""
    theme = re.search(r'_(downtown|industrial|university)$', recipe, re.I)
    base = recipe[:theme.start()] if theme else recipe
    if base in PARK_NAMES:
        return f'{PARK_NAMES[base]} ({theme.group(1).capitalize()})' if theme else PARK_NAMES[base]
    body = re.sub(r'^[a-z]{2}_', '', base, flags=re.I).replace('XLrg', '_XL')
    words = []
    for part in filter(None, body.split('_')):
        if part.lower() in PARK_SIZES:
            words.append(PARK_SIZES[part.lower()]); continue
        for token in re.findall(r'[A-Z]{2,}(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|[A-Z]+|\d+', part):
            words += [token] if token.isdigit() or token.isupper() else split_words(token.lower())
    name = ' '.join(PARK_SPELLING.get(w.capitalize(), w if w.isupper() else w.capitalize()) for w in words)
    return f'{name} ({theme.group(1).capitalize()})' if theme else name


def catalogue(recipe_xml):
    """([(group, recipe name, model arena id)] in recipe order, {material id: (type, channels)})."""
    root = ET.fromstring(recipe_xml)
    materials = {int(m.get('id'), 16): (m.get('type'), {sp.get('chn'): int(sp.get('id'), 16) for sp in m.findall('sp')})
                 for m in root.findall('mat')}
    objects = []
    for comp in root.findall('comp'):
        if comp.get('n') in PARK_MARKERS:
            continue
        if comp.get('n') not in CATEGORIES and comp.get('n') not in PARK_CATEGORIES:
            raise ValueError(f'unknown dropper group {comp.get("n")}')
        for mod in comp.findall('mod'):
            lod = min(mod.findall('lod'), key=lambda l: int(l.get('idx')))
            objects.append((comp.get('n'), mod.get('n'), int(lod.get('arenaid'), 16)))
    return objects, materials


def recipes(archive):
    """The dropper's objects (dynamic recipe) then Create-a-Park's pieces (static recipe), and their materials."""
    objects, materials, paths = [], {}, []
    for kind in ('dynamic', 'static'):
        entry = next(e for e in archive.entries if re.search(rf'recipe/{kind}/{kind}_db_[^/]*\.xml$', e.path))
        o, m = catalogue(archive.read(entry))
        objects += o; materials |= m; paths.append(entry.path)
    models = [model_name(o[1]) for o in objects]
    if len(set(models)) != len(models):
        raise ValueError('two recipe objects share a model name')
    return objects, materials, paths


def recipe_ids(archive):
    """Recipe name -> the ids a saved park names it by (its <mod> id and its <lod> ids), both recipes."""
    out = {}
    for kind in ('dynamic', 'static'):
        entry = next(e for e in archive.entries if re.search(rf'recipe/{kind}/{kind}_db_[^/]*\.xml$', e.path))
        for mod in ET.fromstring(archive.read(entry)).iter('mod'):
            out[mod.get('n')] = [mod.get('id').lower()] + [lod.get('id').lower() for lod in mod.findall('lod')]
    return out


def category(group):
    return CATEGORIES.get(group) or PARK_CATEGORIES[group]


def item_name(group, recipe):
    return park_name(recipe) if group in PARK_CATEGORIES else display_name(recipe)


def face_normals(p, n, faces):
    """Per face: area-weighted geometric normal, oriented by the authored vertex normals."""
    a, b, c = p[faces[:, 0]], p[faces[:, 1]], p[faces[:, 2]]
    g = np.cross(b - a, c - a) * .5
    authored = n[faces].sum(axis=1)
    g[np.einsum('ij,ij->i', g, authored) < 0] *= -1
    return g


def facing(meshes, sloped):
    """Quarter turns (about +Z) for the object's GTA-space render meshes."""
    if sloped:
        total, weight = np.zeros(2), 0.
        for p, n, _, faces in meshes:
            g = face_normals(p, n, faces)
            area = np.linalg.norm(g, axis=1)
            nz = np.divide(g[:, 2], area, out=np.zeros_like(area), where=area > 0)
            up = (nz > .15) & (nz < .97)
            total += g[up, :2].sum(axis=0); weight += area[up].sum()
        if weight and np.linalg.norm(total) > .2 * weight:
            # the ramp's faces point at the rider: turn that direction to -Y
            turn = (-90 - np.degrees(np.arctan2(total[1], total[0]))) / 90
            return int(np.rint(turn)) % 4
    p = np.concatenate([m[0] for m in meshes])
    extent = p.max(axis=0) - p.min(axis=0)
    return 1 if extent[0] > extent[1] * 1.1 else 0


def quarter(k):
    c, s = [(1, 0), (0, 1), (-1, 0), (0, -1)][k]
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.]])


def tangents(p, n, uv, faces):
    t, b = np.zeros_like(p), np.zeros_like(p)
    for i0, i1, i2 in faces:
        dp1, dp2, du1, du2 = p[i1] - p[i0], p[i2] - p[i0], uv[i1] - uv[i0], uv[i2] - uv[i0]
        for i in (i0, i1, i2):
            t[i] += np.cross(dp2, n[i]) * du1[0] + np.cross(n[i], dp1) * du2[0]
            b[i] += np.cross(dp2, n[i]) * du1[1] + np.cross(n[i], dp1) * du2[1]
    out = np.zeros((len(p), 4))
    for i in range(len(p)):
        tt = t[i] - n[i] * np.dot(n[i], t[i])
        if not np.linalg.norm(tt) > 1e-12:  # also NaN (degenerate UVs)
            tt = np.cross(n[i], [1, 0, 0] if abs(n[i][0]) < .9 else [0, 1, 0])
        tt /= np.linalg.norm(tt)
        out[i] = [*tt, -1. if np.dot(np.cross(n[i], tt), b[i]) < 0 else 1.]
    return out


def node(parent, tag, text=None, **attributes):
    result = ET.SubElement(parent, tag, {k: str(v) for k, v in attributes.items()})
    if text is not None:
        result.text = str(text)
    return result


def f(x):
    return format(float(np.float32(x)), '.9g')


def vec(parent, tag, values):
    return node(parent, tag, **dict(zip('xyzw', map(f, values))))


class Textures:
    """Decoded disc textures as PNGs (one per id and role), encoded to DDS in one rage run."""

    def __init__(self, archive, work):
        self.entries = {int(Path(e.path).stem, 16): e for e in archive.entries
                        if '/texture/' in e.path and e.path.endswith('.rx2')}
        self.archive, self.work, self.names = archive, work, {}
        self.decoded = {}

    def rgba(self, texture):
        if texture not in self.decoded:
            if texture not in self.entries:
                raise ValueError(f'texture {texture:016x} not on the disc')
            parsed = rx2_parser.RX2File(self.archive.read(self.entries[texture])); parsed.parse()
            t = parsed.decode_textures()
            if not t:
                raise ValueError(f'texture {texture:016x} did not decode')
            self.decoded[texture] = np.frombuffer(t[0].rgba, np.uint8).reshape(t[0].height, t[0].width, 4)
        return self.decoded[texture]

    def name(self, channels, role, cutout, tint=None):
        """The texture name for a material channel; writes its PNG once. `tint` (r, g, b 0-255)
        recolours a base texture as Skate 3's park editor does."""
        if tint is not None and role in ('d', 'c'):
            key = self.name(channels, role, cutout) + f'_{tint_hex(tint)}'
            if key not in self.names:
                base = np.asarray(Image.open(self.work / f'{key.rsplit("_", 1)[0]}.png'))
                Image.fromarray(colourise(base, tint), 'RGBA').save(self.work / f'{key}.png')
                self.names[key] = 'DiffuseSampler'
            return key
        if role == 'dc':  # the decal layer, its alpha kept
            key, make = f'sk3_{channels["decal"]:016x}_dc', lambda: self.rgba(channels['decal'])
        elif role == 's' and 'specular' not in channels:
            key, make = 'sk3_flat_s', lambda: np.full((4, 4, 4), (48, 48, 48, 255), np.uint8)
        elif role == 'nm' and channels.get('normal', PLACEHOLDER) == PLACEHOLDER:
            key, make = 'sk3_flat_nm', lambda: np.full((4, 4, 4), (128, 128, 255, 255), np.uint8)
        elif role == 'nm':
            key, make = f'sk3_{channels["normal"]:016x}_nm', lambda: self.opaque(self.rgba(channels['normal']))
        elif role == 's':
            source = channels.get('specular', PLACEHOLDER)
            key = f'sk3_{source:016x}_s'
            def make():
                grey = self.rgba(source)[:, :, :3].max(axis=2)
                return np.dstack([grey, grey, grey, np.full_like(grey, 255)])
        else:
            diffuse, alpha = channels['diffuse'], channels.get('transparency')
            key = f'sk3_{diffuse:016x}_{"c" if cutout else "d"}'
            def make():
                image = self.rgba(diffuse).copy()
                if not cutout:
                    image[:, :, 3] = 255
                elif alpha is not None and alpha != diffuse:
                    a = Image.fromarray(self.rgba(alpha)[:, :, 3]).resize(image.shape[1::-1], Image.BILINEAR)
                    image[:, :, 3] = np.asarray(a)
                return image
        if key not in self.names:
            Image.fromarray(make(), 'RGBA').save(self.work / f'{key}.png')
            self.names[key] = 'BumpSampler' if role == 'nm' else 'SpecSampler' if role == 's' else 'DiffuseSampler'
        return key

    @staticmethod
    def opaque(image):
        image = image.copy(); image[:, :, 3] = 255
        return image


def tint_hex(tint):
    return ''.join(f'{c:02x}' for c in tint)


def colourise(image, tint):
    """Skate 3's piece colour: the colour itself, with the texture's own light and dark
    (luminance over its mean); alpha kept."""
    rgb = image[:, :, :3].astype(np.float64) / 255
    lum = rgb @ [0.299, 0.587, 0.114]
    detail = lum / max(float(lum.mean()), 1e-3)
    out = np.clip(detail[:, :, None] * (np.array(tint, np.float64) / 255), 0, 1)
    return np.dstack([np.round(out * 255).astype(np.uint8), image[:, :, 3]])


def park_tints(miscload, archive):
    """(recipe name, tint) for every coloured piece of Skate 3's own parks (white: as it is)."""
    names = {i: name for name, ids in recipe_ids(archive).items() for i in ids}
    parks = BigArchive(miscload)
    out = set()
    for entry in parks.entries:
        if entry.path.lower() not in PARK_LAYOUTS:
            continue
        data = parks.read(entry)
        count = struct.unpack_from('>I', data, 0x20)[0]
        for at in range(0x40, min(0x40 + 48 * count, len(data) - 47), 48):
            name = names.get(f'0x{struct.unpack_from(">Q", data, at)[0]:016x}')
            tint = tuple(int(round(c * 255)) for c in struct.unpack_from('>3f', data, at + 8))
            if name and tint != (255, 255, 255) and all(0 <= c <= 255 for c in tint):
                out.add((name, tint))
    return sorted(out)


def bound_fields(parent, lo, hi, margin, material, poly_flags):
    centre = (lo + hi) / 2; size = hi - lo
    vec(parent, 'BoxMin', lo); vec(parent, 'BoxMax', hi); vec(parent, 'BoxCenter', centre)
    vec(parent, 'SphereCenter', centre); node(parent, 'SphereRadius', value=f(np.linalg.norm(size) / 2))
    node(parent, 'Margin', value=f(margin)); node(parent, 'Volume', value=f(np.prod(np.maximum(size, .01))))
    s2 = size * size
    vec(parent, 'Inertia', [(s2[1] + s2[2]) / 12, (s2[0] + s2[2]) / 12, (s2[0] + s2[1]) / 12])
    for key, value in [('MaterialIndex', material), ('MaterialColourIndex', 0), ('ProceduralID', 0), ('RoomID', 0),
                       ('PedDensity', 0), ('UnkFlags', 0), ('PolyFlags', poly_flags), ('UnkType', 1)]:
        node(parent, key, value=value)


# The flags GTA's own skate props carry on their geometry bound (prop_skate_kickers).
FLAGS1 = 'MAP_WEAPON, MAP_DYNAMIC, MAP_ANIMAL, MAP_COVER, MAP_VEHICLE'
FLAGS2 = ('VEHICLE_NOT_BVH, VEHICLE_BVH, PED, RAGDOLL, ANIMAL, ANIMAL_RAGDOLL, OBJECT, PLANT, PROJECTILE, EXPLOSION, '
          'FORKLIFT_FORKS, TEST_WEAPON, TEST_CAMERA, TEST_AI, TEST_SCRIPT, TEST_VEHICLE_WHEEL, GLASS')


def bounds(root, tris, material):
    vertices, index = [], {}
    polys = []
    for t in tris:
        ids = []
        for v in t:
            key = tuple(np.float32(v))
            if key not in index:
                index[key] = len(vertices); vertices.append(key)
            ids.append(index[key])
        if len(set(ids)) == 3:
            polys.append(ids)
    v = np.array(vertices, dtype=np.float64)
    lo, hi = v.min(axis=0), v.max(axis=0)
    composite = node(root, 'Bounds', type='Composite')
    bound_fields(composite, lo, hi, 0, 0, 0)
    child = node(node(composite, 'Children'), 'Item', type='Geometry')
    bound_fields(child, lo, hi, .025, 0, 0)
    node(child, 'CompositeTransform', '1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1')
    node(child, 'CompositeFlags1', FLAGS1); node(child, 'CompositeFlags2', FLAGS2)
    vec(child, 'GeometryCenter', (lo + hi) / 2)
    node(child, 'UnkFloat1', value='0'); node(child, 'UnkFloat2', value='0.0025')
    m = node(node(child, 'Materials'), 'Item')
    for key, value in [('Type', material), ('ProceduralID', 0), ('RoomID', 0), ('PedDensity', 0)]:
        node(m, key, value=value)
    node(m, 'Flags', 'NONE'); node(m, 'MaterialColourIndex', value=0); node(m, 'Unk', value=0)
    # relative to GeometryCenter: the file stores them quantised to +-half the box from it (CodeWalker's
    # convention); model-space values past that were clamped, flattening each piece's lower half for GTA
    node(child, 'Vertices', '\n' + '\n'.join(', '.join(map(f, p - (lo + hi) / 2)) for p in v) + '\n')
    polygons = node(child, 'Polygons')
    for a, b, c in polys:
        node(polygons, 'Triangle', m=0, v1=a, v2=b, v3=c, f1=0, f2=0, f3=0)
    return len(v), len(polys)


def write_svwc(path, tris, material, tile=64.0):
    """SVWC 3 (rust/svwc): tiles stored raw (block length 0), no zstd needed."""
    tiles = {}
    for i, t in enumerate(tris):
        lo, hi = np.floor(t[:, :2].min(axis=0) / tile).astype(int), np.floor(t[:, :2].max(axis=0) / tile).astype(int)
        for tx in range(lo[0], hi[0] + 1):
            for ty in range(lo[1], hi[1] + 1):
                tiles.setdefault((tx, ty), []).append(i)
    order = sorted(tiles)
    records = sum(len(tiles[k]) for k in order)
    out = bytearray(b'SVWC' + struct.pack('<IfII', 3, tile, len(order), records))
    first = 0
    for k in order:
        out += struct.pack('<iiII', *k, first, len(tiles[k])); first += len(tiles[k])
    at = 0
    for k in order:
        out += struct.pack('<QI', at, 0); at += len(tiles[k]) * 40
    for k in order:
        for i in tiles[k]:
            out += struct.pack('<9f', *np.asarray(tris[i], np.float32).ravel()) + bytes([material, 0, 0, 0])
    path.write_bytes(out)


def decal_uvs(raw, mesh):
    """A decal mesh's second UVs: the last two halves of its four-half TEXCOORD0 (None without one)."""
    for a in mesh.attributes:
        if a.descriptor[4:12] == HALF4_TEXCOORD0:
            n, stride = len(mesh.vertices), mesh.vertex_stride
            rows = np.frombuffer(raw, np.uint8, n * stride, mesh.source_offsets['vertex_buffer']).reshape(n, stride)
            return rows[:, a.offset + 4:a.offset + 8].copy().view('>f2').astype(np.float64)
    return None


def convert(archive, objects, materials, textures, template, decal_template=None):
    """Every object's GTA-space meshes, bound and drawable XML (no rage yet)."""
    models = {int(Path(e.path).stem, 16): e for e in archive.entries
              if re.search(r'/(dynamic|static)/model/', e.path) and e.path.endswith('.rx2')}
    results = []
    for group, recipe, arena, *variant in objects:
        tint = variant[0] if variant else None
        raw = archive.read(models[arena])
        parsed = parse_rx2(raw, strict=False)
        collision = decode_rx2_clustered_meshes(raw)
        if not parsed.meshes or not collision:
            raise ValueError(f'{recipe}: no mesh or collision')
        meshes, decals = [], []
        for mesh in parsed.meshes:
            p = np.asarray(mesh.vertices, np.float64) @ C.T
            n = np.asarray(mesh.normals, np.float64) @ C.T if mesh.normals is not None else None
            if n is None or mesh.uvs is None:
                raise ValueError(f'{recipe}: mesh without normals or UVs')
            faces = np.asarray(mesh.faces, np.int64)
            length = np.linalg.norm(n, axis=1)
            if (bad := ~(length > 1e-6)).any():  # unset normals: the faces around them
                around = np.zeros_like(p); a, b, c = p[faces[:, 0]], p[faces[:, 1]], p[faces[:, 2]]
                for k in range(3):
                    np.add.at(around, faces[:, k], np.cross(b - a, c - a))
                n[bad] = around[bad]; n[bad & ~(np.linalg.norm(n, axis=1) > 1e-12)] = [0, 0, 1]
            n /= np.linalg.norm(n, axis=1, keepdims=True)
            meshes.append((p, n, np.asarray(mesh.uvs, np.float64), faces, mesh.name))
            kind, channels = materials[int(mesh.name.rsplit('_0x', 1)[1], 16)]
            if decal_template is not None and kind == 'environmentParkDecal' and 'decal' in channels:
                second = decal_uvs(raw, mesh)
                if second is not None:  # some meshes carry the material but one UV set: no decal to place
                    decals.append((len(meshes) - 1, second, channels))
        # Skate drops its pipes upright and lets physics topple them; frozen, a
        # standing pipe is a pole. A rail far taller than wide lies on its side.
        render = np.concatenate([m[0] for m in meshes]); extent = render.max(axis=0) - render.min(axis=0)
        laid = group == 'DMO_Rails' and extent[2] > 2.5 * extent[:2].max()
        lay = np.array([[1., 0, 0], [0, 0, 1], [0, -1, 0]]) if laid else np.eye(3)
        sloped = group in SLOPED_GROUPS or recipe in SLOPED
        turn = quarter(facing([(p @ lay.T, n @ lay.T, uv, faces) for p, n, uv, faces, _ in meshes], sloped)) @ lay
        meshes = [(p @ turn.T, n @ turn.T, uv, faces, name) for p, n, uv, faces, name in meshes]
        render = np.concatenate([m[0] for m in meshes])
        # centred on X/Y; Skate's ground stays z=0 (a laid pipe rests on it)
        shift = np.array([*(-(render.min(axis=0) + render.max(axis=0))[:2] / 2), -render.min(axis=0)[2] if laid else 0])
        meshes = [(p + shift, n, uv, faces, name) for p, n, uv, faces, name in meshes]
        tris = grind_cap(recipe, np.array([[t.a, t.b, t.c] for m in collision for t in m.triangles], np.float64) @ C.T @ turn.T + shift)
        material = MATERIAL.get(recipe, category(group)[1])
        model = model_name(recipe) + (f'_{tint_hex(tint)}' if tint else '')
        root = ET.Element('Drawable'); node(root, 'Name', model)
        render = np.concatenate([m[0] for m in meshes])
        lo, hi = render.min(axis=0), render.max(axis=0); centre = (lo + hi) / 2
        vec(root, 'BoundingSphereCenter', centre)
        node(root, 'BoundingSphereRadius', value=f(np.linalg.norm(render - centre, axis=1).max()))
        vec(root, 'BoundingBoxMin', lo); vec(root, 'BoundingBoxMax', hi)
        cutouts = [materials[int(m[4].rsplit('_0x', 1)[1], 16)][0].endswith('AlphaTest') for m in meshes]
        # bit per bucket: 0 opaque, 2 decal, 3 cutout
        buckets = (8 if any(cutouts) else 0) | (1 if not all(cutouts) else 0) | (4 if decals else 0)
        for level in ['High', 'Med', 'Low', 'Vlow']:
            node(root, 'LodDist' + level, value=9998)
        for level in ['High', 'Med', 'Low', 'Vlow']:
            node(root, 'Flags' + level, value=buckets if level == 'High' else 0)
        group_node = node(root, 'ShaderGroup'); dictionary = node(group_node, 'TextureDictionary')
        shaders = node(group_node, 'Shaders')
        used = []
        for p, n, uv, faces, name in meshes:
            mat_id = int(name.rsplit('_0x', 1)[1], 16)
            kind, channels = materials[mat_id]
            cutout = kind.endswith('AlphaTest')
            names = {'DiffuseSampler': textures.name(channels, 'd', cutout, tint),
                     'BumpSampler': textures.name(channels, 'nm', cutout),
                     'SpecSampler': textures.name(channels, 's', cutout)}
            for texture in names.values():
                if texture not in used:
                    used.append(texture)
            shader = copy.deepcopy(template)
            bucket = 3 if cutout else 0  # 3: GTA's cutout (alpha-test) bucket
            shader.find('RenderBucket').set('value', str(bucket))
            for param in shader.findall('./Parameters/Item'):
                key = param.get('name')
                if key in names: param.find('Name').text = names[key]
                if key == 'wetnessMultiplier': param.set('x', '0.2')
                if key == 'specularIntensityMult': param.set('x', '0.3')
                if key == 'specularFalloffMult': param.set('x', '40')
            shaders.append(shader)
        # The decal layer: its mesh again, a few millimetres out, with the decal texture on its second UVs.
        overlays = []
        for index, second, channels in decals:
            p, n, _, faces, name = meshes[index]
            overlays.append((p + n * DECAL_LIFT, n, second, faces, name))
            names = {'DiffuseSampler': textures.name(channels, 'dc', False),
                     'BumpSampler': textures.name({}, 'nm', False), 'SpecSampler': textures.name({}, 's', False)}
            for texture in names.values():
                if texture not in used:
                    used.append(texture)
            shader = copy.deepcopy(decal_template)
            for param in shader.findall('./Parameters/Item'):
                if param.get('name') in names: param.find('Name').text = names[param.get('name')]
            shaders.append(shader)
        for texture in used:
            t = node(dictionary, 'Item'); node(t, 'Name', texture); node(t, 'Unk32', value=128)
            node(t, 'Usage', 'NORMAL' if texture.endswith('_nm') else 'DIFFUSE')
            node(t, 'UsageFlags', 'NOT_HALF'); node(t, 'ExtraFlags', value=0); node(t, 'FileName', texture + '.dds')
        model_node = node(node(root, 'DrawableModelsHigh'), 'Item')
        for key, value in [('RenderMask', 255), ('Flags', 0), ('HasSkin', 0), ('BoneIndex', 0), ('Unknown1', 0)]:
            node(model_node, key, value=value)
        geometries = node(model_node, 'Geometries')
        for i, (p, n, uv, faces, _) in enumerate(meshes + overlays):
            if len(p) > 65535:
                raise ValueError(f'{recipe}: mesh over 65535 vertices')
            t = tangents(p, n, uv, faces)
            g = node(geometries, 'Item'); node(g, 'ShaderIndex', value=i)
            vec(g, 'BoundingBoxMin', [*p.min(axis=0), 0]); vec(g, 'BoundingBoxMax', [*p.max(axis=0), 0])
            vb = node(g, 'VertexBuffer'); node(vb, 'Flags', value=0); layout = node(vb, 'Layout', type='GTAV1')
            for semantic in ['Position', 'Normal', 'Colour0', 'TexCoord0', 'Tangent']:
                node(layout, semantic)
            rows = [' '.join(map(f, [*p[j], *n[j]])) + ' 255 255 255 255 ' + ' '.join(map(f, [*uv[j], *t[j]]))
                    for j in range(len(p))]
            node(vb, 'Data', '\n' + '\n'.join(rows) + '\n')
            node(node(g, 'IndexBuffer'), 'Data', ' '.join(map(str, faces.ravel())))
        vertices, polygons = bounds(root, tris, material)
        node(root, 'Lights')
        # turn and shift: GTA model space = turn @ C @ Skate model space + shift (a saved park's placements need them)
        results.append(dict(group=group, recipe=recipe, model=model, root=root, tris=tris, material=material,
                            turn=np.round(turn, 6).tolist(), shift=np.round(shift, 6).tolist(),
                            **({'variant_of': model_name(recipe), 'tint': tint_hex(tint)} if tint else {}),
                            meshes=len(meshes) + len(overlays), decals=len(overlays),
                            triangles=int(sum(len(m[3]) for m in meshes)),
                            bound_vertices=vertices, bound_polygons=polygons,
                            size=[round(float(x), 3) for x in hi - lo], arena=f'{arena:016x}'))
    return results


def build(rage, work, out, item):
    # Each build reads its textures from its own folder: two rage builds on one
    # DDS at once fail ("texture file format not supported").
    work = work / item['model']; work.mkdir()
    for texture in item['root'].findall('./ShaderGroup/TextureDictionary/Item/FileName'):
        shutil.copyfile(work.parent / texture.text, work / texture.text)
    xml = work / f'{item["model"]}.ydr.xml'
    ET.indent(item['root']); ET.ElementTree(item['root']).write(xml, encoding='utf-8', xml_declaration=True)
    ydr = out / f'{item["model"]}.ydr'
    run_rage(rage, 'resource', 'build', xml, '-o', ydr, '--strict')
    back = work / f'{item["model"]}.roundtrip.xml'
    run_rage(rage, 'resource', 'dump', ydr, '-o', back)
    parsed = ET.parse(back)
    if len(parsed.findall('./DrawableModelsHigh/Item/Geometries/Item')) != item['meshes']:
        raise ValueError(f'{item["model"]}: geometry count changed')
    polys = parsed.findall('./Bounds/Children/Item/Polygons/*')
    if len(polys) != item['bound_polygons']:
        raise ValueError(f'{item["model"]}: bound has {len(polys)} polygons, expected {item["bound_polygons"]}')
    if not parsed.findall('./ShaderGroup/TextureDictionary/Item'):
        raise ValueError(f'{item["model"]}: textures lost')
    return sha256_file(ydr)


def update_index(world, hashes):
    index = world.with_suffix('.prop-models.txt')
    lines = index.read_text(encoding='utf-8').splitlines() if index.exists() else \
        ['# SkateV local collision model templates v1: archetype hash']
    ours = {f'{h:08x}' for h in hashes}
    kept = [l for l in lines if l.split('#', 1)[0].strip().lower() not in ours]
    index.write_text('\n'.join(kept + sorted(ours)) + '\n', encoding='utf-8')


def shader_template(rage, reference, work, kind, need):
    """The first shader of `kind` (file name) in an owned .ydr that has the `need` samplers."""
    dumped = work / f'{Path(reference).stem}.ydr.xml'
    run_rage(rage, 'resource', 'dump', reference, '-o', dumped)
    found = next((s for s in ET.parse(dumped).findall('./ShaderGroup/Shaders/Item')
                  if s.findtext('FileName') in (kind, f'hash_{joaat(kind):08X}')
                  and need <= {p.get('name') for p in s.findall('./Parameters/Item')}), None)
    if found is None:
        raise ValueError(f'{reference} has no {kind} shader with {sorted(need)}')
    return found


def export(park, reference, rage, out, world, workers, decal_reference=None, layouts=None):
    archive = BigArchive(park)
    objects, materials, recipe_paths = recipes(archive)
    work = out.parent / (out.name + '.work')
    shutil.rmtree(work, ignore_errors=True); shutil.rmtree(out, ignore_errors=True)
    work.mkdir(parents=True); out.mkdir(parents=True)
    samplers = {'DiffuseSampler', 'BumpSampler', 'SpecSampler'}
    template = shader_template(rage, reference, work, 'normal_spec.sps', samplers)
    decal_template = shader_template(rage, decal_reference, work, 'normal_spec_decal.sps', samplers) if decal_reference else None
    if decal_template is None:
        print('no --decal-reference: Create-a-Park pieces without their decal layer', flush=True)
    textures = Textures(archive, work)
    tints = park_tints(layouts, archive) if layouts else []
    by_name = {o[1]: o for o in objects}
    variants = [(*by_name[name], tint) for name, tint in tints if name in by_name]
    items = convert(archive, objects + variants, materials, textures, template, decal_template)
    if variants:
        print(f'{len(variants)} tinted copies for the skate.Park lots', flush=True)
    # Normal maps stay full RGB (bc1): rage's default for them is two-channel BC5.
    # Globs, not paths: hundreds of textures would pass Windows' command line limit.
    run_rage(rage, 'textures', 'encode', work / '*_nm.png', '-o', work, '-f', 'bc1')
    run_rage(rage, 'textures', 'encode', work / '*_d.png', work / '*_c.png', work / '*_s.png', '-o', work)
    if any(work.glob('*_[dc]_*.png')):  # tinted copies' base textures (<key>_<rrggbb>)
        run_rage(rage, 'textures', 'encode', work / '*_d_*.png', work / '*_c_*.png', '-o', work)
    if any(work.glob('*_dc.png')):
        run_rage(rage, 'textures', 'encode', work / '*_dc.png', '-o', work, '-f', 'bc3')
    missing = [p.name for p in work.glob('*.png') if not p.with_suffix('.dds').is_file()]
    if missing:
        raise ValueError(f'textures not encoded: {missing[:5]}')
    with ThreadPoolExecutor(workers) as pool:
        hashes = list(pool.map(lambda item: build(rage, work, out, item), items))
    ytyp = out / f'{YTYP}.ytyp'
    run_rage(rage, 'ytyp', 'from-drawables', out, '-o', ytyp,
             '--flags', ARCHETYPE_FLAGS, '--lod-dist', LOD_DISTANCE, '--hd-dist', LOD_DISTANCE)
    dumped = work / f'{YTYP}.ytyp.xml'; run_rage(rage, 'resource', 'dump', ytyp, '-o', dumped)
    archetypes = {a.findtext('name'): a for a in ET.parse(dumped).findall('./archetypes/Item')}
    for item in items:
        a = archetypes.get(item['model'])
        if a is None or int(a.find('flags').get('value')) != ARCHETYPE_FLAGS or a.findtext('physicsDictionary') != item['model']:
            raise ValueError(f'{item["model"]}: archetype missing, wrong flags or without its embedded bound')
    templates = world.with_suffix('.prop-models')
    templates.mkdir(parents=True, exist_ok=True)
    for item in items:  # tinted copies collide as their piece, inside the lots' own templates
        if 'variant_of' not in item:
            write_svwc(templates / f'{joaat(item["model"]):08x}.svwc', item['tris'], item['material'])
    update_index(world, [joaat(i['model']) for i in items if 'variant_of' not in i])
    ids = recipe_ids(archive)
    if len(archetypes) != len(items):
        raise ValueError(f'{YTYP}.ytyp has {len(archetypes)} archetypes for {len(items)} objects')
    receipt = {'source': {'parkassets_sha256': sha256_file(park), 'recipes': recipe_paths},
               'shader_reference_sha256': sha256_file(reference), 'rage_binary_sha256': sha256_file(rage),
               'decal_reference_sha256': sha256_file(decal_reference) if decal_reference else None,
               'ytyp': ytyp.name, 'ytyp_sha256': sha256_file(ytyp), 'archetype_flags': ARCHETYPE_FLAGS,
               'space': 'GTA = turn*C*skate + shift, C=(x,-z,y); quarter turn: ramps face -Y, long objects along Y; centred on X/Y',
               'objects': [{k: v for k, v in i.items() if k not in ('root', 'tris')} |
                           {'ids': [] if 'variant_of' in i else ids[i['recipe']], 'tab': 'Park' if i['group'] in PARK_CATEGORIES else 'Props',
                            'category': category(i['group'])[0], 'name': item_name(i['group'], i['recipe']),
                            'hash': f'{joaat(i["model"]):08x}', 'ydr_sha256': h} for i, h in zip(items, hashes)]}
    (out / 'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({'output': str(out), 'objects': len(items), 'templates': str(templates)}, indent=2))


def cpp_items(park):
    """The host's kItems rows for the disc's catalogue (host/src/dropper_items.inc): the
    dropper's objects (kSkate<n>), then Create-a-Park's pieces (kPark<n>), each group in
    Skate's order (the recipe names' letter prefixes), a piece's three looks together."""
    objects, _, _ = recipes(BigArchive(park))
    props, park = list(CATEGORIES), list(PARK_CATEGORIES)
    def key(o):
        return (0, props.index(o[0])) if o[0] in CATEGORIES else (1, park.index(o[0]))
    for group, name, _ in sorted(objects, key=lambda o: (*key(o), o[1].lower())):
        tag = f'kSkate{props.index(group)}' if group in CATEGORIES else f'kPark{park.index(group)}'
        floor = 'false' if group in CATEGORIES else f'{PARK_SINK}f'  # park pieces: sunk (host/src/object_dropper.cpp)
        print(f'    {{"{model_name(name)}", "{item_name(group, name)}", 0.0f, {floor}, {tag}}},')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--park', type=Path, required=True, help='data/content/parkassets.big from the Skate 3 disc')
    p.add_argument('--shader-reference', type=Path, help='owned GTA .ydr with a normal_spec shader')
    p.add_argument('--decal-reference', type=Path, help="owned GTA .ydr with a normal_spec_decal shader (Create-a-Park's decal layer)")
    p.add_argument('--rage', type=Path, default=REPO / 'build/tools/rage.exe')
    p.add_argument('--out', type=Path)
    p.add_argument('--world', type=Path, help='the world cache (sk8v.svwc); templates go beside it')
    p.add_argument('--workers', type=int, default=6)
    p.add_argument('--layouts', type=Path, help="data/big/miscload.big: tinted copies of Skate 3's own parks' pieces")
    p.add_argument('--print-cpp', action='store_true', help='print the host item rows and stop')
    a = p.parse_args()
    if a.print_cpp:
        cpp_items(a.park)
    else:
        if not (a.shader_reference and a.out and a.world):
            p.error('--shader-reference, --out and --world are required')
        verify_pins(); export(a.park, a.shader_reference, a.rage, a.out, a.world, a.workers, a.decal_reference, a.layouts)
