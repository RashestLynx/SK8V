"""Builds Skate 3's skate.Park lots (Team HQ, Hartley Stadium) as a GTA map,
from the player's own disc. Nothing here is shipped: setup runs it on the
player's PC.

    python tools/prepare-park-lots.py --skate <disc image or extracted folder>
        --shader-reference <owned normal_spec .ydr> --rage build/tools/rage.exe
        --pack <live-clip>/dlc.rpf --out <out>/parks --data <assets>/private/parks

Source (owned disc): data/content/worldDIST_<lot>.big holds the lot's world
stream (data/content/world/stream/DIST_<lot>): the global presentation stream
has its models (mdl_parser) and textures (rx2_parser), the global simulation
stream Skate's collision (RenderWare ClusteredMesh, retail_collision_mesh).
A mesh's material name ends in its diffuse texture id.

Output:
- <out>/sk8v_parks.rpf: a GTA map: per lot its drawables (normal_spec, embedded
  textures), one static collision bound (.ybn, world space) and a .ymap placing
  the lot far out over the sea; sk8v_parks.ytyp and _manifest.ymf.
- <out>/dlc.rpf: SK8V's pack (--pack, from live_clip) with the map added and
  enabled at startup (content.xml), so Story Mode streams the lots like any map
  and GTA's physics level holds their collision (the runtime reads it, live.rs).
- <data>/parks.json (and parks.txt, which the host's park_lot reads): per lot its
  name, where it is, the spawn and exit points; receipt.json.
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
RPF = 'sk8v_parks.rpf'
LOD_DISTANCE = 600
# Each lot: (key, display name, district, GTA origin of its floor centre, collision source).
# Far out west over the sea, apart (each Skate area is 128 m around the skater), well above
# the water; both lots are roofed halls. Team HQ's own collision is complete; Hartley Stadium's
# ground is in simulation records nobody decodes yet (eb0027), so its visible geometry collides.
LOTS = [
    ('teamhq', 'Team HQ', 'DIST_IndustrialSkatePark', (-4600.0, -3400.0, 400.0), 'skate'),
    ('stadium', 'Hartley Stadium', 'DIST_MegaPark', (-4600.0, -2600.0, 400.0), 'render'),
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
            p = np.asarray(mesh.vertices, np.float64) @ C.T
            faces = np.asarray(mesh.faces, np.int64).reshape(-1, 3)
            texture = re.search(r'(0x[0-9A-Fa-f]{16})$', mesh.material_name or '')
            meshes.append((p, faces, np.asarray(mesh.uvs, np.float64), mesh.material_name or '',
                           texture.group(1).lower() if texture else None))
        if meshes:
            out.append((f'{asset.record.asset_id:016x}', meshes))
    return out


def drawable(name, meshes, textures, template, flat, spec):
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
    node(root, 'Lights')
    return root, used


# Static map collision: what GTA's own map bounds carry (reh_dlc_int_03_sum2.ybn).
FLAGS1 = 'MAP_WEAPON, MAP_DYNAMIC, MAP_ANIMAL, MAP_COVER, MAP_VEHICLE'
FLAGS2 = ('VEHICLE_NOT_BVH, VEHICLE_BVH, PED, RAGDOLL, ANIMAL, ANIMAL_RAGDOLL, OBJECT, PLANT, PROJECTILE, EXPLOSION, '
          'FORKLIFT_FORKS, TEST_WEAPON, TEST_CAMERA, TEST_AI, TEST_SCRIPT, TEST_VEHICLE_WHEEL, GLASS')


def collision(tris, materials):
    """A static map bound (BVH geometry) over world-space triangles, one GTA material each."""
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
    root = ET.Element('BoundsFile')
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
    return root, len(polys)


def ymap(name, entities, lo, hi):
    """CMapData placing the lot's drawables (rage `resource recalc` sets extents and flags)."""
    root = ET.Element('CMapData')
    node(root, 'name', name); node(root, 'parent'); node(root, 'flags', value=0); node(root, 'contentFlags', value=1)
    vec(root, 'streamingExtentsMin', lo - LOD_DISTANCE); vec(root, 'streamingExtentsMax', hi + LOD_DISTANCE)
    vec(root, 'entitiesExtentsMin', lo); vec(root, 'entitiesExtentsMax', hi)
    items = node(root, 'entities')
    for archetype, position in entities:
        e = node(items, 'Item', type='CEntityDef')
        node(e, 'archetypeName', archetype); node(e, 'flags', value=0); node(e, 'guid', value=joaat(archetype))
        vec(e, 'position', position); vec(e, 'rotation', [0, 0, 0, 1])
        node(e, 'scaleXY', value=1); node(e, 'scaleZ', value=1); node(e, 'parentIndex', value=-1)
        node(e, 'lodDist', value=LOD_DISTANCE); node(e, 'childLodDist', value=0)
        node(e, 'lodLevel', 'LODTYPES_DEPTH_ORPHANHD'); node(e, 'numChildren', value=0)
        node(e, 'priorityLevel', 'PRI_REQUIRED'); node(e, 'extensions')
        node(e, 'ambientOcclusionMultiplier', value=255); node(e, 'artificialAmbientOcclusion', value=255)
        node(e, 'tintValue', value=0)
    for tag in ['containerLods', 'boxOccluders', 'occludeModels', 'physicsDictionaries', 'instancedData',
                'timeCycleModifiers', 'carGenerators', 'LODLightsSOA', 'DistantLODLightsSOA']:
        node(root, tag)
    block = node(root, 'block')
    for key, value in [('version', 0), ('flags', 0)]:
        node(block, key, value=value)
    for key, text in [('name', name), ('exportedBy', 'SK8V prepare-park-lots.py'), ('owner', ''), ('time', '')]:
        node(block, key, text)
    return root


