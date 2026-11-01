"""Core tests (no Blender needed):  python -m pytest tests   or   python tests/test_core.py

Builds small DistantHorizons-format databases with a Python port of DH's own V2 writer
(FullDataSourceV2DTO.writeDataSourceDataArrayToBlobV2), stored uncompressed (CompressionMode 0),
then checks decoding, ring tiling, greedy merging, the Mineways hole, thin-block culling and alignment."""
import os
import random
import sqlite3
import struct
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dh_import as D  # noqa: E402

W = 64
SCHEMA = ("CREATE TABLE FullData(DetailLevel TINYINT, PosX INT, PosZ INT, MinY INT, DataChecksum INT, Data BLOB, "
          "ColumnGenerationStep BLOB, ColumnWorldCompressionMode BLOB, Mapping BLOB, DataFormatVersion TINYINT, "
          "CompressionMode TINYINT, ApplyToParent BIT, LastModifiedUnixDateTime BIGINT, CreatedUnixDateTime BIGINT, "
          "NorthAdjData BLOB, SouthAdjData BLOB, EastAdjData BLOB, WestAdjData BLOB, Regenerate BIT, "
          "PRIMARY KEY(DetailLevel, PosX, PosZ))")


# ------------------------------------------------------------------ DH V2 writer port

def _varint(v):
    o = bytearray()
    while v >= 128:
        o.append((v & 127) | 128)
        v >>= 7
    o.append(v)
    return bytes(o)


def _zz(n):
    return (n << 1) ^ (n >> 31)


def encode(cols, part):
    """cols: {(x, z): [(id, height, bottomY, sky, block), ...] top -> bottom}"""
    x0, x1, z0, z1 = {"data": (1, 63, 1, 63), "N": (0, 64, 0, 1), "S": (0, 64, 63, 64),
                      "E": (63, 64, 0, 64), "W": (0, 1, 0, 64)}[part]
    order = [(x, z) for x in range(x0, x1) for z in range(z0, z1)]
    o = bytearray()
    for k in order:
        o += _varint(len(cols.get(k, [])))
    prev = 0
    for k in order:
        for (i, h, b, sky, bl) in cols.get(k, []):
            disc = b != prev - h
            prev = b
            o += _varint((i << 2) | (2 if (sky | bl) else 0) | (1 if disc else 0))
    for k in order:
        for (i, h, b, sky, bl) in cols.get(k, []):
            o += _varint(h)
    prev = 0
    for k in order:
        for (i, h, b, sky, bl) in cols.get(k, []):
            if b != prev - h:
                o += _varint(_zz(b - (prev - h)))
            prev = b
    for k in order:
        for (i, h, b, sky, bl) in cols.get(k, []):
            if (bl << 4) | sky:
                o.append((bl << 4) | sky)
    return bytes(o)


def mapping(names):
    o = struct.pack(">i", len(names))
    for s in names:
        e = s.encode()
        o += struct.pack(">H", len(e)) + e
    return o


def make_db(sections, names):
    """sections: {(level, px, pz): cols}"""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    for (lvl, px, pz), cols in sections.items():
        con.execute("INSERT INTO FullData VALUES(?,?,?,0,0,?,?,NULL,?,2,0,0,0,0,?,?,?,?,0)",
                    (lvl, px, pz, encode(cols, "data"), bytes([9]) * 4096, mapping(names),
                     encode(cols, "N"), encode(cols, "S"), encode(cols, "E"), encode(cols, "W")))
    con.commit()
    con.close()
    return path


