"""Minimal ECMA-335 metadata reader for PTCGL's Mono assemblies.

Reads TypeDef/MethodDef/Field tables straight out of a .NET PE file so we can
answer "which type declares this method?" without a full decompiler. Used to
pin hook targets, and to diff symbols after a game update to see what moved.

Only the tables we actually need are decoded; the rest are skipped by row size.
"""

import struct
import sys
from dataclasses import dataclass, field

# Table indices we care about (ECMA-335 II.22).
TBL_MODULE = 0x00
TBL_TYPEREF = 0x01
TBL_TYPEDEF = 0x02
TBL_FIELD = 0x04
TBL_METHODDEF = 0x06
TBL_PARAM = 0x08

# Row sizes for every table, expressed as a list of column kinds. We only spell
# out the tables up to the ones we read; later tables are sized via ROW_SHAPES.
# Column kinds: fixed-width ints, or heap/coded-index references whose width
# depends on heap sizes and row counts.


@dataclass
class TypeDef:
    namespace: str
    name: str
    methods: list = field(default_factory=list)
    fields: list = field(default_factory=list)
    # RVA of each method body, parallel to `methods`. 0 means "no body" — normal for
    # abstract/extern methods, but true of *every* method in a reference assembly.
    method_rvas: list = field(default_factory=list)

    @property
    def full_name(self):
        return f"{self.namespace}.{self.name}" if self.namespace else self.name


def _read_pe(data):
    """Locate the CLI metadata root inside a PE file. Returns (data, offset)."""
    if data[:2] != b"MZ":
        raise ValueError("not a PE file")
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_off : pe_off + 4] != b"PE\0\0":
        raise ValueError("bad PE signature")

    coff = pe_off + 4
    n_sections = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20

    magic = struct.unpack_from("<H", data, opt)[0]
    # CLI header is data directory index 14.
    dd_off = opt + (96 if magic == 0x10B else 112)
    cli_rva = struct.unpack_from("<I", data, dd_off + 14 * 8)[0]
    if cli_rva == 0:
        raise ValueError("not a managed assembly")

    sections = []
    sec_off = opt + opt_size
    for i in range(n_sections):
        s = sec_off + i * 40
        vaddr = struct.unpack_from("<I", data, s + 12)[0]
        vsize = struct.unpack_from("<I", data, s + 8)[0]
        raw = struct.unpack_from("<I", data, s + 20)[0]
        sections.append((vaddr, vsize, raw))

    def rva_to_off(rva):
        for vaddr, vsize, raw in sections:
            if vaddr <= rva < vaddr + max(vsize, 1):
                return raw + (rva - vaddr)
        raise ValueError(f"unmapped RVA {rva:#x}")

    cli_off = rva_to_off(cli_rva)
    meta_rva = struct.unpack_from("<I", data, cli_off + 8)[0]
    return rva_to_off(meta_rva)


def _read_streams(data, root):
    """Parse the metadata root and return {stream_name: (offset, size)}."""
    if data[root : root + 4] != b"BSJB":
        raise ValueError("bad metadata signature")
    ver_len = struct.unpack_from("<I", data, root + 12)[0]
    p = root + 16 + ver_len
    p += 2  # flags
    n_streams = struct.unpack_from("<H", data, p)[0]
    p += 2

    streams = {}
    for _ in range(n_streams):
        off, size = struct.unpack_from("<II", data, p)
        p += 8
        end = data.index(b"\0", p)
        name = data[p:end].decode("ascii")
        p = end + 1
        p = (p + 3) & ~3  # 4-byte aligned
        streams[name] = (root + off, size)
    return streams