def write(root, path):
    ET.indent(root); ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)


def spawn_point(floor_tris, centre):
    """The floor point nearest the lot's centre (GTA space), and the heading facing the centre's longer side."""
    best, best_d = None, 1e18
    for t in floor_tris:
        c = t.mean(axis=0)
        d = np.sum((c[:2] - centre[:2]) ** 2)
        if d < best_d:
            best, best_d = c, d
    return best


def lot(key, label, district, origin, source, skate, work, template, textures_out, rage):
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
    drawables = []
    for i, (asset, meshes) in enumerate(parts):
        name = f'sk8v_{key}_{i}'
        root, used = drawable(name, meshes, textures, template, flat, spec)
        drawables.append(dict(name=name, root=root, used=used, asset=asset, meshes=len(meshes),
                              triangles=int(sum(len(m[1]) for m in meshes))))
    if source == 'skate':
        tris, mats = [], []
        for asset in sim:
            for mesh in decode_rx2_clustered_meshes(asset.data):
                for t in mesh.triangles:
                    tris.append(np.array([t.a, t.b, t.c], np.float64) @ C.T + shift)
                    mats.append(CONCRETE)
    else:
        tris, mats = [], []
        for _, meshes in parts:
            for p, faces, _, material, _ in meshes:
                m = surface(material)
                for face in faces:
                    tris.append(p[face]); mats.append(m)
    tris = np.array(tris)
    bound, polys = collision(tris + origin, mats)
    write(bound, lot_work / f'sk8v_{key}.ybn.xml')
    # Floor: upward faces within half a metre of the lot's floor height.
    up = []
    for t in tris:
        n = np.cross(t[1] - t[0], t[2] - t[0]); length = np.linalg.norm(n)
        if length > 1e-6 and n[2] / length > .95 and abs(t[:, 2].mean()) < .5:
            up.append(t)
    spawn = spawn_point(up, np.zeros(3)) + origin + [0, 0, 1.0]
    size = hi - lo
    # The exit stands 3 m from the spawn, towards the lot's longer axis.
    exit_point = spawn + ([3, 0, 0] if size[0] >= size[1] else [0, 3, 0])
    entities = [(d['name'], origin) for d in drawables]
    span_lo, span_hi = origin + (lo + shift), origin + (hi + shift)
    map_root = ymap(f'sk8v_{key}', entities, span_lo, span_hi)
    write(map_root, lot_work / f'sk8v_{key}.ymap.xml')
    return dict(key=key, name=label, district=district, origin=origin.round(3).tolist(),
                spawn=spawn.round(3).tolist(), heading=0.0, exit=exit_point.round(3).tolist(),
                size=size.round(2).tolist(), collision=source, collision_polygons=polys,
                drawables=drawables, work=lot_work)


