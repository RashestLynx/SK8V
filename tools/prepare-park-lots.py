"""Builds Skate 3's skate.Park lots (Team HQ, Hartley Stadium) as GTA objects,
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
Space: Skate's Y-up metres -> GTA C=(x,-z,y) (as prepare-dropper.py), centred
on X/Y, the floor at the lot's origin height.
"""
import argparse
import copy
import importlib.util
import json
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
# Each lot: (key, display name, district, GTA origin of its floor centre, collision source).
# Far out west over the sea, apart (each Skate area is 128 m around the skater), 60 m above
# the water (a fall lands in the sea; not up near the game's height limit); roofed halls. Team HQ's own collision is complete; Hartley Stadium's
# ground is in simulation records nobody decodes yet (eb0027), so its visible geometry collides.
LOTS = [
    ('teamhq', 'Team HQ', 'DIST_IndustrialSkatePark', (-4600.0, -3400.0, 60.0), 'skate'),
    ('stadium', 'Hartley Stadium', 'DIST_MegaPark', (-4600.0, -2600.0, 60.0), 'render'),
]
# GTA materials.dat indices by the Skate 3 material name's surface word (rust/skatev-runtime/src/materials.rs).
SURFACE = [('wood', 70), ('metal', 58), ('rail', 59), ('plastic', 84), ('glass', 4), ('grass', 49),
           ('dirt', 32), ('sand', 31), ('brick', 1), ('concrete', 1), ('asphalt', 6), ('tile', 1)]
CONCRETE = 1


def surface(material_name):
    low = (material_name or '').lower()
    return next((m for word, m in SURFACE if word in low), CONCRETE)


def disc_lot(skate, district, work):
    """The lot's .big from the disc image (or an extracted folder)."""
    name = f'data/content/world{district}.big'
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

    def common(self):
        for name, colour in [('sk8v_flat_nm', (128, 128, 255, 255)), ('sk8v_spec', (60, 60, 60, 255))]:
            Image.fromarray(np.full((4, 4, 4), colour, np.uint8), 'RGBA').save(self.work / f'{name}.png')
        return 'sk8v_flat_nm', 'sk8v_spec'


def models(assets):
    """Every render mesh in GTA space: (points, faces, uvs, material name, texture id)."""
    out = []
    for asset in assets:
        if asset.record.asset_type != ASSET_TYPE_MODEL:
            continue
        parsed = parse_rx2(asset.data, strict=False)
        meshes = []
        for mesh in parsed.meshes:
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
        shaders_used.append((diffuse, cutout))
    buckets = (8 if any(c for _, c in shaders_used) else 0) | (1 if not all(c for _, c in shaders_used) else 0)
    for level in ['High', 'Med', 'Low', 'Vlow']:
        node(root, 'LodDist' + level, value=9998)
    for level in ['High', 'Med', 'Low', 'Vlow']:
        node(root, 'Flags' + level, value=buckets if level == 'High' else 0)
    group = node(root, 'ShaderGroup'); dictionary = node(group, 'TextureDictionary'); shaders = node(group, 'Shaders')
    used = []
    for diffuse, cutout in shaders_used:
        names = {'DiffuseSampler': diffuse, 'BumpSampler': flat, 'SpecSampler': spec}
        for texture in names.values():
            if texture not in used:
                used.append(texture)
        shader = copy.deepcopy(template)
        shader.find('RenderBucket').set('value', '3' if cutout else '0')
        for param in shader.findall('./Parameters/Item'):
            key = param.get('name')
            if key in names: param.find('Name').text = names[key]
            if key == 'wetnessMultiplier': param.set('x', '0.2')
            if key == 'specularIntensityMult': param.set('x', '0.15')
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


def drop_hidden(tris, render, reach=1.0, rise=.1):
    """Skate 3's Team HQ grows as the player's team does, and its collision holds every
    stage: 5-15 m walls across the hall (one beside the spawn), a 20 cm slab under it,
    and floor and roof outside the building, none of them in the stage we draw (the
    player's invisible walls, 2026-10-16). A collision face is kept when at least a
    third of it lies near visible faces of its kind: a wall within `reach` of a visible
    wall, a floor (or roof, or ramp) within `reach` across and `rise` up or down of a
    visible one. (A wall near only the floor is not seen.)"""
    def steep(t):
        n = np.cross(t[1] - t[0], t[2] - t[0]); length = np.linalg.norm(n)
        return length < 1e-12 or abs(n[2]) / length < .5
    cells = {True: np.array([reach / 2] * 3), False: np.array([reach / 2, reach / 2, rise / 2])}
    seen = {True: set(), False: set()}
    for r in render:
        kind = steep(r)
        for q in np.floor(surface_points(r, .25) / cells[kind]).astype(np.int64):
            seen[kind].add((int(q[0]), int(q[1]), int(q[2])))
    span = range(-2, 3)
    rises = {True: span, False: range(-1, 2)}  # a floor: within 5-10 cm up or down
    out, dropped = [], 0
    for t3 in tris:
        kind = steep(t3)
        near_cells = seen[kind]
        pts = np.floor(surface_points(t3, reach / 2) / cells[kind]).astype(np.int64)
        near = sum(any((int(x) + dx, int(y) + dy, int(z) + dz) in near_cells for dx in span for dy in span for dz in rises[kind])
                   for x, y, z in pts)
        if near * 3 >= len(pts):
            out.append(t3)
        else:
            dropped += 1
    return out, dropped


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
    area = {}
    for t in tris:
        n = np.cross(t[1] - t[0], t[2] - t[0]); length = np.linalg.norm(n)
        z = t[:, 2].mean()
        if length > 1e-9 and n[2] / length > .95 and z < .6:
            key = round(float(z), 2)
            area[key] = area.get(key, 0.0) + length / 2
    return max(area, key=area.get)