def read_assembly(path):
    """Return {full_type_name: TypeDef} for a managed assembly."""
    with open(path, "rb") as fh:
        data = fh.read()

    root = _read_pe(data)
    streams = _read_streams(data, root)

    tilde = streams.get("#~") or streams["#-"]
    strings_off = streams["#Strings"][0]

    p = tilde[0]
    heap_sizes = data[p + 6]
    valid, sorted_ = struct.unpack_from("<QQ", data, p + 8)
    p += 24

    # Row count for each present table.
    rows = {}
    for i in range(64):
        if valid >> i & 1:
            rows[i] = struct.unpack_from("<I", data, p)[0]
            p += 4

    str_wide = bool(heap_sizes & 0x01)
    guid_wide = bool(heap_sizes & 0x02)
    blob_wide = bool(heap_sizes & 0x04)
    s_sz = 4 if str_wide else 2
    g_sz = 4 if guid_wide else 2
    b_sz = 4 if blob_wide else 2

    def idx_size(*tables):
        """Width of a simple index into the given table(s)."""
        return 4 if max((rows.get(t, 0) for t in tables), default=0) >= 0x10000 else 2

    def coded_size(tag_bits, tables):
        biggest = max((rows.get(t, 0) for t in tables), default=0)
        return 4 if biggest >= (1 << (16 - tag_bits)) else 2

    # Coded index widths used by the tables we must traverse or skip.
    res_scope = coded_size(2, [TBL_MODULE, 0x23, 0x26, TBL_TYPEREF])
    type_dor = coded_size(2, [TBL_TYPEDEF, TBL_TYPEREF, 0x1B])
    has_const = coded_size(2, [TBL_FIELD, TBL_PARAM, 0x17])
    has_custom = coded_size(5, list(range(0, 0x2B)))
    has_fmarshal = coded_size(1, [TBL_FIELD, TBL_PARAM])
    has_declsec = coded_size(2, [TBL_TYPEDEF, TBL_METHODDEF, 0x20])
    memberref = coded_size(3, [TBL_TYPEDEF, TBL_TYPEREF, 0x1A, TBL_METHODDEF, 0x1B])
    hassem = coded_size(1, [0x04, 0x17])
    methoddor = coded_size(1, [TBL_FIELD, TBL_METHODDEF])
    memberfwd = coded_size(1, [TBL_FIELD, TBL_METHODDEF])
    implement = coded_size(2, [TBL_FIELD, TBL_METHODDEF, 0x1A])
    custommod = coded_size(2, [TBL_TYPEDEF, TBL_TYPEREF, 0x1B])
    resolution = coded_size(2, [0x00, 0x1A, 0x23, 0x26])
    typeormethod = coded_size(1, [TBL_TYPEDEF, TBL_METHODDEF])

    # Column layout per table, as byte widths.
    shapes = {
        0x00: [2, s_sz, g_sz, g_sz, g_sz],
        0x01: [res_scope, s_sz, s_sz],
        0x02: [4, s_sz, s_sz, type_dor, idx_size(TBL_FIELD), idx_size(TBL_METHODDEF)],
        0x03: [idx_size(TBL_TYPEDEF), idx_size(TBL_FIELD)],
        0x04: [2, s_sz, b_sz],
        0x05: [idx_size(TBL_TYPEDEF), idx_size(TBL_METHODDEF)],
        0x06: [4, 2, 2, s_sz, b_sz, idx_size(TBL_PARAM)],
        0x07: [idx_size(TBL_TYPEDEF), idx_size(TBL_METHODDEF)],
        0x08: [2, 2, s_sz],
        0x09: [idx_size(TBL_TYPEDEF), type_dor],
        0x0A: [memberref, s_sz, b_sz],
        0x0B: [1, 1, has_const, b_sz],
        0x0C: [has_custom, custommod, b_sz],
        0x0D: [has_fmarshal, b_sz],
        0x0E: [2, has_declsec, b_sz],
        0x0F: [2, 4, idx_size(TBL_TYPEDEF)],
        0x10: [4, idx_size(TBL_FIELD)],
        0x11: [idx_size(TBL_TYPEDEF)],
        0x12: [2, idx_size(TBL_TYPEDEF), idx_size(TBL_METHODDEF)],
        0x14: [4, idx_size(0x17)],
        0x15: [idx_size(0x14), idx_size(TBL_PARAM)],
        0x16: [idx_size(TBL_TYPEDEF), methoddor, methoddor],
        0x17: [2, s_sz, b_sz],
        0x18: [idx_size(TBL_TYPEDEF), idx_size(TBL_METHODDEF)],
        0x19: [idx_size(TBL_METHODDEF), idx_size(TBL_METHODDEF)],
        0x1A: [s_sz],
        0x1B: [b_sz],
        0x1C: [2, memberfwd, s_sz, idx_size(0x1A)],
        0x1D: [4, idx_size(TBL_FIELD)],
        0x20: [4, b_sz, b_sz, s_sz, s_sz, b_sz],
        0x21: [4, 2, 2, 2, 4, b_sz, s_sz, s_sz],
        0x22: [4, 2, 2, 2, 4, b_sz, s_sz, s_sz, b_sz],
        0x23: [4, 2, 2, 2, 4, b_sz, s_sz, s_sz, b_sz],
        0x24: [4, s_sz, s_sz],
        0x25: [4, b_sz, s_sz, s_sz, b_sz],
        0x26: [b_sz],
        0x27: [4, s_sz, s_sz, implement],
        0x28: [4, 4, s_sz, resolution],
        0x29: [4, idx_size(TBL_TYPEDEF), idx_size(TBL_TYPEDEF)],
        0x2A: [2, 2, typeormethod, s_sz],
        0x2B: [b_sz],
        0x2C: [idx_size(0x2A), type_dor],
    }

    def read_str(off):
        end = data.index(b"\0", strings_off + off)
        return data[strings_off + off : end].decode("utf-8", "replace")

    # Read tables in order; capture the three we need, skip the rest by size.
    typedefs, methods, method_rvas, fields = [], [], [], []
    for t in sorted(rows):
        shape = shapes.get(t)
        n = rows[t]
        if shape is None:
            raise ValueError(f"unknown table {t:#x}; cannot compute row size")
        width = sum(shape)

        if t not in (TBL_TYPEDEF, TBL_METHODDEF, TBL_FIELD):
            p += width * n
            continue

        for _ in range(n):
            vals, q = [], p
            for w in shape:
                if w == 1:
                    vals.append(data[q])
                elif w == 2:
                    vals.append(struct.unpack_from("<H", data, q)[0])
                else:
                    vals.append(struct.unpack_from("<I", data, q)[0])
                q += w
            p += width

            if t == TBL_TYPEDEF:
                # TypeDef columns: Flags, Name, Namespace, Extends, FieldList, MethodList.
                typedefs.append((read_str(vals[1]), read_str(vals[2]), vals[4], vals[5]))
            elif t == TBL_METHODDEF:
                # MethodDef columns: RVA, ImplFlags, Flags, Name, Signature, ParamList.
                methods.append(read_str(vals[3]))
                method_rvas.append(vals[0])
            else:
                fields.append(read_str(vals[1]))

    # TypeDef rows point at the *start* of their run in Field/MethodDef; the run
    # ends where the next type's begins.
    result = {}
    for i, (name, ns, f_start, m_start) in enumerate(typedefs):
        f_end = typedefs[i + 1][2] if i + 1 < len(typedefs) else len(fields) + 1
        m_end = typedefs[i + 1][3] if i + 1 < len(typedefs) else len(methods) + 1
        td = TypeDef(
            namespace=ns,
            name=name,
            methods=methods[m_start - 1 : m_end - 1],
            method_rvas=method_rvas[m_start - 1 : m_end - 1],
            fields=fields[f_start - 1 : f_end - 1],
        )
        result[td.full_name] = td
    return result