def store_binaries(path):
    """Marks an OPEN RPF7's plain (non-resource) files as stored.

    `rage create` copies plain files raw but writes their on-disk size as their
    full size, which GTA reads as "deflated, this long": it then fails to
    inflate them (ERR_FIL_PACK_3 "Corrupt game data", 2026-10-15). Stored
    files carry on-disk size 0, as live_clip's pack and Rockstar's nested
    archives do. Resources (their own deflate stream and page flags) are left
    as they are."""
    data = bytearray(Path(path).read_bytes())
    magic, count, names, encryption = struct.unpack_from('<4I', data, 0)
    if magic != 0x52504637 or encryption != 0x4E45504F:
        raise ValueError(f'{path}: not an OPEN RPF7 archive')
    patched = 0
    for i in range(count):
        at = 16 + 16 * i
        if struct.unpack_from('<I', data, at + 4)[0] == 0x7FFFFF00:
            continue  # a directory
        if data[at + 7] & 0x80:
            continue  # a resource
        on_disk = int.from_bytes(data[at + 2:at + 5], 'little')
        size = struct.unpack_from('<I', data, at + 8)[0]
        if on_disk and on_disk == size:
            data[at + 2:at + 5] = b'\0\0\0'
            patched += 1
        elif on_disk:
            raise ValueError(f'{path}: entry {i} is really compressed ({on_disk} of {size} bytes)')
    Path(path).write_bytes(bytes(data))
    return patched


def pack_with_map(rage, pack, map_rpf, work, out, ityp_request=False):
    """SK8V's pack plus the map, enabled at startup in its one change set
    (`ityp_request`: also a DLC_ITYP_REQUEST for the lots' types). Off by default:
    bisected in game 2026-10-15, that request crashes Story Mode while loading;
    the map rpf alone mounts fine."""
    tree = work / 'pack'
    shutil.rmtree(tree, ignore_errors=True)
    run_rage(rage, 'extract', pack, '-o', tree)
    levels = tree / 'x64/levels/gta5'; levels.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(map_rpf, levels / RPF)
    content = tree / 'content.xml'
    text = content.read_text(encoding='utf-8')
    if RPF in text:
        raise ValueError('the pack already lists the park map')
    path = f'dlc_skatev:/%PLATFORM%/levels/gta5/{RPF}'
    item = (f'    <Item>\n      <filename>{path}</filename>\n      <fileType>RPF_FILE</fileType>\n'
            '      <overlay value="false" />\n      <disabled value="true" />\n      <persistent value="true" />\n'
            '      <contents>CONTENTS_MAP</contents>\n    </Item>\n')
    enable = f'        <Item>{path}</Item>\n'
    if ityp_request:
        item += (f'    <Item>\n      <filename>{path}/{YTYP}.ytyp</filename>\n      <fileType>DLC_ITYP_REQUEST</fileType>\n'
                 '      <overlay value="false" />\n      <disabled value="true" />\n      <persistent value="true" />\n    </Item>\n')
        enable += f'        <Item>{path}/{YTYP}.ytyp</Item>\n'
    if text.count('</dataFiles>') != 1 or text.count('</filesToEnable>') != 1:
        raise ValueError('unexpected content.xml layout')
    text = text.replace('  </dataFiles>', item + '  </dataFiles>')
    text = text.replace('      </filesToEnable>', enable + '      </filesToEnable>')
    content.write_text(text, encoding='utf-8')
    run_rage(rage, 'create', tree, '-o', out, '-e', 'open')
    store_binaries(out)


