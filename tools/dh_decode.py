#!/usr/bin/env python3
"""
dh_decode.py - standalone decoder / inspector for Distant Horizons FullData (DataFormatVersion 2).

Standalone (no Blender). Read-only. Memory-light: one section at a time, or a
streamed scan of the tiny ColumnGenerationStep blobs for the survey.

Format (from DH core: FullDataSourceV2DTO / FullDataPointIdMap / VarintUtil):
  * Every blob is compressed per the row's CompressionMode (4 = Z_STD_BLOCK = plain zstd frame).
  * A section is 64 x 64 columns. Column index = relX * 64 + relZ.
    Section detail level = 6 + DetailLevel, so each column is 2**DetailLevel blocks wide
    and the section's min corner block = PosX * 64 * 2**DetailLevel.
  * Data blob covers x 1..62, z 1..62; North/South/East/West blobs cover the 4 edges
    (N: z=0, S: z=63, E: x=63, W: x=0). Each part is five varint streams, columns in
    x-outer / z-inner order:
      1. per-column point count
      2. per point: (mappingId << 2) | (hasLight << 1) | hasDiscontinuity
      3. per point: height (thickness in blocks)
      4. per discontinuous point: zigzag(bottomY - expectedBottomY)
         where expectedBottomY = previousBottomY - height (previous carries across columns)
      5. per lit point: one byte (blockLight << 4) | skyLight
    Points in a column are ordered top -> bottom. bottomY is relative to the world's min Y.
  * Mapping blob: int32 BE count, then Java writeUTF strings "<biome>_DH-BSW_<blockstate>".
  * ColumnGenerationStep blob: 4096 bytes, signed. -1 = DOWN_SAMPLED, 0 = EMPTY,
    5 = SURFACE, 7 = TERRAIN, 8 = FEATURES, 9 = LIGHT ...

Requirements:  py -m pip install numpy zstandard

Usage:
  py dh_decode.py DB survey
      Per detail level: section count, how many columns are real vs down-sampled,
      and the block area covered by real data. Streams; takes a minute or two.

  py dh_decode.py DB section --level 0 --block 0 0
      Decode the section at that detail level containing block (X, Z) (or the nearest
      one that exists), print stats, and write PNG previews + an .npz to --out.
  py dh_decode.py DB section --level 3 --pos -56 -8
      Same, but pick the section by its PosX/PosZ.

  py dh_decode.py DB coverage --mark X Z
      Writes dh_decode_out\\coverage.png: a map colored by the finest REAL detail level
      (white = full 1-block detail). Lists the areas with the most full-detail data.

  py dh_decode.py DB probe --block X Z
      For one spot, shows what every detail level 0-9 has there (real or down-sampled,
      surface height, surface block).

Options for 'section':
  --world-min-y N   world bottom Y (vanilla 1.20.1 overworld = -64). Default -64.
  --out DIR         output folder (default: dh_decode_out next to this script)
"""
import argparse
import os
import sqlite3
import struct
import sys
import time
import urllib.request
import zlib

try:
    import numpy as np
except ImportError:
    sys.exit("numpy is missing:  py -m pip install numpy zstandard")

W = 64
N_COLS = W * W
AIR_NAMES = {"AIR", "minecraft:air", "minecraft:cave_air", "minecraft:void_air"}
GEN_STEP_NAMES = {-1: "down_sampled", 0: "empty", 1: "structure_start", 2: "structure_reference",
                  3: "biomes", 4: "noise", 5: "surface", 6: "carvers", 7: "terrain/liquid_carvers",
                  8: "features", 9: "light"}
SEPARATOR = "_DH-BSW_"


# ----------------------------------------------------------------------------- decompression

