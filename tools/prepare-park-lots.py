"""Builds Skate 3's skate.Park lots (Team HQ, Hartley Stadium, Downtown Skate Park) as GTA objects,
from the player's own disc. Nothing here is shipped: setup runs it on the
player's PC.

    python tools/prepare-park-lots.py --skate <disc image or extracted folder>
        --shader-reference <owned normal_spec .ydr> --rage build/tools/rage.exe
        --data <assets>/private/parks --world <world>/sk8v.svwc

Source (owned disc): data/content/worldDIST_<lot>.big holds the lot's world
stream (data/content/world/stream/DIST_<lot>): the global presentation stream
has its models (mdl_parser) and textures (rx2_parser), the global simulation
stream Skate's collision (RenderWare ClusteredMesh, retail_collision_mesh).
A mesh's material name ends in its diffuse texture id.

Output (loaded like the Object Dropper's objects, never through the startup
pack: a GTA map in SK8V's dlc.rpf crashed Story Mode, 2026-10-15):
- <data>/sk8v_<lot>_<n>.ydr per model asset: render meshes (normal_spec,
  embedded textures) and the piece's share of the lot's collision as an
  embedded bound (BVH), so GTA's peds walk on it; sk8v_parks.ytyp (flags
  0x20020: CREATE_OBJECT needs them). The host registers them and spawns
  every piece of a lot, frozen, at the lot's origin.
- <world>.prop-models/<hash>.svwc + index lines: each piece's exact template,
  which the runtime builds into the static area (Skate collision, rails).
- <data>/parks.txt (the host's park_lot reads it) and parks.json: per lot its
  name, origin, spawn, exit and pieces; receipt.json.
- <data>/<lot>.layout: Skate 3's own park in the lot (data/big/miscload.big's
  data/skatepark/<name>.park: the Mega Park in Hartley Stadium, the full Team
  HQ), each piece as the Object Dropper's model (needs prepare-dropper.py's
  receipt and templates) with its GTA position and rotation; the host spawns
  them with the lot for GTA. Their Skate collision goes into the lot's own
  templates (hundreds of pieces would not fit Skate's list of moving objects).
Space: Skate's Y-up metres -> GTA C=(x,-z,y) (as prepare-dropper.py), centred
on X/Y, the floor at the lot's origin height.
"""
import argparse
import collections
import copy
import importlib.util
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image

from _common import DONOR, REPO, joaat, run_rage, sha256_file, verify_pins