def export(skate, reference, rage, pack, out, data):
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='sk8v-parks-', dir=out.parent))
    try:
        shutil.rmtree(out, ignore_errors=True); out.mkdir(parents=True)
        map_tree = work / 'map'; map_tree.mkdir()
        reference_xml = work / 'shader-reference.ydr.xml'
        run_rage(rage, 'resource', 'dump', reference, '-o', reference_xml)
        template = next(s for s in ET.parse(reference_xml).findall('./ShaderGroup/Shaders/Item')
                        if {'DiffuseSampler', 'BumpSampler', 'SpecSampler'} <= {p.get('name') for p in s.findall('./Parameters/Item')})
        textures = work / 'textures'; textures.mkdir()
        lots = [lot(*spec, skate, work, template, textures, rage) for spec in LOTS]
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
                ydr = map_tree / f'{d["name"]}.ydr'
                run_rage(rage, 'resource', 'build', xml, '-o', ydr, '--strict')
                ydrs.append(ydr)
            run_rage(rage, 'resource', 'build', item['work'] / f'sk8v_{item["key"]}.ybn.xml',
                     '-o', map_tree / f'sk8v_{item["key"]}.ybn')
            run_rage(rage, 'resource', 'build', item['work'] / f'sk8v_{item["key"]}.ymap.xml',
                     '-o', map_tree / f'sk8v_{item["key"]}.ymap')
        run_rage(rage, 'ytyp', 'from-drawables', *ydrs, '-o', map_tree / f'{YTYP}.ytyp',
                 '--flags', 0, '--lod-dist', LOD_DISTANCE, '--hd-dist', LOD_DISTANCE)
        run_rage(rage, 'resource', 'recalc', map_tree)
        run_rage(rage, 'manifest', 'generate', map_tree, '-o', map_tree / '_manifest.ymf')
        # Every bound came back with its polygons (rage round trip).
        for item in lots:
            back = work / f'{item["key"]}.ybn.roundtrip.xml'
            run_rage(rage, 'resource', 'dump', map_tree / f'sk8v_{item["key"]}.ybn', '-o', back)
            polys = ET.parse(back).findall('.//Polygons/*')
            if len(polys) != item['collision_polygons']:
                raise ValueError(f'{item["key"]}: bound has {len(polys)} polygons, expected {item["collision_polygons"]}')
        map_rpf = out / RPF
        run_rage(rage, 'create', map_tree, '-o', map_rpf, '-e', 'open')
        store_binaries(map_rpf)
        pack_with_map(rage, pack, map_rpf, work, out / 'dlc.rpf')
        data.mkdir(parents=True, exist_ok=True)
        parks = {'format': 'sk8v-parks-1', 'lots': [{k: v for k, v in i.items() if k not in ('drawables', 'work')} for i in lots]}
        (data / 'parks.json').write_text(json.dumps(parks, indent=2), encoding='utf-8')
        # The host's copy, one lot a line: key, name, spawn xyz, heading, exit xyz, origin xyz, reach (m)
        rows = ['# SK8V park lots v1: key\tname\tspawn\theading\texit\torigin\treach']
        xyz = lambda v: ' '.join(f'{c:.3f}' for c in v)
        for i in lots:
            reach = float(np.hypot(*i['size'][:2]) / 2 + 20)
            rows.append('\t'.join([i['key'], i['name'], xyz(i['spawn']), f"{i['heading']:.1f}", xyz(i['exit']),
                                   xyz(i['origin']), f'{reach:.1f}']))
        (data / 'parks.txt').write_text('\n'.join(rows) + '\n', encoding='utf-8')
        receipt = {'shader_reference_sha256': sha256_file(reference), 'rage_binary_sha256': sha256_file(rage),
                   'pack_source_sha256': sha256_file(pack), 'pack_sha256': sha256_file(out / 'dlc.rpf'),
                   'map_sha256': sha256_file(map_rpf),
                   'lots': [{k: v for k, v in i.items() if k not in ('drawables', 'work')} |
                            {'drawables': [{k: v for k, v in d.items() if k not in ('root', 'used')} for d in i['drawables']]}
                            for i in lots]}
        (data / 'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        print(json.dumps({'map': str(map_rpf), 'pack': str(out / 'dlc.rpf'), 'parks': str(data / 'parks.json'),
                          'lots': [(i['name'], i['spawn']) for i in lots]}, indent=2))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--skate', type=Path, required=True, help='Skate 3 disc image or its extracted folder')
    p.add_argument('--shader-reference', type=Path, required=True, help='owned GTA .ydr with a normal_spec shader')
    p.add_argument('--rage', type=Path, default=REPO / 'build/tools/rage.exe')
    p.add_argument('--pack', type=Path, required=True, help="live_clip's dlc.rpf")
    p.add_argument('--out', type=Path, required=True, help='folder for the map and the combined pack')
    p.add_argument('--data', type=Path, required=True, help='<assets>/private/parks: parks.json for the host')
    p.add_argument('--no-pins', action='store_true', help='skip the donor pin check (development)')
    a = p.parse_args()
    if not a.no_pins:
        verify_pins()
    export(a.skate, a.shader_reference, a.rage, a.pack, a.out.resolve(), a.data.resolve())