def _make_zstd():
    try:
        import zstandard
        d = zstandard.ZstdDecompressor()
        return "zstandard", lambda b: d.decompress(b, max_output_size=64 * 1024 * 1024)
    except ImportError:
        pass
    try:
        from compression import zstd as czstd  # Python 3.14+
        return "compression.zstd", czstd.decompress
    except ImportError:
        pass
    try:  # system libzstd (Linux/macOS); not normally present on Windows
        import ctypes
        import ctypes.util
        name = ctypes.util.find_library("zstd")
        if name:
            lib = ctypes.CDLL(name)
            lib.ZSTD_decompress.restype = ctypes.c_size_t
            lib.ZSTD_isError.restype = ctypes.c_uint
            lib.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong

            def dec(b):
                n = lib.ZSTD_getFrameContentSize(b, len(b))
                if n >= 2 ** 63:
                    n = 64 * 1024 * 1024
                out = ctypes.create_string_buffer(max(1, n))
                r = lib.ZSTD_decompress(out, n, b, len(b))
                if lib.ZSTD_isError(r):
                    raise ValueError("zstd decompression failed")
                return out.raw[:r]
            return "libzstd (ctypes)", dec
    except Exception:
        pass
    return None, None


ZSTD_NAME, _zstd = _make_zstd()


def decompress(blob, mode):
    if blob is None:
        return None
    blob = bytes(blob)
    if mode == 0:
        return blob
    if mode in (2, 4):
        if _zstd is None:
            sys.exit("No zstd decoder available:  py -m pip install zstandard")
        return _zstd(blob)
    if mode == 3:
        import lzma
        return lzma.decompress(blob)
    raise ValueError(f"CompressionMode {mode} (LZ4) is not supported by this script yet")


# ----------------------------------------------------------------------------- varints (numpy)

def read_varints(arr, pos, n):
    """Decode n unsigned LEB128 varints from uint8 array `arr` starting at `pos`.
    Returns (int64 values, new pos)."""
    if n == 0:
        return np.zeros(0, np.int64), pos
    tail = arr[pos:]
    ends = np.flatnonzero(tail < 128)
    if len(ends) < n:
        raise ValueError(f"blob ended early: wanted {n} varints, found {len(ends)}")
    ends = ends[:n]
    starts = np.empty(n, np.int64)
    starts[0] = 0
    starts[1:] = ends[:-1] + 1
    lens = ends - starts + 1
    if lens.max() > 5:
        raise ValueError("varint longer than 5 bytes - data is not in the expected format")
    vals = np.zeros(n, np.int64)
    for k in range(int(lens.max())):
        m = lens > k
        vals[m] |= (tail[starts[m] + k].astype(np.int64) & 127) << (7 * k)
    return vals, pos + int(ends[-1]) + 1


def part_columns(part):
    """Column indices (x*64+z) covered by a blob part, in DH's write order."""
    x0, x1, z0, z1 = {"data": (1, W - 1, 1, W - 1), "N": (0, W, 0, 1), "S": (0, W, W - 1, W),
                      "E": (W - 1, W, 0, W), "W": (0, 1, 0, W)}[part]
    xs, zs = np.meshgrid(np.arange(x0, x1), np.arange(z0, z1), indexing="ij")
    return (xs * W + zs).ravel()


def parse_part(buf, part):
    """Parse one decompressed V2 data blob. Returns dict of per-point arrays."""
    arr = np.frombuffer(buf, np.uint8)
    cols = part_columns(part)
    counts, p = read_varints(arr, 0, len(cols))
    npts = int(counts.sum())
    enc, p = read_varints(arr, p, npts)
    ids = enc >> 2
    lit = (enc & 2) != 0
    disc = (enc & 1) != 0
    heights, p = read_varints(arr, p, npts)
    nd = int(disc.sum())
    errs_raw, p = read_varints(arr, p, nd)
    errs = np.zeros(npts, np.int64)
    errs[disc] = (errs_raw >> 1) ^ -(errs_raw & 1)          # zigzag decode
    bottoms = np.cumsum(errs - heights)                     # previousBottomY - height + error
    nl = int(lit.sum())
    if p + nl > len(arr):
        raise ValueError("blob ended early in light bytes")
    light = arr[p:p + nl]
    p += nl
    sky = np.zeros(npts, np.uint8)
    blk = np.zeros(npts, np.uint8)
    sky[lit] = light & 15
    blk[lit] = light >> 4
    return {"col": np.repeat(cols, counts), "id": ids, "bottom": bottoms, "height": heights,
            "sky": sky, "block": blk, "consumed": p, "size": len(arr)}