def terrain(fn, lvl, px, pz):
    cw = 2 ** lvl
    sw = W * cw
    cols = {}
    for rx in range(W):
        for rz in range(W):
            h = fn(px * sw + rx * cw + cw // 2, pz * sw + rz * cw + cw // 2)
            cols[(rx, rz)] = [(0, 2160 - h, h, 15, 0), (1, 1, h - 1, 0, 0), (2, h - 1, 0, 0, 0)]
    return cols


NAMES = ["minecraft:plains_DH-BSW_AIR", "minecraft:plains_DH-BSW_minecraft:grass_block_STATE_{snowy:false}",
         "minecraft:plains_DH-BSW_minecraft:stone_STATE_"]


def hills(x, z):
    return int(130 + 20 * np.sin(x / 50.0) + 15 * np.cos(z / 37.0))


# ------------------------------------------------------------------ tests

def test_decode_roundtrip():
    random.seed(7)
    cols = {}
    for x in range(W):
        for z in range(W):
            top = random.randint(60, 300)
            pts = [(0, 2160 - top, top, 15, 0)]
            y = top
            for _ in range(random.randint(1, 6)):
                if y <= 2:
                    break
                h = random.randint(1, min(40, y - 1))
                y -= h
                if random.random() < 0.2 and y > 5:
                    y -= random.randint(1, 4)       # gap -> discontinuity
                pts.append((random.randint(1, 2), h, y, random.randint(0, 15), random.randint(0, 15)))
            cols[(x, z)] = pts
    row = (encode(cols, "data"), encode(cols, "N"), encode(cols, "S"), encode(cols, "E"), encode(cols, "W"),
           mapping(NAMES), 0)
    pts, (names, biomes) = D.decode_section(row)
    got = {}
    for c, i, b, h, s in zip(pts["col"], pts["id"], pts["bottom"], pts["height"], pts["sky"]):
        got.setdefault((int(c) // W, int(c) % W), []).append((int(i), int(h), int(b), int(s)))
    for k, v in cols.items():
        assert got[k] == [(i, h, b, s) for (i, h, b, s, _) in v]
    assert biomes[0] == "minecraft:plains" and names[2] == "minecraft:stone_STATE_"


def _rings_db(cx, cz):
    secs = {}
    for lvl, reach in ((0, 200), (1, 420), (2, 800)):
        sw = W * 2 ** lvl
        for px in range((cx - reach) // sw, (cx + reach) // sw + 1):
            for pz in range((cz - reach) // sw, (cz + reach) // sw + 1):
                secs[(lvl, px, pz)] = terrain(hills, lvl, px, pz)
    return make_db(secs, NAMES)


def _import(path, cx, cz, rings, hole=None, merge=True):
    cache = D.SectionCache(path, -64, None)
    out = []
    lay = D.ring_layout(cx, cz, rings)
    for level, inner, outer in lay:
        for tile in D.ring_tiles(level, inner, outer, cx, cz):
            r = D.build_tile(cache, level, tile, inner, outer, hole, True, skirt=False, merge=merge)
            if r:
                out.append(r[0])
    cache.close()
    return lay, np.concatenate(out)


def test_rings_tile_exactly_and_merge_keeps_area():
    cx, cz = 8431, 4250
    rings = [(100, 0), (300, 1), (600, 2)]
    path = _rings_db(cx, cz)
    try:
        hole = (8381, 8480, 4200, 4299)
        areas = {}
        for merge in (False, True):
            lay, v = _import(path, cx, cz, rings, hole, merge)
            n = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 1])
            areas[merge] = np.linalg.norm(n, axis=1).sum()
            x0, x1, z0, z1 = lay[-1][2]
            grid = np.zeros((x1 - x0, z1 - z0), np.int32)
            for q in v[n[:, 2] > 0]:
                a, b = int(q[:, 0].min()), int(q[:, 0].max())
                za, zb = int(-q[:, 1].max()), int(-q[:, 1].min())
                grid[a - x0:b - x0, za - z0:zb - z0] += 1
            expect = np.ones_like(grid)
            expect[hole[0] - x0:hole[1] + 1 - x0, hole[2] - z0:hole[3] + 1 - z0] = 0
            assert (grid > 1).sum() == 0, "ring overlap"
            assert ((grid == 0) & (expect == 1)).sum() == 0, "ring gap"
            assert ((grid > 0) & (expect == 0)).sum() == 0, "faces inside hole"
        assert abs(areas[True] - areas[False]) < 1e-6 * areas[False], "greedy merge changed surface area"
    finally:
        os.remove(path)


def test_thin_snow_does_not_hide_step_walls():
    names = ["minecraft:grove_DH-BSW_AIR", "minecraft:grove_DH-BSW_minecraft:snow_STATE_{layers:1}",
             "minecraft:grove_DH-BSW_minecraft:grass_block_STATE_{snowy:true}",
             "minecraft:grove_DH-BSW_minecraft:dirt_STATE_"]
    cols = {}
    for x in range(W):
        for z in range(W):
            h = 1300 + x            # 1-block steps, snow on every step
            cols[(x, z)] = [(0, 2160 - h - 1, h + 1, 15, 0), (1, 1, h, 15, 0), (2, 1, h - 1, 0, 0), (3, h - 1, 0, 0, 0)]
    path = make_db({(0, 0, 0): cols}, names)
    try:
        cache = D.SectionCache(path, -64, None)
        lay = D.ring_layout(32, 32, [(32, 0)])
        v, rgb = D.build_tile(cache, 0, (0, 64, 0, 64), None, lay[0][2], None, True, skirt=False, merge=False)
        cache.close()
        n = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 1])
        sel = (n[:, 0] < 0) & np.isclose(v[:, :, 0].mean(1), 40) & np.isclose(-v[:, :, 1].mean(1), 10.5)
        covered = sum(v[i, :, 2].max() - v[i, :, 2].min() for i in np.flatnonzero(sel))
        assert abs(covered - 1.0) < 1e-6, f"step wall covers {covered} blocks, expected 1"
    finally:
        os.remove(path)


def test_align_finds_known_offset():
    cx, cz, oy, scale = 0, 0, 64, 1.0
    cols = {(x, z): [(0, 2160 - hills(x * 7, z * 5), hills(x * 7, z * 5), 15, 0),
                     (1, 1, hills(x * 7, z * 5) - 1, 0, 0), (2, hills(x * 7, z * 5) - 1, 0, 0, 0)]
            for x in range(W) for z in range(W)}
    path = make_db({(0, 0, 0): cols}, NAMES)
    try:
        cache = D.SectionCache(path, -64, None)
        truth = D.dh_surface_grid(cache, 0, 64, 0, 64)
        sx, sz, dy = 5, -3, 2
        tx, tz = np.meshgrid(np.arange(10, 54), np.arange(10, 54), indexing="ij")
        tx, tz = tx.ravel(), tz.ravel()
        h = truth[tx, tz] + dy
        C = np.stack([(tx - sx + 0.5 - cx) * scale, (cz - (tz - sz + 0.5)) * scale, (h - oy) * scale], -1)
        N = np.tile([0, 0, 1.0], (len(C), 1))
        A = np.full(len(C), scale * scale)
        mx, mz, mh = D.mesh_heightmap(C, N, A, scale, cx, cz, oy)
        S = 12
        x0, z0 = int(mx.min()) - S, int(mz.min()) - S
        grid = D.dh_surface_grid(cache, x0, int(mx.max()) + S + 1, z0, int(mz.max()) + S + 1)
        cache.close()
        r = D.best_shift(mx, mz, mh, grid, x0, z0, S)
        assert r[:3] == (sx, sz, float(dy)), r
    finally:
        os.remove(path)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