def spawn_point(floor_tris, centre):
    """The floor point nearest the lot's centre (GTA space), and the heading facing the centre's longer side."""
    best, best_d = None, 1e18
    for t in floor_tris:
        c = t.mean(axis=0)
        d = np.sum((c[:2] - centre[:2]) ** 2)
        if d < best_d:
            best, best_d = c, d
    return best


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
    if source == 'skate':
        boxes = []
        for _, meshes in parts:
            pts = np.concatenate([m[0] for m in meshes])
            boxes.append((pts.min(axis=0)[:2] - 1, pts.max(axis=0)[:2] + 1))
        lot_tris = [np.array([tri.a, tri.b, tri.c], np.float64) @ C.T + shift
                    for asset in sim for mesh in decode_rx2_clustered_meshes(asset.data) for tri in mesh.triangles]
        lot_tris, flipped = face_up(lot_tris, floor_height(lot_tris) + 20.0)
        print(f'{label}: {flipped} downward-only floor faces turned up', flush=True)
        lot_tris, hidden = drop_hidden(lot_tris, [p[face] for _, meshes in parts for p, faces, *_ in meshes for face in faces])
        print(f'{label}: {hidden} collision faces with nothing visible there dropped', flush=True)
        for t3 in lot_tris:
            c = t3.mean(axis=0)[:2]
            inside = [i for i, (a, b) in enumerate(boxes) if (c >= a).all() and (c <= b).all()]
            if inside:
                i = min(inside, key=lambda i: np.prod(boxes[i][1] - boxes[i][0]))
            else:
                i = min(range(len(boxes)), key=lambda i: np.sum(((boxes[i][0] + boxes[i][1]) / 2 - c) ** 2))
            owned[i][0].append(t3); owned[i][1].append(CONCRETE)
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
        root, used, polys = drawable(name, meshes, textures, template, flat, spec, tris, mats)
        drawables.append(dict(name=name, root=root, used=used, asset=asset, meshes=len(meshes), tris=tris, mats=mats,
                              triangles=int(sum(len(m[1]) for m in meshes)), collision_polygons=polys))
    alltris = np.concatenate([d['tris'] for d in drawables if len(d['tris'])])
    # Spawn: on the floor (its main level, not a low bit: a spawn 12 cm under Team HQ's
    # floor bailed the skater at once, 2026-10-15), nearest the lot's centre, 1 m up.
    floor = floor_height(alltris)
    up = []
    for t in alltris:
        n = np.cross(t[1] - t[0], t[2] - t[0]); length = np.linalg.norm(n)
        if length > 1e-6 and n[2] / length > .95 and abs(t[:, 2].mean() - floor) < .03:
            up.append(t)
    spawn = spawn_point(up, np.zeros(3))
    spawn = np.array([spawn[0], spawn[1], floor]) + origin + [0, 0, 1.0]
    size = hi - lo
    # The exit stands 3 m from the spawn, towards the lot's longer axis.
    exit_point = spawn + ([3, 0, 0] if size[0] >= size[1] else [0, 3, 0])
    return dict(key=key, name=label, district=district, origin=origin.round(3).tolist(),
                spawn=spawn.round(3).tolist(), heading=0.0, exit=exit_point.round(3).tolist(),
                size=size.round(2).tolist(), floor=round(float(floor), 3), collision=source, drawables=drawables)


def export(skate, reference, rage, data, world):
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
        pngs = sorted(textures.glob('*.png'))
        run_rage(rage, 'textures', 'encode', *[p for p in pngs if p.stem.endswith('_nm')], '-o', textures, '-f', 'bc1')
        run_rage(rage, 'textures', 'encode', *[p for p in pngs if not p.stem.endswith('_nm')], '-o', textures)
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
            for d in item['drawables']:
                if len(d['tris']):
                    write_svwc(templates / f'{joaat(d["name"]):08x}.svwc', d['tris'], d['mats'])
                    hashes.append(joaat(d['name']))
        dropper.update_index(world, hashes)
        public = lambda i: {k: v for k, v in i.items() if k != 'drawables'} | {
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
    p.add_argument('--no-pins', action='store_true', help='skip the donor pin check (development)')
    a = p.parse_args()
    if not a.no_pins:
        verify_pins()
    export(a.skate, a.shader_reference, a.rage, a.data.resolve(), a.world.resolve())