def is_reference_assembly(path):
    """True if the assembly is a metadata-only reference assembly.

    Mono refuses to load these, reporting the unhelpful "File does not contain a valid
    CIL image", so it is worth catching before shipping one into a game. NuGet serves
    exactly such an assembly for some packages (Lib.Harmony.Ref), which is easy to pick
    up by accident via MSBuild's @(ReferencePath).

    Detection is by the `ReferenceAssemblyAttribute` marker, not by looking for missing
    method bodies: modern reference assemblies emit real `throw null;` bodies, so their
    method RVAs are non-zero and a body count does not distinguish them.
    """
    with open(path, "rb") as fh:
        return b"ReferenceAssemblyAttribute" in fh.read()


def find(path, needle):
    """Yield (type, kind, member) for every member matching `needle`."""
    needle = needle.lower()
    for td in read_assembly(path).values():
        for m in td.methods:
            if needle in m.lower():
                yield td.full_name, "method", m
        for f in td.fields:
            if needle in f.lower():
                yield td.full_name, "field", f
        if needle in td.name.lower():
            yield td.full_name, "type", td.name


if __name__ == "__main__":
    asm, term = sys.argv[1], sys.argv[2]
    for owner, kind, member in find(asm, term):
        print(f"{owner}\n    {kind:<7} {member}")
