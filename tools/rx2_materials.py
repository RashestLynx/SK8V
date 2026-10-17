"""Each render mesh's own material in a Skate 3 model RX2 (RenderWare 4 arena).

mdl_parser pairs a model's meshes with its material table by position (mesh i, material i),
but the table is in its own order: a mesh's render record (type 0x00EB0023) names its
material by an arena subreference (0x800000 | n) whose record is (dictionary object, offset
into that material table), the table's entry k sitting at its array offset + 12 k. Hartley
Stadium's floor drew with the stadium banner (jerseys) and Downtown's walls with blue doors
by position (2026-10-17); by reference they are concrete.

mesh_materials(raw) -> per mesh (mdl_parser's order), its material's parameters {kind: value}
(Name, diffuse, normal, specular, ...), or None when the model does not resolve that way.
"""
import struct

TOC_RECORD = 24
TYPE_VERTEX, TYPE_FACE = b'\x00\x02\x00\xEA', b'\x00\x02\x00\xEB'
TYPE_MATERIAL, TYPE_RENDER = b'\x00\xEB\x00\x05', b'\x00\xEB\x00\x23'
SECTION_SUBREFERENCES = 0x00010007


def _u32(raw, at):
    return struct.unpack_from('>I', raw, at)[0]


def _cstring(raw, at):
    end = raw.index(b'\0', at)
    return raw[at:end].decode('latin-1')


def _material_table(raw, at):
    """[(params dict)] in table order: header (count, params, array offset, params offset, ...),
    the array's 12-byte entries (param count, 0, param block offset), 32-byte params (kind, value)."""
    count, _, array, _ = struct.unpack_from('>4i', raw, at)
    out = []
    for k in range(count):
        n, _, block = struct.unpack_from('>3i', raw, at + array + 12 * k)
        params = {}
        for j in range(n):
            p = at + block + 32 * j
            kind = _cstring(raw, at + struct.unpack_from('>i', raw, p)[0])
            value_at = struct.unpack_from('>i', raw, p + 24)[0]
            try:
                params.setdefault(kind, _cstring(raw, at + value_at))
            except (ValueError, IndexError):
                pass
        out.append(params)
    return out, array


def mesh_materials(raw):
    try:
        count, table = _u32(raw, 0x20), _u32(raw, 0x30)
        toc = [(raw[table + i * TOC_RECORD + 20:table + i * TOC_RECORD + 24],
                struct.unpack_from('>5I', raw, table + i * TOC_RECORD)) for i in range(count)]
        # sections: the manifest (at 0x34) lists them by offset from itself
        manifest = _u32(raw, 0x34)
        n_sections = _u32(raw, manifest + 8)
        subrefs = None
        for k in range(n_sections):
            sec = manifest + _u32(raw, manifest + 12 + 4 * k)
            if sec + 24 <= len(raw) and _u32(raw, sec) == SECTION_SUBREFERENCES:
                n, records = _u32(raw, sec + 4), _u32(raw, sec + 20)
                subrefs = [struct.unpack_from('>2I', raw, records + 8 * i) for i in range(n)]
        if subrefs is None:
            return None
        tables = {}
        for i, (kind, values) in enumerate(toc):
            if kind == TYPE_MATERIAL:
                tables[i] = _material_table(raw, values[0])
        # the meshes, as mdl_parser counts them: a vertex buffer with a face buffer two records on
        renders = [values for kind, values in toc if kind == TYPE_RENDER]
        meshes = sum(1 for i, (kind, _) in enumerate(toc)
                     if kind == TYPE_VERTEX and i + 2 < len(toc) and toc[i + 2][0] == TYPE_FACE)
        if len(renders) != meshes:
            return None
        out = []
        for values in renders:
            ints = struct.unpack_from(f'>{values[2] // 4}I', raw, values[0])
            ref = next((x for x in ints[8:] if x & 0x800000 and x < 0x1000000), None)
            if ref is None or (ref & 0x7FFFFF) >= len(subrefs):
                return None
            obj, offset = subrefs[ref & 0x7FFFFF]
            if obj not in tables:
                return None
            materials, array = tables[obj]
            k, rest = divmod(offset - array, 12)
            if rest or not 0 <= k < len(materials):
                return None
            out.append(materials[k])
        return out
    except (struct.error, ValueError, IndexError):
        return None


def resolve(parsed, raw):
    """mdl_parser's model with each mesh's name and material_name (its diffuse) from its own
    material, as mesh_materials resolves them; as parsed where the model does not resolve."""
    materials = mesh_materials(raw)
    if materials is None or len(materials) != len(parsed.meshes):
        return parsed
    for mesh, material in zip(parsed.meshes, materials):
        if material.get('Name'):
            mesh.name = material['Name']
        if material.get('diffuse'):
            mesh.material_name = material['diffuse']
    return parsed