def parse_mapping(buf):
    count = struct.unpack_from(">i", buf, 0)[0]
    p = 4
    biomes, blocks = [], []
    for _ in range(count):
        n = struct.unpack_from(">H", buf, p)[0]
        p += 2
        s = buf[p:p + n].decode("utf-8", "replace")
        p += n
        biome, _, block = s.partition(SEPARATOR)
        biomes.append(biome)
        blocks.append(block)
    return biomes, blocks, p


def assemble_section(row):
    """row: dict from sqlite. Returns combined per-point arrays for all 64x64 columns."""
    mode = row["CompressionMode"]
    parts = [("data", row["Data"]), ("N", row["NorthAdjData"]), ("S", row["SouthAdjData"]),
             ("E", row["EastAdjData"]), ("W", row["WestAdjData"])]
    parsed, report = [], []
    for name, blob in parts:
        if blob is None:
            report.append(f"{name}: NULL")
            continue
        raw = decompress(blob, mode)
        d = parse_part(raw, name)
        if d["consumed"] != d["size"]:
            report.append(f"{name}: WARNING consumed {d['consumed']} of {d['size']} bytes")
        else:
            report.append(f"{name}: {len(raw)} bytes -> {len(d['id'])} points (exact fit)")
        parsed.append((name, d))
    # later parts overwrite earlier ones for shared columns (same order DH reads them)
    owner = np.full(N_COLS, -1)
    for i, (name, d) in enumerate(parsed):
        owner[part_columns(name)] = i
    keys = ("col", "id", "bottom", "height", "sky", "block")
    out = {k: [] for k in keys}
    for i, (name, d) in enumerate(parsed):
        keep = owner[d["col"]] == i
        for k in keys:
            out[k].append(d[k][keep])
    out = {k: (np.concatenate(v) if v else np.zeros(0, np.int64)) for k, v in out.items()}
    return out, report


# ----------------------------------------------------------------------------- png output

def write_png(path, img):
    """img: HxW uint8 (gray) or HxWx3 uint8 (RGB)."""
    img = np.ascontiguousarray(img, np.uint8)
    h, w = img.shape[:2]
    ctype = 0 if img.ndim == 2 else 2
    raw = b"".join(b"\x00" + img[y].tobytes() for y in range(h))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        f.write(chunk(b"IEND", b""))


def id_colors(n):
    rng = np.random.default_rng(12345)
    c = rng.integers(40, 255, size=(max(n, 1), 3)).astype(np.uint8)
    return c


# ----------------------------------------------------------------------------- db helpers

def open_db(path):
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        sys.exit(f"File not found: {path}")
    uri = "file:" + urllib.request.pathname2url(path) + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        con.execute("SELECT 1 FROM FullData LIMIT 1").fetchall()
    except sqlite3.OperationalError as e:
        sys.exit(f"Could not open database: {e}\nClose Minecraft completely and try again.")
    return con


# ----------------------------------------------------------------------------- survey