sys.path.insert(0, str(DONOR))
sys.path.insert(0, str(DONOR / 'tools/vendor/utt'))
sys.path.insert(0, str(DONOR / 'tools/vendor/university/tools/vanilla_map_extraction/tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools.owned_game.big import BigArchive  # noqa: E402
import rx2_parser  # noqa: E402
from mdl_parser import parse_rx2  # noqa: E402
import rx2_materials  # noqa: E402
from retail_collision_mesh import decode_rx2_clustered_meshes  # noqa: E402
from skate3_streams import ASSET_TYPE_MODEL, ASSET_TYPE_TEXTURE, load_global_stream  # noqa: E402
import xiso  # noqa: E402

_spec = importlib.util.spec_from_file_location('prepare_dropper', Path(__file__).resolve().parent / 'prepare-dropper.py')
dropper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dropper)
node, f, vec, tangents, bound_fields = dropper.node, dropper.f, dropper.vec, dropper.tangents, dropper.bound_fields

C = dropper.C
YTYP = 'sk8v_parks'
ARCHETYPE_FLAGS = 0x20020  # as the dropper's: Legacy CREATE_OBJECT needs bit 17
LOD_DISTANCE = 600
# Each lot: (key, display name, district, GTA origin of its floor centre, collision source): 'skate'
# Skate 3's collision matched to what is drawn (Team HQ's holds every stage of the growing HQ),
# 'skate-raw' its collision as it is (its gaps stay: the Starter park's floor had holes), 'render' the drawn faces.
# Far out west over the sea, apart (each Skate area is 128 m around the skater), 60 m above
# the water (a fall lands in the sea; not up near the game's height limit); roofed halls. Team HQ's own collision is complete; Hartley Stadium's
# ground is in simulation records nobody decodes yet (eb0027), so its visible geometry collides.
LOTS = [
    ('teamhq', 'Team HQ', 'DIST_IndustrialSkatePark', (-4600.0, -3400.0, 60.0), 'skate'),
    ('stadium', 'Hartley Stadium', 'DIST_MegaPark', (-4600.0, -2600.0, 60.0), 'render'),
    # Downtown Plaza: an open plaza ringed by city blocks (332 x 402 m, towers 165 m tall); its own collision.
    ('downtown', 'Downtown Skate Park', 'DIST_DownTownSkatePark', (-4600.0, -1800.0, 60.0), 'skate'),
    # The starter park: a hall (137 x 112 m).
    ('starter', 'Starter Park', 'DIST_StartPark', (-4600.0, -1000.0, 60.0), 'skate'),
    # The Maloof Money Cup contest course (147 x 281 m): its course is the level itself, no .park.
    ('maloof', 'Maloof Money Cup', 'DIST_MaloofMoneyCup', (-4600.0, -200.0, 60.0), 'skate'),
    # Danny Way's skate school: hills and ramps, about 1 x 1.2 km; far out on its own. Its drawn faces
    # collide (Skate 3's collision there has gaps, and matching it to the drawing takes hours at this size).
    ('skateschool', 'Skate School', 'DIST_SkateSchool', (-6200.0, -2000.0, 60.0), 'render'),
    # DIST_BlackBoxPark (the space park) is left out: its stream lacks assets its table names.
]
# Skate 3's own park per lot (data/skatepark/<name>.park in miscload.big).
LAYOUTS = {'teamhq': 'Industrial_Fullpark', 'stadium': 'MegaPark_Fullpark', 'downtown': 'Downtown_Fullpark',
           'starter': 'Starter_Fullpark'}
PARKS_BIG = 'data/big/miscload.big'
# GTA materials.dat indices by the Skate 3 material name's surface word (rust/skatev-runtime/src/materials.rs).
SURFACE = [('wood', 70), ('metal', 58), ('rail', 59), ('plastic', 84), ('glass', 4), ('grass', 49),
           ('dirt', 32), ('sand', 31), ('brick', 1), ('concrete', 1), ('asphalt', 6), ('tile', 1)]
CONCRETE = 1


def surface(material_name):
    low = (material_name or '').lower()
    return next((m for word, m in SURFACE if word in low), CONCRETE)


def disc_lot(skate, district, work):
    """The lot's .big from the disc image (or an extracted folder)."""
    return disc_file(skate, f'data/content/world{district}.big', work)


def disc_file(skate, name, work):
    """A file of the disc image (or an extracted folder)."""
    if Path(skate).is_dir():
        return Path(skate) / name
    target = work / name
    if not target.is_file():
        disc = xiso.Disc(skate)
        try:
            disc.extract(name, target)
        finally:
            disc.close()
    return target


def park_records(data):
    """A Skate 3 .park: (piece id, position, quaternion x y z w, tint r g b 0-255) per placed piece, in Skate's
    district space. Big endian: a 0x40 byte header (piece count at 0x20), then 48 byte records:
    id (u64), colour (3 floats, the editor's tint), position (3 floats), rotation (4 floats)."""
    count = struct.unpack_from('>I', data, 0x20)[0]
    out = []
    for at in range(0x40, 0x40 + 48 * count, 48):
        if at + 48 > len(data):
            break
        piece = struct.unpack_from('>Q', data, at)[0]
        q = np.array(struct.unpack_from('>4f', data, at + 32), np.float64)
        if np.linalg.norm(q) < .5:  # an empty slot
            continue
        tint = tuple(int(round(c * 255)) for c in struct.unpack_from('>3f', data, at + 8))
        out.append((piece, np.array(struct.unpack_from('>3f', data, at + 20), np.float64), q / np.linalg.norm(q), tint))
    return out


def quat_matrix(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def matrix_quat(m):
    """Rotation matrix -> quaternion x y z w (Shepperd: from the largest component, safe at 180 degrees)."""
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0:
        s = 2 * np.sqrt(1 + trace)
        q = [(m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, s / 4]
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = 2 * np.sqrt(1 + m[0, 0] - m[1, 1] - m[2, 2])
        q = [s / 4, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s]
    elif m[1, 1] > m[2, 2]:
        s = 2 * np.sqrt(1 + m[1, 1] - m[0, 0] - m[2, 2])
        q = [(m[0, 1] + m[1, 0]) / s, s / 4, (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s]
    else:
        s = 2 * np.sqrt(1 + m[2, 2] - m[0, 0] - m[1, 1])
        q = [(m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, s / 4, (m[1, 0] - m[0, 1]) / s]
    q = np.array(q)
    return q / np.linalg.norm(q)


def layout(item, records, pieces, tinted=None):
    """The lot's park as Object Dropper models (a piece's tinted copy when it has one, `tinted`:
    (model, rrggbb) -> copy): [(model, GTA position, quaternion x y z w)].
    A piece's model space is turn @ C @ Skate's + shift (prepare-dropper.py); the lot's GTA
    space is origin + C @ Skate's district space + the lot's shift."""
    out, missing = [], collections.Counter()
    base = np.array(item['origin']) + np.array(item['shift'])
    for piece, pos, q, *tint in records:
        o = pieces.get(f'0x{piece:016x}')
        if o is None:
            missing[f'0x{piece:016x}'] += 1
            continue
        turn, shift = np.array(o['turn']), np.array(o['shift'])
        m = C @ quat_matrix(q) @ C.T @ turn.T
        model = (tinted or {}).get((o['model'], ''.join(f'{c:02x}' for c in tint[0])) if tint else None, o['model'])
        out.append((model, base + C @ pos - m @ shift, matrix_quat(m)))
    if missing:
        print(f'{item["name"]}: {sum(missing.values())} pieces of its park are not dropper objects: {dict(missing)}', flush=True)
    return out


def read_svwc(path):
    """An SVWC 3 template's triangles and GTA material per triangle (each once; tiles repeat them)."""
    b = path.read_bytes()
    tiles, records = struct.unpack_from('<II', b, 12)
    at = 20 + tiles * 16 + tiles * 12
    rows = np.frombuffer(b, np.uint8, records * 40, at).reshape(records, 40)
    rows = np.unique(rows, axis=0)
    return rows[:, :36].copy().view('<f4').reshape(-1, 3, 3).astype(np.float64), rows[:, 36].astype(int).tolist()


def stream(big, district, work):
    """The lot's global presentation and simulation streams."""
    archive = BigArchive(big)
    root = work / 'stream'
    archive.extract_entries(archive.entries, root, overwrite=True)
    directory = root / 'data/content/world/stream' / district
    return load_global_stream(directory, 'Pres', district), load_global_stream(directory, 'Sim', district)


def vertex_normals(p, faces):
    n = np.zeros_like(p)
    a, b, c = p[faces[:, 0]], p[faces[:, 1]], p[faces[:, 2]]
    g = np.cross(b - a, c - a)
    for k in range(3):
        np.add.at(n, faces[:, k], g)
    length = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.divide(n, length, out=np.tile([0., 0., 1.], (len(p), 1)), where=length > 1e-12)
    return n


class Textures:
    """The lot's textures as PNGs (diffuse; flat normal; grey spec), encoded in one rage run."""

    def __init__(self, assets, work, key):
        self.work, self.key, self.names = work, key, {}
        self.data = {}
        for asset in assets:
            if asset.record.asset_type != ASSET_TYPE_TEXTURE:
                continue
            found = re.findall(rb'(0x[0-9A-Fa-f]{16})\.Texture', asset.data)
            self.data[found[-1].decode().lower() if found else f'0x{asset.record.asset_id:016x}'] = asset.data

    def rgba(self, texture):
        parsed = rx2_parser.RX2File(self.data[texture]); parsed.parse()
        t = parsed.decode_textures()
        if not t:
            raise ValueError(f'texture {texture} did not decode')
        return np.frombuffer(t[0].rgba, np.uint8).reshape(t[0].height, t[0].width, 4)

    def diffuse(self, texture):
        """(name, cutout) for a material's diffuse texture."""
        if texture not in self.data:
            texture = None
        name = f'sk8v_{self.key}_{texture[2:] if texture else "white"}'
        if name not in self.names:
            image = self.rgba(texture).copy() if texture else np.full((4, 4, 4), 200, np.uint8)
            cutout = bool((image[:, :, 3] < 128).mean() > .02)
            if not cutout:
                image[:, :, 3] = 255
            Image.fromarray(image, 'RGBA').save(self.work / f'{name}.png')
            self.names[name] = cutout
        return name, self.names[name]

    def layer(self, texture, role):
        """A normal ('nm', as it is, opaque) or specular ('s', its brightest channel as grey) texture by id,
        or None when the lot's stream does not hold it."""
        if not texture or texture not in self.data:
            return None
        name = f'sk8v_{self.key}_{texture[2:]}_{role}'
        if name not in self.names:
            image = self.rgba(texture)
            if role == 'nm':
                image = image.copy(); image[:, :, 3] = 255
            else:
                grey = image[:, :, :3].max(axis=2)
                image = np.dstack([grey, grey, grey, np.full_like(grey, 255)])
            Image.fromarray(np.ascontiguousarray(image), 'RGBA').save(self.work / f'{name}.png')
            self.names[name] = False
        return name

    def common(self):
        for name, colour in [('sk8v_flat_nm', (128, 128, 255, 255)), ('sk8v_spec', (60, 60, 60, 255))]:
            Image.fromarray(np.full((4, 4, 4), colour, np.uint8), 'RGBA').save(self.work / f'{name}.png')
        return 'sk8v_flat_nm', 'sk8v_spec'


# A material's normal and specular textures (ids), by its material name (its diffuse): the lots'
# surfaces had a flat normal and one grey shine (2026-10-17). Skate 3's lightmaps, detail normals and
# macro overlays (second UV sets, other blends) are not carried.
CHANNELS = {}
FLAT_NORMAL_ID = '0x0000043d03e3870a'  # default_normal


def channel_id(value):
    found = re.search(r'(0x[0-9A-Fa-f]{16})$', value or '')
    return found.group(1).lower() if found and found.group(1).lower() != FLAT_NORMAL_ID else None


def models(assets):
    """Every render mesh in GTA space: (points, faces, uvs, material name, texture id). Each material's
    normal and specular texture ids go to CHANNELS."""
    out = []
    for asset in assets:
        if asset.record.asset_type != ASSET_TYPE_MODEL:
            continue
        parsed = rx2_materials.resolve(parse_rx2(asset.data, strict=False), asset.data)  # each mesh its own material
        materials = rx2_materials.mesh_materials(asset.data)
        if materials is not None and len(materials) != len(parsed.meshes):
            materials = None
        meshes = []
        for index, mesh in enumerate(parsed.meshes):
            if materials and mesh.material_name:
                CHANNELS.setdefault(mesh.material_name, {'normal': channel_id(materials[index].get('normal')),
                                                         'specular': channel_id(materials[index].get('specular'))})
            if mesh.uvs is None or not len(mesh.faces):
                continue
            # Skate 3's light effects (fx_*: projected light colours) drawn as walls are
            # stripes of colour (Team HQ, 2026-10-15): left out.
            if (mesh.material_name or '').lower().startswith('fx_'):
                continue
            p = np.asarray(mesh.vertices, np.float64) @ C.T
            faces = np.asarray(mesh.faces, np.int64).reshape(-1, 3)
            texture = re.search(r'(0x[0-9A-Fa-f]{16})$', mesh.material_name or '')
            meshes.append((p, faces, np.asarray(mesh.uvs, np.float64), mesh.material_name or '',
                           texture.group(1).lower() if texture else None))
        if meshes:
            out.append((f'{asset.record.asset_id:016x}', meshes))
    return out


def drawable(name, meshes, textures, template, flat, spec, tris=(), mats=()):
    root = ET.Element('Drawable'); node(root, 'Name', name)
    allp = np.concatenate([m[0] for m in meshes])
    lo, hi = allp.min(axis=0), allp.max(axis=0); centre = (lo + hi) / 2
    vec(root, 'BoundingSphereCenter', centre)
    node(root, 'BoundingSphereRadius', value=f(np.linalg.norm(allp - centre, axis=1).max()))
    vec(root, 'BoundingBoxMin', lo); vec(root, 'BoundingBoxMax', hi)
    shaders_used = []
    for p, faces, uv, material, texture in meshes:
        diffuse, cutout = textures.diffuse(texture)
        layers = CHANNELS.get(material, {})
        shaders_used.append((diffuse, cutout, textures.layer(layers.get('normal'), 'nm') or flat,
                             textures.layer(layers.get('specular'), 's')))
    buckets = (8 if any(c for _, c, *_ in shaders_used) else 0) | (1 if not all(c for _, c, *_ in shaders_used) else 0)
    for level in ['High', 'Med', 'Low', 'Vlow']:
        node(root, 'LodDist' + level, value=9998)
    for level in ['High', 'Med', 'Low', 'Vlow']:
        node(root, 'Flags' + level, value=buckets if level == 'High' else 0)
    group = node(root, 'ShaderGroup'); dictionary = node(group, 'TextureDictionary'); shaders = node(group, 'Shaders')
    used = []
    for diffuse, cutout, normal, shine in shaders_used:
        names = {'DiffuseSampler': diffuse, 'BumpSampler': normal, 'SpecSampler': shine or spec}
        for texture in names.values():
            if texture not in used:
                used.append(texture)
        shader = copy.deepcopy(template)
        shader.find('RenderBucket').set('value', '3' if cutout else '0')
        for param in shader.findall('./Parameters/Item'):
            key = param.get('name')
            if key in names: param.find('Name').text = names[key]
            if key == 'wetnessMultiplier': param.set('x', '0.2')
            if key == 'specularIntensityMult': param.set('x', '0.3' if shine else '0.15')  # as the dropper's pieces
            if key == 'specularFalloffMult': param.set('x', '40')
        shaders.append(shader)
    for texture in used:
        t = node(dictionary, 'Item'); node(t, 'Name', texture); node(t, 'Unk32', value=128)
        node(t, 'Usage', 'NORMAL' if texture.endswith('_nm') else 'DIFFUSE')
        node(t, 'UsageFlags', 'NOT_HALF'); node(t, 'ExtraFlags', value=0); node(t, 'FileName', texture + '.dds')
    model = node(node(root, 'DrawableModelsHigh'), 'Item')
    for key, value in [('RenderMask', 255), ('Flags', 0), ('HasSkin', 0), ('BoneIndex', 0), ('Unknown1', 0)]:
        node(model, key, value=value)
    geometries = node(model, 'Geometries')
    for i, (p, faces, uv, _, _) in enumerate(meshes):
        if len(p) > 65535:
            raise ValueError(f'{name}: mesh over 65535 vertices')
        n = vertex_normals(p, faces)
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
    polys = embed_bound(root, tris, mats) if len(tris) else 0
    node(root, 'Lights')
    return root, used, polys


# What GTA's own map and skate-prop bounds carry (reh_dlc_int_03_sum2.ybn, prop_skate_kickers).
FLAGS1 = 'MAP_WEAPON, MAP_DYNAMIC, MAP_ANIMAL, MAP_COVER, MAP_VEHICLE'
FLAGS2 = ('VEHICLE_NOT_BVH, VEHICLE_BVH, PED, RAGDOLL, ANIMAL, ANIMAL_RAGDOLL, OBJECT, PLANT, PROJECTILE, EXPLOSION, '
          'FORKLIFT_FORKS, TEST_WEAPON, TEST_CAMERA, TEST_AI, TEST_SCRIPT, TEST_VEHICLE_WHEEL, GLASS')


def embed_bound(root, tris, materials):
    """The piece's collision as the drawable's embedded bound: a composite with
    one BVH geometry (vertices about its centre), one GTA material each."""
    mats = sorted(set(materials))
    vertices, index, polys = [], {}, []
    for t, m in zip(tris, materials):
        ids = []
        for v in t:
            key = tuple(np.float32(v))
            if key not in index:
                index[key] = len(vertices); vertices.append(key)
            ids.append(index[key])
        if len(set(ids)) == 3:
            polys.append((mats.index(m), ids))
    v = np.array(vertices, np.float64)
    lo, hi = v.min(axis=0), v.max(axis=0)
    composite = node(root, 'Bounds', type='Composite')
    bound_fields(composite, lo, hi, 0, 0, 0)
    child = node(node(composite, 'Children'), 'Item', type='GeometryBVH')
    bound_fields(child, lo, hi, .005, 0, 0)
    node(child, 'CompositeTransform', '1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1')
    node(child, 'CompositeFlags1', FLAGS1); node(child, 'CompositeFlags2', FLAGS2)
    vec(child, 'GeometryCenter', (lo + hi) / 2)
    node(child, 'UnkFloat1', value='0'); node(child, 'UnkFloat2', value='0.0025')
    items = node(child, 'Materials')
    for m in mats:
        item = node(items, 'Item')
        for key, value in [('Type', m), ('ProceduralID', 0), ('RoomID', 0), ('PedDensity', 0)]:
            node(item, key, value=value)
        node(item, 'Flags', 'NONE'); node(item, 'MaterialColourIndex', value=0); node(item, 'Unk', value=0)
    centre = (lo + hi) / 2
    node(child, 'Vertices', '\n' + '\n'.join(', '.join(map(f, p - centre)) for p in v) + '\n')
    polygons = node(child, 'Polygons')
    for m, (a, b, c) in polys:
        node(polygons, 'Triangle', m=m, v1=a, v2=b, v3=c, f1=0, f2=0, f3=0)
    return len(polys)


def write_svwc(path, tris, materials, tile=64.0):
    """SVWC 3 (rust/svwc), tiles stored raw, one GTA material per triangle (prepare-dropper.py's, per triangle)."""
    tiles = {}
    for i, t in enumerate(tris):
        lo = np.floor(t[:, :2].min(axis=0) / tile).astype(int); hi = np.floor(t[:, :2].max(axis=0) / tile).astype(int)
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
            out += struct.pack('<9f', *np.asarray(tris[i], np.float32).ravel()) + bytes([materials[i], 0, 0, 0])
    path.write_bytes(out)


def write(root, path):
    ET.indent(root); ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)


def surface_points(t, step):
    """Points over a triangle about `step` apart (its corners included)."""
    e1, e2 = t[1] - t[0], t[2] - t[0]
    n = int(max(np.linalg.norm(e1), np.linalg.norm(e2), np.linalg.norm(t[2] - t[1])) / step) + 1
    a, b = np.meshgrid(np.arange(n + 1), np.arange(n + 1))
    keep = a + b <= n
    return t[0] + np.outer(a[keep] / n, e1) + np.outer(b[keep] / n, e2)


def split_long(p, faces, uv, longest=8.0):
    """A render mesh with no edge over `longest` (each too-long edge halved, again and again;
    shared edges split once, uvs interpolated). GTA leaves Skate 3's 60-100 m floor triangles
    undrawn: the sea showed through the Maloof course's floor beside its spawn (2026-10-17)."""
    p, uv = [*map(np.asarray, p)], [*map(np.asarray, uv)]
    mids, out, todo = {}, [], [tuple(f) for f in faces]
    while todo:
        f = todo.pop()
        edges = [float(np.linalg.norm(p[f[(i + 1) % 3]] - p[f[i]])) for i in range(3)]
        i = int(np.argmax(edges))
        if edges[i] <= longest:
            out.append(f)
            continue
        a, b, c = f[i], f[(i + 1) % 3], f[(i + 2) % 3]
        key = (min(a, b), max(a, b))
        if key not in mids:
            mids[key] = len(p)
            p.append((p[a] + p[b]) / 2); uv.append((uv[a] + uv[b]) / 2)
        m = mids[key]
        todo += [(a, m, c), (m, b, c)]
    return np.array(p), np.array(out, np.int64), np.array(uv)


def split_fit(p, faces, uv):
    """split_long within a GTA mesh's 65535 vertices (coarser where a huge mesh would not fit)."""
    for longest in (8.0, 16.0, 32.0, 64.0):
        q, f2, uv2 = split_long(p, faces, uv, longest)
        if len(q) <= 65535:
            return q, f2, uv2
    return p, faces, uv


def subdivide(t3, longest):
    """A face cut (longest edge in half, again and again) until no edge is longer than `longest`."""
    out, todo = [], [t3]
    while todo:
        f = todo.pop()
        edges = [np.linalg.norm(f[(i + 1) % 3] - f[i]) for i in range(3)]
        i = int(np.argmax(edges))
        if edges[i] <= longest:
            out.append(f)
            continue
        a, b, c = f[i], f[(i + 1) % 3], f[(i + 2) % 3]
        m = (a + b) / 2
        todo += [np.array([a, m, c]), np.array([m, b, c])]
    return out


def steep(t):
    """A wall (more than 60 degrees from flat), as opposed to a floor, ramp or roof."""
    n = np.cross(t[1] - t[0], t[2] - t[0]); length = np.linalg.norm(n)
    return length < 1e-12 or abs(n[2]) / length < .5


class Near:
    """Is a face near `reference` faces of its kind? At least a third of it: a wall
    within `wall` of a wall (1 m kept 2 m stubs of hidden wall beside Team HQ's
    scaffold towers); a floor (or roof, or ramp) within `reach` across and about
    `rise` up or down of a floor. (A wall near only the floor is not near.)"""
    def __init__(self, reference, wall=.5, reach=1.0, rise=.1):
        self.reach = reach
        self.cells = {True: np.array([wall / 2] * 3), False: np.array([reach / 2, reach / 2, rise / 2])}
        self.seen = {True: set(), False: set()}
        for r in reference:
            kind = steep(r)
            for q in np.floor(surface_points(r, .25) / self.cells[kind]).astype(np.int64):
                self.seen[kind].add((int(q[0]), int(q[1]), int(q[2])))

    def __call__(self, t3):
        kind = steep(t3)
        seen, span = self.seen[kind], range(-2, 3)
        rises = span if kind else range(-1, 2)  # a floor: within 5-10 cm up or down
        pts = np.floor(surface_points(t3, self.cells[kind][0]) / self.cells[kind]).astype(np.int64)
        near = sum(any((int(x) + dx, int(y) + dy, int(z) + dz) in seen for dx in span for dy in span for dz in rises)
                   for x, y, z in pts)
        return near * 3 >= len(pts)


def split_by(faces, test, longest=3.0):
    """Faces that pass `test`, judged in pieces no longer than `longest`: one 65 m Team HQ
    floor triangle reaches from inside the hall out past its north wall, and judged
    whole it took a quarter of the hall's floor with it (2026-10-16). A face whose
    pieces all agree stays whole (GTA's bounds index 16-bit: no needless pieces)."""
    out = []
    for f in faces:
        pieces = subdivide(f, longest)
        verdicts = [test(q) for q in pieces]
        if all(verdicts):
            out.append(f)
        elif any(verdicts):
            out += [q for q, v in zip(pieces, verdicts) if v]
    return out


def drop_hidden(tris, render):
    """Skate 3's Team HQ grows as the player's team does, and its collision holds every
    stage: 5-15 m walls across the hall (one beside the spawn), a 20 cm slab under it,
    and floor and roof outside the building, none of them in the stage we draw (the
    player's invisible walls, 2026-10-16). Collision is kept where it lies near visible
    faces of its kind (Near)."""
    return split_by(tris, Near(render))


def add_visible(tris, render, top):
    """The other way round: most of Team HQ's south and east walls are drawn but have no
    collision in Skate 3 (a ped walked out through the east wall, 2026-10-16), and some
    floor has none either. Visible faces up to `top` with no collision of their kind
    near collide as drawn; walls both ways (GTA's collision is one-sided)."""
    near = Near(tris)
    low = [r for r in render if r[:, 2].min() < top]
    missing = split_by(low, lambda q: not near(q))
    added = []
    for r in missing:
        added.append(r)
        if steep(r):
            added.append(r[[0, 2, 1]])
    return tris + added, len(missing)


def cover(faces, x0, y0, nx, ny, cell, corners=True):
    """Which `cell`-metre cells (from x0, y0) the faces' plans cover (cell centres inside them;
    with `corners`, also the cells their corners lie in: faces smaller than a cell count)."""
    out = np.zeros((nx, ny), bool)
    for t3 in faces:
        lo = np.floor((t3[:, :2].min(axis=0) - [x0, y0]) / cell).astype(int)
        hi = np.floor((t3[:, :2].max(axis=0) - [x0, y0]) / cell).astype(int)
        lo, hi = np.maximum(lo, 0), np.minimum(hi, [nx - 1, ny - 1])
        if (hi < lo).any():
            continue
        gx, gy = np.meshgrid(np.arange(lo[0], hi[0] + 1), np.arange(lo[1], hi[1] + 1), indexing='ij')
        px, py = x0 + (gx + .5) * cell, y0 + (gy + .5) * cell
        (ax, ay), (bx, by), (cx, cy) = t3[:, :2]
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-12:
            if corners:
                out[np.clip(((t3[:, 0] - x0) / cell).astype(int), 0, nx - 1), np.clip(((t3[:, 1] - y0) / cell).astype(int), 0, ny - 1)] = True
            continue
        l1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / d
        l2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / d
        inside = (l1 >= -1e-6) & (l2 >= -1e-6) & (1 - l1 - l2 >= -1e-6)
        out[gx[inside], gy[inside]] = True
        if corners:
            out[np.clip(((t3[:, 0] - x0) / cell).astype(int), 0, nx - 1), np.clip(((t3[:, 1] - y0) / cell).astype(int), 0, ny - 1)] = True
    return out


def runs(mask, x0, y0, cell, longest=8.0):
    """The mask's cells as rectangles (x0, y0, x1, y1): runs along x per row, cut to `longest`."""
    out, step = [], max(1, int(longest / cell))
    for j in range(mask.shape[1]):
        i = 0
        while i < mask.shape[0]:
            if not mask[i, j]:
                i += 1
                continue
            k = i
            while k < mask.shape[0] and mask[k, j] and k - i < step:
                k += 1
            out.append((x0 + i * cell, y0 + j * cell, x0 + k * cell, y0 + (j + 1) * cell))
            i = k
    return out


def boundary(tris, floor, render, height=8.0, cell=1.0):
    """Walls (both ways) round the floor's extent, from the floor `height` m up, and a plate
    2 cm under the floor across it: Team HQ's east doorways open onto nothing (Skate
    3's yard outside is not drawn), a ped walked out of them into the sea, and the
    floor had small holes (2026-10-16). Skate 3's own lots end in such invisible
    boundaries too. Per `cell`: no plate over anything drawn lower than the floor (a lid
    over Team HQ's 1.3 m pit, 2026-10-17), and under all of the plate ground is drawn
    (`ground`: its rectangles; Downtown's floor extent ran 40 m past the drawn plaza and
    part of the Maloof course's drawn floor does not show in GTA: the ped walked on the
    sea, 2026-10-17). Returns (collision, ground)."""
    pts = np.array([v for t3 in tris if not steep(t3) and np.abs(t3[:, 2] - floor).max() < .05 for v in t3])
    (x0, y0), (x1, y1) = pts[:, :2].min(axis=0), pts[:, :2].max(axis=0)
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    out = []
    for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1]):
        a0, b0 = np.array([ax, ay, floor]), np.array([bx, by, floor])
        a1, b1 = a0 + [0, 0, height], b0 + [0, 0, height]
        for f in (np.array([a0, b0, b1]), np.array([a0, b1, a1])):
            out += [f, f[[0, 2, 1]]]
    nx, ny = int(np.ceil((x1 - x0) / cell)), int(np.ceil((y1 - y0) / cell))
    up = [r for r in render if not steep(r) and np.cross(r[1] - r[0], r[2] - r[0])[2] > 0]
    sunken = cover([r for r in up if r[:, 2].mean() < floor - .1], x0, y0, nx, ny, cell)
    z = floor - .02
    for a, b, c, d in runs(~sunken, x0, y0, cell):
        for f in (np.array([[a, b, z], [c, b, z], [c, d, z]]), np.array([[a, b, z], [c, d, z], [a, d, z]])):
            out += subdivide(f, 8.0)  # the runtime's static area is tiled: no 130 m faces
    return out, runs(~sunken, x0, y0, cell, longest=16.0)  # also under drawn floor: GTA leaves some of it undrawn


def ground_mesh(rects, floor, texture, tile=4.0):
    """The drawn ground (a render mesh: points, faces, uvs, material, texture) over `rects`,
    4 cm under the floor (under any drawn floor it meets; the plate is at 2 cm)."""
    p, faces = [], []
    z = floor - .04
    for a, b, c, d in rects:
        k = len(p)
        p += [[a, b, z], [c, b, z], [c, d, z], [a, d, z]]
        faces += [[k, k + 1, k + 2], [k, k + 2, k + 3]]
    p = np.array(p, np.float64)
    return p, np.array(faces, np.int64), p[:, :2] / tile, 'sk8v_ground', texture


def face_up(tris, top, cell=2.0):
    """Skate 3's collision for a lot is two-sided where it matters: about 1,500 m2 of
    Team HQ's floor exists only as downward faces, which GTA and SK8V's runtime (one-
    sided) fall straight through (2026-10-16). A floor or ramp face (more than 60
    degrees from vertical) that faces down with no upward face under its centre at
    the same height is turned over, up to `top` (the floor + 20 m: Team HQ's upper
    level; roof undersides stay ceilings). Walls are left as they are."""
    def normal(t):
        n = np.cross(t[1] - t[0], t[2] - t[0]); length = np.linalg.norm(n)
        return n / length if length > 1e-12 else None
    def height(t, x, y):
        p = t[:, :2]
        d = (p[1, 1] - p[2, 1]) * (p[0, 0] - p[2, 0]) + (p[2, 0] - p[1, 0]) * (p[0, 1] - p[2, 1])
        if abs(d) < 1e-12:
            return None
        l1 = ((p[1, 1] - p[2, 1]) * (x - p[2, 0]) + (p[2, 0] - p[1, 0]) * (y - p[2, 1])) / d
        l2 = ((p[2, 1] - p[0, 1]) * (x - p[2, 0]) + (p[0, 0] - p[2, 0]) * (y - p[2, 1])) / d
        if min(l1, l2, 1 - l1 - l2) < -1e-6:
            return None
        return l1 * t[0, 2] + l2 * t[1, 2] + (1 - l1 - l2) * t[2, 2]
    normals = [normal(t) for t in tris]
    grid = {}
    for i, (t, n) in enumerate(zip(tris, normals)):
        if n is None or n[2] <= .5:
            continue
        lo = np.floor(t[:, :2].min(axis=0) / cell).astype(int); hi = np.floor(t[:, :2].max(axis=0) / cell).astype(int)
        for gx in range(lo[0], hi[0] + 1):
            for gy in range(lo[1], hi[1] + 1):
                grid.setdefault((gx, gy), []).append(i)
    out, flipped = [], 0
    for t, n in zip(tris, normals):
        if n is not None and n[2] < -.5 and t[:, 2].mean() <= top:
            # The centre and three points toward the corners must all lie on an upward face.
            centre = t.mean(axis=0)
            covered = True
            for x, y, z in [centre] + [.6 * centre + .4 * v for v in t]:
                hit = False
                for i in grid.get((int(np.floor(x / cell)), int(np.floor(y / cell))), ()):
                    h = height(tris[i], x, y)
                    if h is not None and abs(h - z) < .05:
                        hit = True
                        break
                if not hit:
                    covered = False
                    break
            if not covered:
                t = t[[0, 2, 1]]
                flipped += 1
        out.append(t)
    return out, flipped


def floor_height(tris):
    """The lot's floor: the height (1 cm bins) of the most upward-facing area within
    0.6 m of its lowest point (Team HQ's collision floor is at +0.12, Hartley
    Stadium's visible floor at +0.21: a few low bits sit at 0)."""
    area, anywhere = {}, {}
    for t in tris:
        n = np.cross(t[1] - t[0], t[2] - t[0]); length = np.linalg.norm(n)
        z = t[:, 2].mean()
        if length > 1e-9 and n[2] / length > .95:
            key = round(float(z), 2)
            anywhere[key] = anywhere.get(key, 0.0) + length / 2
            if z < .6:
                area[key] = area.get(key, 0.0) + length / 2
    # Lots whose low bits sit far under the floor (the Maloof course's pits, the Skate School's
    # hills): the largest flat level at any height.
    return max(area or anywhere, key=(area or anywhere).get)


def spawn_point(floor_tris, centre):
    """The floor point nearest the lot's centre (GTA space), and the heading facing the centre's longer side."""
    best, best_d = None, 1e18
    for t in floor_tris:
        c = t.mean(axis=0)
        d = np.sum((c[:2] - centre[:2]) ** 2)
        if d < best_d:
            best, best_d = c, d
    return best


def clear_spawn(item, park_tris, room=2.5, headroom=3.0):
    """Moves the lot's spawn (and exit) off its Skate 3 park: the floor point nearest the old spawn with
    no piece within `room` metres that rises over the floor. Unchanged when the spawn is clear."""
    spawn, floor_z = np.array(item['spawn']), item['spawn'][2] - 1.0
    tall = park_tris[park_tris[:, :, 2].max(axis=1) > floor_z + 0.05] if len(park_tris) else park_tris
    tall = tall[tall[:, :, 2].min(axis=1) < floor_z + headroom] if len(tall) else tall
    if not len(tall):
        return
    centres = tall.mean(axis=1)[:, :2]
    lo, hi = tall[:, :, :2].min(axis=1), tall[:, :, :2].max(axis=1)

    def blocked(p):
        near = np.all((p[:2] >= lo - room) & (p[:2] <= hi + room), axis=1)
        return near.any()
    if not blocked(spawn):
        return
    order = np.argsort(np.sum((item['floor_points'][:, :2] - spawn[:2]) ** 2, axis=1))
    for i in order:
        p = item['floor_points'][i]
        if abs(p[2] - floor_z) < 0.05 and not blocked(p):
            moved = np.array([p[0], p[1], spawn[2]])
            item['exit'] = (np.array(item['exit']) + moved - spawn).round(3).tolist()
            item['spawn'] = moved.round(3).tolist()
            print(f'{item["name"]}: spawn moved off its Skate 3 park to {item["spawn"]}', flush=True)
            return


def lot(key, label, district, origin, source, skate, work, template, textures_out):
    origin = np.array(origin)
    lot_work = work / key; lot_work.mkdir(parents=True)
    pres, sim = stream(disc_lot(skate, district, work), district, lot_work)
    textures = Textures(pres, textures_out, key)
    flat, spec = textures.common()
    parts = models(pres)
    render = np.concatenate([p for _, meshes in parts for p, *_ in meshes])
    lo, hi = render.min(axis=0), render.max(axis=0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])  # centred, floor at 0
    parts = [(asset, [(p + shift, faces, uv, material, texture) for p, faces, uv, material, texture in meshes])
             for asset, meshes in parts]
    # The lot's collision, then each triangle to one piece: its own render faces
    # ('render'), or the piece whose footprint holds the triangle's centre ('skate').
    owned = [([], []) for _ in parts]
    if source in ('skate', 'skate-raw'):
        boxes = []
        for _, meshes in parts:
            pts = np.concatenate([m[0] for m in meshes])
            boxes.append((pts.min(axis=0)[:2] - 1, pts.max(axis=0)[:2] + 1))
        lot_tris = [np.array([tri.a, tri.b, tri.c], np.float64) @ C.T + shift
                    for asset in sim for mesh in decode_rx2_clustered_meshes(asset.data) for tri in mesh.triangles]
        lot_tris, flipped = face_up(lot_tris, floor_height(lot_tris) + 20.0)
        print(f'{label}: {flipped} downward-only floor faces turned up', flush=True)
        if source == 'skate':
            render_tris = [p[face] for _, meshes in parts for p, faces, *_ in meshes for face in faces]
            before = len(lot_tris)
            lot_tris = drop_hidden(lot_tris, render_tris)
            print(f'{label}: collision where nothing is visible dropped ({before} faces in, {len(lot_tris)} out)', flush=True)
            lot_tris, shown = add_visible(lot_tris, render_tris, floor_height(lot_tris) + 20.0)
            print(f'{label}: {shown} visible faces with no collision there made to collide', flush=True)
            level = floor_height(lot_tris)
            walls, ground = boundary(lot_tris, level, render_tris)
            lot_tris = lot_tris + walls
        if os.environ.get('SK8V_PARKS_DEBUG'):  # development: the lot's final collision, for checks
            np.save(Path(os.environ['SK8V_PARKS_DEBUG']) / f'{key}-collision.npy', np.array(lot_tris))
        for t3 in lot_tris:
            c = t3.mean(axis=0)[:2]
            inside = [i for i, (a, b) in enumerate(boxes) if (c >= a).all() and (c <= b).all()]
            if inside:
                i = min(inside, key=lambda i: np.prod(boxes[i][1] - boxes[i][0]))
            else:
                i = min(range(len(boxes)), key=lambda i: np.sum(((boxes[i][0] + boxes[i][1]) / 2 - c) ** 2))
            owned[i][0].append(t3); owned[i][1].append(CONCRETE)
        if source == 'skate' and ground:
            # its look: the texture most of the drawn floor wears
            area = collections.Counter()
            for _, meshes in parts:
                for p, faces, _, material, texture in meshes:
                    for face in faces:
                        q = p[face]
                        n = np.cross(q[1] - q[0], q[2] - q[0])
                        if n[2] > 0 and abs(q[:, 2].mean() - level) < .1:
                            area[(material, texture)] += np.linalg.norm(n) / 2
            material, texture = area.most_common(1)[0][0] if area else ('sk8v_ground', None)
            ground_p, ground_faces, ground_uv, _, _ = ground_mesh(ground, level, texture)
            parts.append(('ground', [(ground_p, ground_faces, ground_uv, material, texture)]))  # the floor's material, its maps too
            owned.append(([], []))
            print(f'{label}: ground drawn under its floor ({sum((c - a) * (d - b) for a, b, c, d in ground):.0f} m2)', flush=True)
    else:
        # The visible geometry collides. Its floor is not one level: along the model tiles'
        # seams some vertices sit 21 cm low, a ledge across Hartley Stadium's floor that threw
        # the board around (2026-10-15). Whatever lies within half a metre of the floor is
        # flattened onto it.
        floor = floor_height([p[face] for _, meshes in parts for p, faces, *_ in meshes for face in faces])
        for i, (_, meshes) in enumerate(parts):
            for p, faces, _, material, _ in meshes:
                m = surface(material)
                q = p.copy()
                q[np.abs(q[:, 2] - floor) < .5, 2] = floor
                for face in faces:
                    owned[i][0].append(q[face]); owned[i][1].append(m)
    drawables = []
    for i, (asset, meshes) in enumerate(parts):
        name = f'sk8v_{key}_{i}'
        tris, mats = np.array(owned[i][0]).reshape(-1, 3, 3), owned[i][1]
        # drawn only: cut long triangles (collision keeps the faces as they are: cutting them moved the
        # Skate School's spawn and opened cracks it fell through, 2026-10-17)
        drawn = [(*split_fit(p, faces, uv), material, texture) for p, faces, uv, material, texture in meshes]
        root, used, polys = drawable(name, drawn, textures, template, flat, spec, tris, mats)
        drawables.append(dict(name=name, root=root, used=used, asset=asset, meshes=len(meshes), tris=tris, mats=mats,
                              triangles=int(sum(len(m[1]) for m in meshes)), collision_polygons=polys))
    alltris = np.concatenate([d['tris'] for d in drawables if len(d['tris'])])
    # Spawn: on the floor (its main level, not a low bit: a spawn 12 cm under Team HQ's
    # floor bailed the skater at once, 2026-10-15), nearest the lot's centre, 1 m up.
    # A lot's raw collision can reach far under what is drawn (the Skate School's plane 20 m below its
    # hills): its floor and spawn come from surfaces at or over its lowest drawn point.
    alltris = alltris[alltris[:, :, 2].mean(axis=1) > -0.5] if source == 'skate-raw' else alltris
    floor = floor_height(alltris)
    up = []
    for t in alltris:
        n = np.cross(t[1] - t[0], t[2] - t[0]); length = np.linalg.norm(n)
        if length > 1e-6 and n[2] / length > .95 and abs(t[:, 2].mean() - floor) < .03:
            up.append(t)
    spawn = spawn_point(up, np.zeros(3))
    floor_points = np.array([t.mean(axis=0) for t in up]) if up else np.zeros((0, 3))
    spawn = np.array([spawn[0], spawn[1], floor]) + origin + [0, 0, 1.0]
    size = hi - lo
    # The exit stands 3 m from the spawn, towards the lot's longer axis.
    exit_point = spawn + ([3, 0, 0] if size[0] >= size[1] else [0, 3, 0])
    return dict(key=key, name=label, district=district, origin=origin.round(3).tolist(), shift=shift.round(6).tolist(),
                spawn=spawn.round(3).tolist(), heading=0.0, exit=exit_point.round(3).tolist(),
                size=size.round(2).tolist(), floor=round(float(floor), 3), collision=source, drawables=drawables,
                floor_points=floor_points + origin)


def export(skate, reference, rage, data, world, dropper_receipt=None):
    data.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='sk8v-parks-', dir=data.parent))
    try:
        for old in data.glob('sk8v_*'):
            old.unlink()
        reference_xml = work / 'shader-reference.ydr.xml'
        run_rage(rage, 'resource', 'dump', reference, '-o', reference_xml)
        template = next(s for s in ET.parse(reference_xml).findall('./ShaderGroup/Shaders/Item')
                        if {'DiffuseSampler', 'BumpSampler', 'SpecSampler'} <= {p.get('name') for p in s.findall('./Parameters/Item')})
        if template.findtext('FileName') not in ('normal_spec.sps', f'hash_{joaat("normal_spec.sps"):08X}'):
            raise ValueError(f'shader reference is {template.findtext("FileName")}, expected normal_spec.sps')
        textures = work / 'textures'; textures.mkdir()
        lots = [lot(*spec, skate, work, template, textures) for spec in LOTS]
        # Skate 3's own parks in the lots, as the Object Dropper's models.
        for old in data.glob('*.layout'):
            old.unlink()
        if dropper_receipt and dropper_receipt.is_file():  # lots without a LAYOUTS entry have their course built in
            objects = json.loads(dropper_receipt.read_text(encoding='utf-8'))['objects']
            pieces = {i: o for o in objects if 'turn' in o for i in o.get('ids', [])}
            tinted = {(o['variant_of'], o['tint']): o['model'] for o in objects if 'variant_of' in o}
            parks = BigArchive(disc_file(skate, PARKS_BIG, work))
            for item in [i for i in lots if i['key'] in LAYOUTS]:
                entry = next(e for e in parks.entries if e.path.lower() == f'data/skatepark/{LAYOUTS[item["key"]].lower()}.park')
                placed = layout(item, park_records(parks.read(entry)), pieces, tinted)
                # Their Skate collision, in the lot's space (its pieces stand at the origin, unturned).
                templates_dir, cache, tris, mats = world.with_suffix('.prop-models'), {}, [], []
                base_model = {o['model']: o.get('variant_of', o['model']) for o in objects}
                for model, pos, q in placed:
                    if model not in cache:  # a tinted copy collides as its piece
                        cache[model] = read_svwc(templates_dir / f'{joaat(base_model.get(model, model)):08x}.svwc')
                    t3, m3 = cache[model]
                    tris.append(t3 @ quat_matrix(q).T + (pos - np.array(item['origin']))); mats += m3
                item['layout_tris'] = (np.concatenate(tris), mats) if tris else (np.zeros((0, 3, 3)), [])
                clear_spawn(item, np.concatenate(tris) + np.array(item['origin']) if tris else np.zeros((0, 3, 3)))
                rows = ['SK8VLAYOUT 1'] + [f'{joaat(m):08X} {p[0]:.4f} {p[1]:.4f} {p[2]:.4f} {q[0]:.6f} {q[1]:.6f} {q[2]:.6f} {q[3]:.6f}'
                                           for m, p, q in placed]
                (data / f'{item["key"]}.layout').write_text('\n'.join(rows) + '\n', encoding='utf-8')
                item['layout'] = {'park': entry.path, 'pieces': len(placed), 'models': len({m for m, _, _ in placed})}
                print(f'{item["name"]}: {len(placed)} pieces of {entry.path}', flush=True)
        else:
            print('no Object Dropper receipt: the lots without their Skate 3 parks', flush=True)
        pngs = sorted(textures.glob('*.png'))
        # in batches: with the normal and specular maps one command line runs past Windows' limit
        for normal, extra in ((True, ['-f', 'bc1']), (False, [])):
            batch = [p for p in pngs if p.stem.endswith('_nm') == normal]
            for at in range(0, len(batch), 150):
                run_rage(rage, 'textures', 'encode', *batch[at:at + 150], '-o', textures, *extra)
        ydrs = []
        for item in lots:
            for d in item['drawables']:
                folder = work / 'build' / d['name']; folder.mkdir(parents=True)
                for texture in d['used']:
                    shutil.copyfile(textures / f'{texture}.dds', folder / f'{texture}.dds')
                xml = folder / f'{d["name"]}.ydr.xml'; write(d['root'], xml)
                ydr = data / f'{d["name"]}.ydr'
                run_rage(rage, 'resource', 'build', xml, '-o', ydr, '--strict')
                back = folder / 'roundtrip.xml'
                run_rage(rage, 'resource', 'dump', ydr, '-o', back)
                polys = ET.parse(back).findall('./Bounds/Children/Item/Polygons/*')
                if len(polys) != d['collision_polygons']:
                    raise ValueError(f'{d["name"]}: bound has {len(polys)} polygons, expected {d["collision_polygons"]}')
                d['ydr_sha256'] = sha256_file(ydr)
                ydrs.append(ydr)
        ytyp = data / f'{YTYP}.ytyp'
        run_rage(rage, 'ytyp', 'from-drawables', *ydrs, '-o', ytyp, '--flags', ARCHETYPE_FLAGS,
                 '--lod-dist', LOD_DISTANCE, '--hd-dist', LOD_DISTANCE)
        dumped = work / 'ytyp.xml'; run_rage(rage, 'resource', 'dump', ytyp, '-o', dumped)
        archetypes = {a.findtext('name'): a for a in ET.parse(dumped).findall('./archetypes/Item')}
        for item in lots:
            for d in item['drawables']:
                a = archetypes.get(d['name'])
                if a is None or int(a.find('flags').get('value')) != ARCHETYPE_FLAGS:
                    raise ValueError(f'{d["name"]}: archetype missing or with the wrong flags')
                if d['collision_polygons'] and a.findtext('physicsDictionary') != d['name']:
                    raise ValueError(f'{d["name"]}: archetype without its embedded bound')
        # Skate's exact templates beside the world cache, and the index the host reads.
        templates = world.with_suffix('.prop-models')
        templates.mkdir(parents=True, exist_ok=True)
        hashes = []
        for item in lots:
            # The lot's Skate 3 park joins its pieces' templates (Skate only; GTA has the spawned pieces),
            # each triangle with the piece whose collision box holds its centre, else the nearest.
            extra = [([], []) for _ in item['drawables']]
            park_tris, park_mats = item.get('layout_tris', (np.zeros((0, 3, 3)), []))
            boxes = [(d['tris'].reshape(-1, 3).min(axis=0)[:2], d['tris'].reshape(-1, 3).max(axis=0)[:2]) if len(d['tris'])
                     else (np.full(2, np.inf), np.full(2, -np.inf)) for d in item['drawables']]
            for t3, m in zip(park_tris, park_mats):
                c = t3.mean(axis=0)[:2]
                inside = [i for i, (a, b) in enumerate(boxes) if (c >= a).all() and (c <= b).all()]
                i = inside[0] if inside else min(range(len(boxes)), key=lambda i: np.sum(((boxes[i][0] + boxes[i][1]) / 2 - c) ** 2)
                                                 if np.isfinite(boxes[i][0]).all() else np.inf)
                extra[i][0].append(t3); extra[i][1].append(m)
            for d, (et, em) in zip(item['drawables'], extra):
                tris = np.concatenate([d['tris'].reshape(-1, 3, 3), np.array(et).reshape(-1, 3, 3)]) if et else d['tris']
                if len(tris):
                    write_svwc(templates / f'{joaat(d["name"]):08x}.svwc', tris, list(d['mats']) + em)
                    hashes.append(joaat(d['name']))
            item.pop('layout_tris', None)
            if park_mats:
                print(f'{item["name"]}: {len(park_mats)} triangles of its Skate 3 park in its templates', flush=True)
        dropper.update_index(world, hashes)
        public = lambda i: {k: v for k, v in i.items() if k not in ('drawables', 'floor_points')} | {
            'pieces': [{k: v for k, v in d.items() if k not in ('root', 'used', 'tris', 'mats')} for d in i['drawables']]}
        (data / 'parks.json').write_text(json.dumps({'format': 'sk8v-parks-2', 'lots': [public(i) for i in lots]}, indent=2),
                                         encoding='utf-8')
        # The host's copy, one lot a line: key, name, spawn xyz, heading, exit xyz, origin xyz, reach (m), pieces
        rows = ['# SK8V park lots v2: key\tname\tspawn\theading\texit\torigin\treach\tpieces']
        xyz = lambda v: ' '.join(f'{c:.3f}' for c in v)
        for i in lots:
            reach = float(np.hypot(*i['size'][:2]) / 2 + 20)
            rows.append('\t'.join([i['key'], i['name'], xyz(i['spawn']), f"{i['heading']:.1f}", xyz(i['exit']),
                                   xyz(i['origin']), f'{reach:.1f}', ','.join(d['name'] for d in i['drawables'])]))
        (data / 'parks.txt').write_text('\n'.join(rows) + '\n', encoding='utf-8')
        receipt = {'shader_reference_sha256': sha256_file(reference), 'rage_binary_sha256': sha256_file(rage),
                   'ytyp': ytyp.name, 'ytyp_sha256': sha256_file(ytyp), 'archetype_flags': ARCHETYPE_FLAGS,
                   'templates': [f'{h:08x}' for h in hashes], 'lots': [public(i) for i in lots]}
        (data / 'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        print(json.dumps({'data': str(data), 'templates': len(hashes),
                          'lots': [(i['name'], i['spawn'], len(i['drawables'])) for i in lots]}, indent=2))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--skate', type=Path, required=True, help='Skate 3 disc image or its extracted folder')
    p.add_argument('--shader-reference', type=Path, required=True, help='owned GTA .ydr with a normal_spec shader')
    p.add_argument('--rage', type=Path, default=REPO / 'build/tools/rage.exe')
    p.add_argument('--data', type=Path, required=True, help='<assets>/private/parks: the lots and parks.txt for the host')
    p.add_argument('--world', type=Path, required=True, help='the world cache (sk8v.svwc); templates go beside it')
    p.add_argument('--dropper', type=Path, help="prepare-dropper.py's receipt.json (default: <data>/../dropper/receipt.json)")
    p.add_argument('--no-pins', action='store_true', help='skip the donor pin check (development)')
    a = p.parse_args()
    if not a.no_pins:
        verify_pins()
    export(a.skate, a.shader_reference, a.rage, a.data.resolve(), a.world.resolve(),
           (a.dropper or a.data / '../dropper/receipt.json').resolve())