def cmd_survey(con, args):
    print(f"zstd backend: {ZSTD_NAME}")
    levels = [r[0] for r in con.execute("SELECT DISTINCT DetailLevel FROM FullData ORDER BY 1")]
    print(f"detail levels present: {levels}\n")
    print("Scanning ColumnGenerationStep for every section (streamed, low memory)...")
    t0 = time.time()
    stats = {}
    cur = con.execute("SELECT DetailLevel, PosX, PosZ, CompressionMode, ColumnGenerationStep FROM FullData")
    n = 0
    while True:
        rows = cur.fetchmany(5000)
        if not rows:
            break
        for lvl, px, pz, mode, blob in rows:
            s = stats.setdefault(lvl, {"sections": 0, "real_sections": 0, "hist": np.zeros(11, np.int64),
                                       "bbox": [None, None, None, None]})
            s["sections"] += 1
            if blob is None:
                continue
            steps = np.frombuffer(decompress(blob, mode), np.int8)[:N_COLS].astype(np.int64)
            s["hist"] += np.bincount(np.clip(steps + 1, 0, 10), minlength=11)
            if (steps >= 5).any():  # at least SURFACE: real generated terrain
                s["real_sections"] += 1
                b = s["bbox"]
                b[0] = px if b[0] is None else min(b[0], px)
                b[1] = px if b[1] is None else max(b[1], px)
                b[2] = pz if b[2] is None else min(b[2], pz)
                b[3] = pz if b[3] is None else max(b[3], pz)
        n += len(rows)
        if n % 100000 < 5000:
            print(f"  {n:,} sections scanned ({time.time() - t0:.0f}s)")
    print(f"done: {n:,} sections in {time.time() - t0:.1f}s\n")

    print("lvl  col width  section width   sections   real*   down_sampled cols   real cols     real-data block area (X range, Z range)")
    for lvl in sorted(stats):
        s = stats[lvl]
        cw = 2 ** lvl
        sw = W * cw
        h = s["hist"]
        total = h.sum()
        down = h[0]
        real = h[6:].sum()
        b = s["bbox"]
        if b[0] is not None:
            area = f"X {b[0] * sw:>8}..{(b[1] + 1) * sw:<8} Z {b[2] * sw:>8}..{(b[3] + 1) * sw:<8}"
        else:
            area = "(none)"
        print(f"{lvl:>3}  {cw:>6} blk  {sw:>8} blk  {s['sections']:>9,}  {s['real_sections']:>7,}  "
              f"{100 * down / max(total, 1):>12.1f} %  {100 * real / max(total, 1):>9.1f} %   {area}")
    print("\n* real = sections with at least one column generated to SURFACE or later.")
    print("  down_sampled columns were filled from coarser levels, not real terrain at that detail.")
    print("\ngeneration-step histogram per level (columns):")
    for lvl in sorted(stats):
        h = stats[lvl]["hist"]
        parts = [f"{GEN_STEP_NAMES.get(i - 1, i - 1)}={h[i]:,}" for i in range(11) if h[i]]
        print(f"  L{lvl}: " + ", ".join(parts))


# ----------------------------------------------------------------------------- section

def cmd_section(con, args):
    print(f"zstd backend: {ZSTD_NAME}")
    lvl = args.level
    cw = 2 ** lvl
    sw = W * cw
    if args.pos:
        tx, tz = args.pos
    else:
        bx, bz = args.block
        tx, tz = bx // sw, bz // sw
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM FullData WHERE DetailLevel=? AND PosX=? AND PosZ=?",
                      (lvl, tx, tz)).fetchone()
    if row is None:
        near = con.execute(
            "SELECT PosX, PosZ FROM FullData WHERE DetailLevel=? "
            "ORDER BY (PosX-?)*(PosX-?) + (PosZ-?)*(PosZ-?) LIMIT 1",
            (lvl, tx, tx, tz, tz)).fetchone()
        if near is None:
            sys.exit(f"No sections at detail level {lvl}.")
        print(f"No section at level {lvl} pos ({tx},{tz}); using nearest ({near[0]},{near[1]}).")
        tx, tz = near
        row = con.execute("SELECT * FROM FullData WHERE DetailLevel=? AND PosX=? AND PosZ=?",
                          (lvl, tx, tz)).fetchone()
    row = dict(row)
    min_bx, min_bz = tx * sw, tz * sw
    print(f"\nSECTION  level={lvl}  pos=({tx},{tz})  column width={cw} blocks  section width={sw} blocks")
    print(f"block area: X {min_bx}..{min_bx + sw}   Z {min_bz}..{min_bz + sw}")
    print(f"DataFormatVersion={row['DataFormatVersion']}  CompressionMode={row['CompressionMode']}  "
          f"Regenerate={row.get('Regenerate')}")
    if row["DataFormatVersion"] != 2:
        sys.exit("This decoder only handles DataFormatVersion 2.")

    mode = row["CompressionMode"]
    steps = np.frombuffer(decompress(row["ColumnGenerationStep"], mode), np.int8)[:N_COLS]
    vals, cnt = np.unique(steps, return_counts=True)
    print("generation steps: " + ", ".join(f"{GEN_STEP_NAMES.get(int(v), int(v))}={c}" for v, c in zip(vals, cnt)))

    biomes, blocks, used = parse_mapping(decompress(row["Mapping"], mode))
    print(f"\nmapping: {len(blocks)} entries")
    for i, (bi, bl) in enumerate(zip(biomes, blocks)):
        if i < args.show_mapping:
            print(f"  [{i:3}] {bl:50} biome={bi}")
    if len(blocks) > args.show_mapping:
        print(f"  ... ({len(blocks) - args.show_mapping} more)")

    pts, report = assemble_section(row)
    print("\nblob parts:")
    for r in report:
        print("  " + r)
    if len(pts["id"]) and pts["id"].max() >= len(blocks):
        print(f"  WARNING: point id {pts['id'].max()} >= mapping size {len(blocks)}")

    npts = len(pts["id"])
    per_col = np.bincount(pts["col"], minlength=N_COLS)
    print(f"\npoints: {npts:,}   columns with data: {(per_col > 0).sum()}/{N_COLS}   "
          f"points/column: max {per_col.max()}, mean {per_col[per_col > 0].mean() if (per_col > 0).any() else 0:.1f}")
    if npts == 0:
        return
    tops = pts["bottom"] + pts["height"]
    print(f"relative Y range of all points: {int(pts['bottom'].min())} .. {int(tops.max())}"
          f"   (world Y = relative + world_min_y {args.world_min_y})")
    if (pts["bottom"] < 0).any() or (pts["height"] <= 0).any():
        print("  WARNING: negative bottomY or non-positive height found - decode problem")

    # surface = first non-air point from the top of each column
    is_air = np.array([b in AIR_NAMES for b in blocks] + [False])[np.minimum(pts["id"], len(blocks))]
    idx = np.arange(npts)
    first = np.full(N_COLS, npts)
    na = ~is_air
    np.minimum.at(first, pts["col"][na], idx[na])
    has = first < npts
    surf_y = np.full(N_COLS, np.nan)
    surf_id = np.full(N_COLS, -1)
    surf_y[has] = tops[first[has]] + args.world_min_y
    surf_id[has] = pts["id"][first[has]]
    print(f"surface world Y: min {np.nanmin(surf_y):.0f}  max {np.nanmax(surf_y):.0f}  "
          f"mean {np.nanmean(surf_y):.1f}   (columns with a non-air block: {has.sum()})")
    ids, c = np.unique(surf_id[has], return_counts=True)
    order = np.argsort(-c)[:12]
    print("most common surface blocks:")
    for k in order:
        print(f"  {c[k]:5}  {blocks[ids[k]]}")

    # sample column dump
    xz = (W // 2) * W + (W // 2)
    sel = pts["col"] == xz
    print(f"\ncolumn ({W // 2},{W // 2}) = block X {min_bx + (W // 2) * cw}, Z {min_bz + (W // 2) * cw}, top to bottom:")
    for i in np.flatnonzero(sel)[:40]:
        b0 = int(pts["bottom"][i]) + args.world_min_y
        print(f"  Y {b0:6} .. {b0 + int(pts['height'][i]):6}  sky {int(pts['sky'][i]):2} blk {int(pts['block'][i]):2}  "
              f"{blocks[pts['id'][i]] if pts['id'][i] < len(blocks) else '?'}")

    # outputs
    os.makedirs(args.out, exist_ok=True)
    tag = f"L{lvl}_{tx}_{tz}"
    hm = surf_y.reshape(W, W)                      # [x, z]
    valid = ~np.isnan(hm)
    lo, hi = (np.nanmin(hm), np.nanmax(hm)) if valid.any() else (0, 1)
    gray = np.zeros((W, W), np.uint8)
    gray[valid] = (40 + 215 * (hm[valid] - lo) / max(hi - lo, 1)).astype(np.uint8)
    img = gray.T                                   # rows = Z (north at top), cols = X
    scale = 8
    write_png(os.path.join(args.out, f"{tag}_height.png"), np.kron(img, np.ones((scale, scale), np.uint8)))
    cols = id_colors(len(blocks))
    rgb = np.zeros((W, W, 3), np.uint8)
    sid = surf_id.reshape(W, W)
    rgb[sid >= 0] = cols[sid[sid >= 0]]
    rgb = rgb.transpose(1, 0, 2)
    write_png(os.path.join(args.out, f"{tag}_blocks.png"), rgb.repeat(scale, 0).repeat(scale, 1))
    np.savez_compressed(os.path.join(args.out, f"{tag}.npz"),
                        level=lvl, pos=np.array([tx, tz]), min_block=np.array([min_bx, min_bz]),
                        col=pts["col"], id=pts["id"], bottom=pts["bottom"], height=pts["height"],
                        sky=pts["sky"], block_light=pts["block"],
                        blocks=np.array(blocks), biomes=np.array(biomes), gen_steps=steps)
    print(f"\nwrote {tag}_height.png (grayscale surface height, north up),")
    print(f"      {tag}_blocks.png (one random color per surface block type),")
    print(f"      {tag}.npz  ->  {os.path.abspath(args.out)}")


# ----------------------------------------------------------------------------- coverage / probe

LEVEL_COLORS = {0: (255, 255, 255), 1: (90, 220, 90), 2: (60, 160, 255), 3: (255, 200, 40),
                4: (230, 90, 60), 5: (110, 70, 140)}


def section_real_fraction(blob, mode):
    if blob is None:
        return 0.0
    steps = np.frombuffer(decompress(blob, mode), np.int8)[:N_COLS]
    return float((steps >= 5).mean())


def cmd_coverage(con, args):
    """PNG map: each pixel = 64x64 blocks, colored by the finest detail level with real data."""
    print(f"zstd backend: {ZSTD_NAME}")
    real = {}  # level -> {(px,pz): fraction}
    t0 = time.time()
    for lvl in range(0, 6):
        cur = con.execute("SELECT PosX, PosZ, CompressionMode, ColumnGenerationStep FROM FullData "
                          "WHERE DetailLevel=?", (lvl,))
        d = {}
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            for px, pz, mode, blob in rows:
                f = section_real_fraction(blob, mode)
                if f > 0:
                    d[(px, pz)] = f
        real[lvl] = d
        print(f"  level {lvl}: {len(d):,} sections with real data ({time.time() - t0:.0f}s)")

    # map extent = real data of levels 0..4 (in blocks), in 64-block pixels
    xs, zs = [], []
    for lvl in range(0, 5):
        sw = W * 2 ** lvl
        for px, pz in real[lvl]:
            xs += [px * sw, (px + 1) * sw]
            zs += [pz * sw, (pz + 1) * sw]
    if not xs:
        sys.exit("No real data at levels 0-4.")
    x0, x1, z0, z1 = min(xs), max(xs), min(zs), max(zs)
    nx, nz = (x1 - x0) // 64, (z1 - z0) // 64
    img = np.zeros((nz, nx, 3), np.uint8)
    img[:] = (25, 25, 25)
    finest = np.full((nz, nx), 99, np.int16)
    for lvl in range(5, -1, -1):
        sw = W * 2 ** lvl
        k = sw // 64
        for (px, pz), f in real[lvl].items():
            if f < 0.5:
                continue
            ix, iz = (px * sw - x0) // 64, (pz * sw - z0) // 64
            a, b = max(ix, 0), min(ix + k, nx)
            c, e = max(iz, 0), min(iz + k, nz)
            if a < b and c < e:
                finest[c:e, a:b] = np.minimum(finest[c:e, a:b], lvl)
    for lvl, col in LEVEL_COLORS.items():
        img[finest == lvl] = col
    # grid every 1024 blocks
    for gx in range((x0 // 1024 + 1) * 1024, x1, 1024):
        img[:, (gx - x0) // 64] //= 2
    for gz in range((z0 // 1024 + 1) * 1024, z1, 1024):
        img[(gz - z0) // 64, :] //= 2
    # axes through 0,0 and probe marker
    if x0 <= 0 < x1:
        img[:, (0 - x0) // 64] = (255, 0, 255)
    if z0 <= 0 < z1:
        img[(0 - z0) // 64, :] = (255, 0, 255)
    if args.mark:
        mx, mz = (args.mark[0] - x0) // 64, (args.mark[1] - z0) // 64
        if 0 <= mx < nx and 0 <= mz < nz:
            img[max(mz - 4, 0):mz + 5, mx] = (255, 0, 0)
            img[mz, max(mx - 4, 0):mx + 5] = (255, 0, 0)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "coverage.png")
    scale = max(1, min(4, 1600 // max(nx, nz)))
    write_png(path, img.repeat(scale, 0).repeat(scale, 1))
    print(f"\nmap covers X {x0}..{x1}, Z {z0}..{z1}; 1 pixel = {64 // scale if scale <= 64 else 1} blocks"
          f" (scaled x{scale}), north up, east right")
    print("colors = finest real level: white L0 (1 blk), green L1 (2), blue L2 (4), yellow L3 (8),")
    print("         red L4 (16), purple L5 (32), dark = none. Grid lines every 1024 blocks;")
    print("         magenta lines = X 0 / Z 0; red cross = --mark position.")
    print(f"wrote {path}")
    # list the biggest real-L0 clusters' bounding box per 1024-block cell
    cells = {}
    for (px, pz), f in real[0].items():
        if f >= 0.5:
            key = ((px * 64) // 1024, (pz * 64) // 1024)
            cells[key] = cells.get(key, 0) + 1
    top = sorted(cells.items(), key=lambda kv: -kv[1])[:15]
    print("\n1024x1024-block cells with the most real L0 sections (max 256 per cell):")
    for (cx, cz), n in top:
        print(f"  X {cx * 1024:>7}..{cx * 1024 + 1024:<7} Z {cz * 1024:>7}..{cz * 1024 + 1024:<7}  {n:4} sections"
              f"  ({100 * n / 256:.0f}% full detail)")


def cmd_probe(con, args):
    """For one block X,Z: what each detail level has there."""
    print(f"zstd backend: {ZSTD_NAME}")
    bx, bz = args.block
    con.row_factory = sqlite3.Row
    print(f"\nblock X {bx}, Z {bz}   (world_min_y {args.world_min_y})")
    print("lvl  col width  section     gen step here     surface world Y   surface block")
    for lvl in range(0, 10):
        cw = 2 ** lvl
        sw = W * cw
        px, pz = bx // sw, bz // sw
        row = con.execute("SELECT * FROM FullData WHERE DetailLevel=? AND PosX=? AND PosZ=?",
                          (lvl, px, pz)).fetchone()
        if row is None:
            print(f"{lvl:>3}  {cw:>6} blk  ({px},{pz})  -- no section --")
            continue
        row = dict(row)
        mode = row["CompressionMode"]
        rx, rz = (bx - px * sw) // cw, (bz - pz * sw) // cw
        ci = rx * W + rz
        step = int(np.frombuffer(decompress(row["ColumnGenerationStep"], mode), np.int8)[ci]) \
            if row["ColumnGenerationStep"] is not None else None
        _, blocks, _ = parse_mapping(decompress(row["Mapping"], mode))
        pts, _ = assemble_section(row)
        sel = np.flatnonzero(pts["col"] == ci)
        surf, name = "-", "-"
        for i in sel:
            nm = blocks[pts["id"][i]] if pts["id"][i] < len(blocks) else "?"
            if nm not in AIR_NAMES:
                surf = str(int(pts["bottom"][i] + pts["height"][i]) + args.world_min_y)
                name = nm
                break
        print(f"{lvl:>3}  {cw:>6} blk  ({px},{pz})  {GEN_STEP_NAMES.get(step, step):16}  {surf:>15}   {name}")


def cmd_nearby(con, args):
    """Sections of one level around a block: real / down-sampled / missing, Regenerate flags, timestamps."""
    print(f"zstd backend: {ZSTD_NAME}")
    lvl, (bx, bz), r = args.level, args.block, args.radius
    sw = W * 2 ** lvl
    cx, cz = bx // sw, bz // sw
    n = max(1, r // sw)
    rows = con.execute(
        "SELECT PosX, PosZ, CompressionMode, ColumnGenerationStep, Regenerate, ApplyToParent, "
        "LastModifiedUnixDateTime, CreatedUnixDateTime FROM FullData "
        "WHERE DetailLevel=? AND PosX BETWEEN ? AND ? AND PosZ BETWEEN ? AND ?",
        (lvl, cx - n, cx + n, cz - n, cz + n)).fetchall()
    got = {}
    for px, pz, mode, blob, regen, atp, mod, cre in rows:
        got[(px, pz)] = (section_real_fraction(blob, mode), regen, atp, mod, cre)
    print(f"\nlevel {lvl} sections within {n * sw} blocks of X {bx}, Z {bz}  (section width {sw})")
    print("map, north up:  # = real   . = down-sampled   r = down-sampled + Regenerate flag   "
          "(blank) = no row   @ = your block")
    for pz in range(cz - n, cz + n + 1):
        line = ""
        for px in range(cx - n, cx + n + 1):
            if (px, pz) == (cx, cz):
                line += "@"
                continue
            v = got.get((px, pz))
            line += " " if v is None else "#" if v[0] >= 0.5 else "r" if v[1] else "."
        print("  " + line)
    vals = list(got.values())
    print(f"\nrows {len(vals)} of {(2 * n + 1) ** 2}:  real {sum(v[0] >= .5 for v in vals)}   "
          f"down-sampled {sum(v[0] < .5 for v in vals)}   with Regenerate=1 {sum(bool(v[1]) for v in vals)}   "
          f"ApplyToParent=1 {sum(bool(v[2]) for v in vals)}")
    me = got.get((cx, cz))
    if me:
        print(f"your section ({cx},{cz}): real fraction {me[0]:.2f}  Regenerate={me[1]}  ApplyToParent={me[2]}  "
              f"created {time.strftime('%Y-%m-%d %H:%M', time.localtime(me[4] / 1000)) if me[4] else '?'}  "
              f"modified {time.strftime('%Y-%m-%d %H:%M', time.localtime(me[3] / 1000)) if me[3] else '?'}")
    near = con.execute(
        "SELECT PosX, PosZ, CompressionMode, ColumnGenerationStep FROM FullData WHERE DetailLevel=? "
        "ORDER BY (PosX-?)*(PosX-?) + (PosZ-?)*(PosZ-?) LIMIT 4000", (lvl, cx, cx, cz, cz))
    for px, pz, mode, blob in near:
        if section_real_fraction(blob, mode) >= 0.5:
            d = ((px - cx) ** 2 + (pz - cz) ** 2) ** 0.5 * sw
            print(f"nearest real level {lvl} section: ({px},{pz}) = block X {px * sw}, Z {pz * sw}, ~{d:.0f} blocks away")
            break
    else:
        print(f"no real level {lvl} section among the 4000 nearest rows")


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Decode Distant Horizons FullData (read-only).")
    ap.add_argument("db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("survey")
    sp = sub.add_parser("section")
    sp.add_argument("--level", type=int, default=0)
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--block", type=int, nargs=2, default=[0, 0], metavar=("X", "Z"))
    g.add_argument("--pos", type=int, nargs=2, metavar=("POSX", "POSZ"))
    sp.add_argument("--world-min-y", type=int, default=-64)
    sp.add_argument("--show-mapping", type=int, default=25)
    sp.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "dh_decode_out"))
    cp = sub.add_parser("coverage")
    cp.add_argument("--mark", type=int, nargs=2, metavar=("X", "Z"))
    cp.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "dh_decode_out"))
    pp = sub.add_parser("probe")
    pp.add_argument("--block", type=int, nargs=2, required=True, metavar=("X", "Z"))
    pp.add_argument("--world-min-y", type=int, default=-64)
    np_ = sub.add_parser("nearby")
    np_.add_argument("--block", type=int, nargs=2, required=True, metavar=("X", "Z"))
    np_.add_argument("--level", type=int, default=0)
    np_.add_argument("--radius", type=int, default=1024)
    args = ap.parse_args()
    con = open_db(args.db)
    try:
        {"survey": cmd_survey, "section": cmd_section, "coverage": cmd_coverage,
         "probe": cmd_probe, "nearby": cmd_nearby}[args.cmd](con, args)
    finally:
        con.close()


if __name__ == "__main__":
    main()
