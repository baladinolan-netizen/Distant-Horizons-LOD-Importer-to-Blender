"""
Distant Horizons Importer for Blender
Imports Distant Horizons LOD data (DistantHorizons.sqlite) as blocky meshes with DH-style
vertex colors: distance rings at different detail levels, tiled, greedy-merged, imported in the
background with a progress bar (Esc cancels). Read-only on the database.

Install: Edit > Preferences > Add-ons > (dropdown / arrow) Install from Disk... > pick this .py
Then in the add-on's preferences press "Install zstandard" once, restart Blender.
Use: 3D Viewport > N sidebar > "DH Import" tab.
"""
bl_info = {
    "name": "Distant Horizons Importer",
    "author": "YOUR NAME",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > DH Import",
    "description": "Import Distant Horizons LOD data (sqlite) as meshes",
    "category": "Import-Export",
}

import os
import sqlite3
import struct
import sys
import json
import time
import urllib.request
import zipfile
import zlib

import numpy as np

# =============================================================================== decoding
# Format from DH core (FullDataSourceV2DTO, FullDataPointIdMap, VarintUtil), DataFormatVersion 2.

W = 64
N_COLS = W * W
SEPARATOR = "_DH-BSW_"


def get_zstd():
    try:
        import zstandard
        d = zstandard.ZstdDecompressor()
        return lambda b: d.decompress(b, max_output_size=64 * 1024 * 1024)
    except ImportError:
        pass
    try:
        from compression import zstd as czstd  # Python 3.14+
        return czstd.decompress
    except ImportError:
        pass
    return None


_ZSTD = None


def decompress(blob, mode):
    global _ZSTD
    if blob is None:
        return None
    blob = bytes(blob)
    if mode == 0:
        return blob
    if mode in (2, 4):
        if _ZSTD is None:
            _ZSTD = get_zstd()
            if _ZSTD is None:
                raise RuntimeError("zstandard is not installed. Open the add-on preferences and press "
                                   "'Install zstandard', then restart Blender.")
        return _ZSTD(blob)
    if mode == 3:
        import lzma
        return lzma.decompress(blob)
    raise RuntimeError(f"CompressionMode {mode} (LZ4) not supported")


def read_varints(arr, pos, n):
    if n == 0:
        return np.zeros(0, np.int64), pos
    tail = arr[pos:]
    ends = np.flatnonzero(tail < 128)
    if len(ends) < n:
        raise ValueError("blob ended early")
    ends = ends[:n]
    starts = np.empty(n, np.int64)
    starts[0] = 0
    starts[1:] = ends[:-1] + 1
    lens = ends - starts + 1
    vals = np.zeros(n, np.int64)
    for k in range(int(lens.max())):
        m = lens > k
        vals[m] |= (tail[starts[m] + k].astype(np.int64) & 127) << (7 * k)
    return vals, pos + int(ends[-1]) + 1


def part_columns(part):
    x0, x1, z0, z1 = {"data": (1, W - 1, 1, W - 1), "N": (0, W, 0, 1), "S": (0, W, W - 1, W),
                      "E": (W - 1, W, 0, W), "W": (0, 1, 0, W)}[part]
    xs, zs = np.meshgrid(np.arange(x0, x1), np.arange(z0, z1), indexing="ij")
    return (xs * W + zs).ravel()


def parse_part(buf, part):
    arr = np.frombuffer(buf, np.uint8)
    cols = part_columns(part)
    counts, p = read_varints(arr, 0, len(cols))
    npts = int(counts.sum())
    enc, p = read_varints(arr, p, npts)
    lit = (enc & 2) != 0
    disc = (enc & 1) != 0
    heights, p = read_varints(arr, p, npts)
    errs_raw, p = read_varints(arr, p, int(disc.sum()))
    errs = np.zeros(npts, np.int64)
    errs[disc] = (errs_raw >> 1) ^ -(errs_raw & 1)
    bottoms = np.cumsum(errs - heights)
    nl = int(lit.sum())
    light = arr[p:p + nl]
    sky = np.zeros(npts, np.uint8)
    sky[lit] = light & 15
    return {"col": np.repeat(cols, counts), "id": enc >> 2, "bottom": bottoms, "height": heights, "sky": sky}


def parse_mapping(buf):
    count = struct.unpack_from(">i", buf, 0)[0]
    p = 4
    names, biomes = [], []
    for _ in range(count):
        n = struct.unpack_from(">H", buf, p)[0]
        p += 2
        s = buf[p:p + n].decode("utf-8", "replace")
        p += n
        biome, _, block = s.partition(SEPARATOR)
        names.append(block)
        biomes.append(biome)
    return names, biomes


def decode_section(row):
    """row: (Data, N, S, E, W, Mapping, CompressionMode). Returns (points dict, block names).
    Points are in DH order: per column, top to bottom."""
    data, n, s, e, w, mapping, mode = row
    parsed = []
    for name, blob in (("data", data), ("N", n), ("S", s), ("E", e), ("W", w)):
        if blob is not None:
            parsed.append((name, parse_part(decompress(blob, mode), name)))
    owner = np.full(N_COLS, -1)
    for i, (name, _) in enumerate(parsed):
        owner[part_columns(name)] = i
    keys = ("col", "id", "bottom", "height", "sky")
    out = {k: [] for k in keys}
    for i, (name, d) in enumerate(parsed):
        keep = owner[d["col"]] == i
        for k in keys:
            out[k].append(d[k][keep])
    out = {k: (np.concatenate(v) if v else np.zeros(0, np.int64)) for k, v in out.items()}
    # stable sort by column so each column's points are contiguous, still top -> bottom
    order = np.argsort(out["col"], kind="stable")
    out = {k: v[order] for k, v in out.items()}
    names, biomes = parse_mapping(decompress(mapping, mode)) if mapping is not None else ([], [])
    return out, (names, biomes)


# =============================================================================== texture colors (DH-style averages)


# ------------------------------------------------------------------ PNG decoding (8-bit, non-interlaced)

def decode_png(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    p = 8
    idat = []
    plte = trns = None
    w = h = ctype = depth = interlace = 0
    while p < len(data):
        ln, typ = struct.unpack(">I4s", data[p:p + 8])
        chunk = data[p + 8:p + 8 + ln]
        p += 12 + ln
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif typ == b"PLTE":
            plte = np.frombuffer(chunk, np.uint8).reshape(-1, 3)
        elif typ == b"tRNS":
            trns = np.frombuffer(chunk, np.uint8)
        elif typ == b"IDAT":
            idat.append(chunk)
        elif typ == b"IEND":
            break
    if interlace:
        raise ValueError("interlaced PNG not supported")
    chans = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(b"".join(idat))
    if depth == 8:
        bpp = chans
        stride = w * chans
    elif depth == 16:
        bpp = chans * 2
        stride = w * chans * 2
    elif depth < 8 and ctype in (0, 3):
        bpp = 1
        stride = (w * depth + 7) // 8
    else:
        raise ValueError(f"unsupported PNG depth {depth}")
    out = bytearray(stride * h)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        f = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    a = np.frombuffer(bytes(out), np.uint8)
    if depth == 16:
        a = a.reshape(h, w * chans, 2)[:, :, 0]
    elif depth < 8:
        bits = np.unpackbits(a.reshape(h, stride), axis=1)[:, :w * depth]
        a = bits.reshape(h, w, depth)
        a = (a * (1 << np.arange(depth - 1, -1, -1))).sum(-1).astype(np.uint8)
        if ctype == 0:
            a = (a * (255 // ((1 << depth) - 1))).astype(np.uint8)
    a = a.reshape(h, w, chans) if a.ndim != 3 or a.shape[-1] != chans else a
    if ctype == 3:
        rgb = plte[a[..., 0]]
        alpha = np.full((h, w, 1), 255, np.uint8)
        if trns is not None:
            t = np.full(256, 255, np.uint8)
            t[:len(trns)] = trns
            alpha = t[a[..., 0]][..., None]
        rgba = np.concatenate([rgb, alpha], -1)
    elif ctype == 0:
        rgba = np.concatenate([a, a, a, np.full_like(a, 255)], -1)
    elif ctype == 4:
        rgba = np.concatenate([a[..., :1]] * 3 + [a[..., 1:]], -1)
    elif ctype == 2:
        rgba = np.concatenate([a, np.full((h, w, 1), 255, np.uint8)], -1)
    else:
        rgba = a
    return rgba.astype(np.float32) / 255.0


# ------------------------------------------------------------------ asset access

class Assets:
    """Looks files up in resource packs first (in the given order), then the vanilla jar."""

    def __init__(self, jar_path, pack_paths=()):
        self.sources = []
        for p in list(pack_paths) + [jar_path]:
            if not p:
                continue
            if os.path.isdir(p):
                self.sources.append(("dir", p))
            elif os.path.isfile(p):
                self.sources.append(("zip", zipfile.ZipFile(p)))
        self._names = [set(s[1].namelist()) if s[0] == "zip" else None for s in self.sources]

    def read(self, path):
        for (kind, src), names in zip(self.sources, self._names):
            if kind == "zip":
                if path in names:
                    return src.read(path)
            else:
                fp = os.path.join(src, *path.split("/"))
                if os.path.isfile(fp):
                    with open(fp, "rb") as f:
                        return f.read()
        return None

    def json(self, path):
        d = self.read(path)
        return json.loads(d) if d is not None else None


def rl(name, folder):
    """'minecraft:block/stone' or 'block/stone' -> assets path."""
    ns, _, p = name.partition(":") if ":" in name else ("minecraft", None, name)
    return f"assets/{ns}/{folder}/{p}"


# ------------------------------------------------------------------ colors

FIXED_TINTS = {"spruce_leaves": 0x619961, "birch_leaves": 0x80A755, "lily_pad": 0x208030,
               "attached_melon_stem": 0xE0C71C, "attached_pumpkin_stem": 0xE0C71C}
FOLIAGE = {"oak_leaves", "jungle_leaves", "acacia_leaves", "dark_oak_leaves", "mangrove_leaves", "vine"}
# only blocks that Minecraft actually colors by biome (a model's tintindex alone is not enough:
# e.g. cherry and azalea leaves have tintindex but no color provider)
GRASS_TINTED = {"grass_block", "grass", "short_grass", "tall_grass", "fern", "large_fern", "potted_fern",
                "sugar_cane", "pink_petals"}
TINTABLE = GRASS_TINTED | FOLIAGE | set(FIXED_TINTS) | {"melon_stem", "pumpkin_stem"}
DIRS = ("up", "down", "north", "south", "east", "west")


def parse_state(full):
    """'minecraft:snow_STATE_{layers:1}{x:y}' -> ('minecraft:snow', {'layers': '1', ...})"""
    name, _, st = full.partition("_STATE_")
    props = {}
    for part in st.strip("{}").split("}{"):
        if ":" in part:
            k, v = part.split(":", 1)
            props[k] = v
    return name, props


class BlockColors:
    def __init__(self, jar_path, pack_paths=()):
        self.a = Assets(jar_path, pack_paths)
        self._tex = {}
        self._model = {}
        self._biome = {}
        self._cache = {}
        self._grass_map = self._img("minecraft:colormap/grass")
        self._foliage_map = self._img("minecraft:colormap/foliage")

    # --- low level
    def _img(self, tex):
        if tex in self._tex:
            return self._tex[tex]
        data = self.a.read(rl(tex, "textures") + ".png")
        img = None
        if data is not None:
            try:
                img = decode_png(data)
                if img.shape[0] > img.shape[1]:   # animated strip: first frame
                    img = img[:img.shape[1]]
            except Exception:
                img = None
        self._tex[tex] = img
        return img

    def _resolve_model(self, name):
        """Returns (textures dict, elements list) following the parent chain."""
        if name in self._model:
            return self._model[name]
        textures, elements, seen = {}, None, 0
        cur = name
        while cur and seen < 20:
            seen += 1
            m = self.a.json(rl(cur, "models") + ".json")
            if m is None:
                break
            for k, v in m.get("textures", {}).items():
                textures.setdefault(k, v)
            if elements is None and "elements" in m:
                elements = m["elements"]
            cur = m.get("parent")
            if cur and "builtin/" in cur:
                break
        res = (textures, elements or [])
        self._model[name] = res
        return res

    @staticmethod
    def _deref(textures, ref):
        for _ in range(10):
            if ref is None or not ref.startswith("#"):
                return ref
            ref = textures.get(ref[1:])
        return None

    def _pick_model(self, name, props):
        bs = self.a.json(rl(name, "blockstates") + ".json")
        if bs is None:
            return None, 0
        cands = []
        if "variants" in bs:
            for key, v in bs["variants"].items():
                ok = all(props.get(k) == val for k, val in
                         (kv.split("=", 1) for kv in key.split(",") if "=" in kv))
                if ok or key == "":
                    cands.append(v)
            if not cands:
                cands = list(bs["variants"].values())
        elif "multipart" in bs:
            cands = [p["apply"] for p in bs["multipart"] if "when" not in p] or \
                    [p["apply"] for p in bs["multipart"]]
        if not cands:
            return None, 0
        v = cands[0]
        if isinstance(v, list):
            v = v[0]
        return v.get("model"), v.get("x", 0)

    # --- biome
    def _biome_info(self, biome):
        if biome in self._biome:
            return self._biome[biome]
        ns, _, b = biome.partition(":")
        j = self.a.json(f"data/{ns or 'minecraft'}/worldgen/biome/{b or ns}.json") or {}
        eff = j.get("effects", {})
        t = min(max(j.get("temperature", 0.8), 0.0), 1.0)
        d = min(max(j.get("downfall", 0.4), 0.0), 1.0) * t
        x, y = int((1 - t) * 255), int((1 - d) * 255)

        def cmap(img, fallback):
            return img[y, x, :3] if img is not None else np.array(fallback, np.float32)

        def hexc(v):
            return np.array([(v >> 16) & 255, (v >> 8) & 255, v & 255], np.float32) / 255

        grass = hexc(eff["grass_color"]) if "grass_color" in eff else cmap(self._grass_map, (0.47, 0.73, 0.33))
        foliage = hexc(eff["foliage_color"]) if "foliage_color" in eff else cmap(self._foliage_map, (0.37, 0.62, 0.21))
        mod = eff.get("grass_color_modifier")
        if mod == "swamp":
            grass = hexc(0x6A7039)
        elif mod == "dark_forest":
            g = (grass * 255).astype(int)
            g = (((g[0] << 16) | (g[1] << 8) | g[2]) & 0xFEFEFE) + 0x28340A >> 1
            grass = hexc(g)
        water = hexc(eff.get("water_color", 0x3F76E4))
        res = {"grass": grass, "foliage": foliage, "water": water}
        self._biome[biome] = res
        return res

    def _tint(self, base, biome):
        if base in FIXED_TINTS:
            v = FIXED_TINTS[base]
            return np.array([(v >> 16) & 255, (v >> 8) & 255, v & 255], np.float32) / 255
        info = self._biome_info(biome)
        if base in FOLIAGE:
            return info["foliage"]
        if base.endswith("_stem"):
            return np.array([0.6, 0.75, 0.2], np.float32)
        return info["grass"]

    # --- public
    def colors(self, full_name, biome):
        """sRGB 0..255 (top, side, bottom) for a DH block-state string, or None if unknown."""
        key = (full_name, biome)
        if key in self._cache:
            return self._cache[key]
        res = self._compute(full_name, biome)
        self._cache[key] = res
        return res

    def _compute(self, full_name, biome):
        name, props = parse_state(full_name)
        base = name.split(":")[-1]
        if base in ("water", "lava", "bubble_column"):
            img = self._img(f"minecraft:block/{'lava' if base == 'lava' else 'water'}_still")
            if img is None:
                return None
            c = self._avg([(img, base != "lava")], self._biome_info(biome)["water"] if base != "lava" else None)
            return (c, c, c) if c is not None else None
        model, xrot = self._pick_model(name, props)
        if model is None:
            return None
        textures, elements = self._resolve_model(model)
        tint = None
        out = {}
        for d in ("up", "north", "down"):
            layers = []
            for el in elements:
                f = el.get("faces", {}).get(d)
                if not f:
                    continue
                tex = self._deref(textures, f.get("texture")) or ""
                img = self._img(tex)
                if img is not None:
                    tinted = ("tintindex" in f and base in TINTABLE) or tex.endswith("grass_block_top")
                    if tinted and tint is None:
                        tint = self._tint(base, biome)
                    layers.append((img, tinted))
            if not layers:  # plants / cross models: use whatever texture there is
                ref = textures.get("particle") or next(iter(textures.values()), None)
                img = self._img(self._deref(textures, ref) or "") if ref else None
                if img is not None:
                    layers = [(img, base in TINTABLE)]
                    if layers[0][1] and tint is None:
                        tint = self._tint(base, biome)
            out[d] = self._avg(layers, tint)
        if xrot in (90, 270):  # e.g. sideways logs: top shows bark, sides show rings
            out["up"], out["north"] = out["north"], out["up"]
        res = tuple(out.get(d) if out.get(d) is not None else
                    next((v for v in out.values() if v is not None), None) for d in ("up", "north", "down"))
        if res[0] is None:
            return None
        return res

    @staticmethod
    def _avg(layers, tint):
        if not layers:
            return None
        acc_rgb = acc_a = None
        for img, tinted in layers:   # alpha-composite layers (e.g. grass side = dirt + tinted overlay)
            rgb = img[..., :3] * (tint if (tinted and tint is not None) else 1.0)
            alpha = img[..., 3:4]
            if acc_rgb is None:
                acc_rgb, acc_a = rgb * alpha, alpha.copy()
            elif img.shape[:2] == acc_rgb.shape[:2]:
                acc_rgb = rgb * alpha + acc_rgb * (1 - alpha)
                acc_a = alpha + acc_a * (1 - alpha)
        tot = acc_a.sum()
        if tot <= 0:
            return None
        return (acc_rgb.sum((0, 1)) / tot * 255).astype(np.float32)


# =============================================================================== block classes

def base_name(full):
    n = full.split("_STATE_")[0]
    return n[10:] if n.startswith("minecraft:") else n


EMPTY_EXACT = {
    "AIR", "air", "cave_air", "void_air", "grass", "short_grass", "tall_grass", "fern", "large_fern",
    "dead_bush", "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet", "oxeye_daisy", "cornflower",
    "lily_of_the_valley", "sunflower", "lilac", "rose_bush", "peony", "pink_petals", "sweet_berry_bush",
    "cave_vines", "cave_vines_plant", "vine", "glow_lichen", "sugar_cane", "kelp", "kelp_plant", "seagrass",
    "tall_seagrass", "torch", "wall_torch", "small_dripleaf", "big_dripleaf", "big_dripleaf_stem",
    "hanging_roots", "spore_blossom", "brown_mushroom", "red_mushroom", "torchflower", "pitcher_plant",
    "lily_pad", "snow_layer_dummy", "light", "structure_void", "barrier", "cobweb", "weeping_vines",
    "weeping_vines_plant", "twisting_vines", "twisting_vines_plant", "nether_sprouts", "crimson_roots",
    "warped_roots", "bush", "firefly_bush", "leaf_litter", "wildflowers", "short_dry_grass", "tall_dry_grass",
}
EMPTY_SUFFIX = ("_sapling", "_tulip", "_button", "_pressure_plate", "_sign", "_banner", "_coral",
                "_coral_fan", "_torch", "_rail", "rail")

PALETTE = [  # (match, sRGB) - first match wins; "=" exact, "*" substring
    ("=snow", (240, 244, 250)), ("=snow_block", (240, 244, 250)), ("=powder_snow", (236, 242, 250)),
    ("=grass_block", (106, 158, 68)), ("=moss_block", (89, 128, 48)), ("=moss_carpet", (89, 128, 48)),
    ("=dirt", (125, 90, 60)), ("=coarse_dirt", (118, 85, 58)), ("=rooted_dirt", (130, 95, 70)),
    ("=podzol", (92, 64, 30)), ("=mud", (60, 57, 60)), ("=dirt_path", (148, 122, 65)),
    ("=farmland", (110, 75, 45)), ("=mycelium", (110, 98, 105)),
    ("=stone", (125, 125, 125)), ("=andesite", (136, 136, 137)), ("=diorite", (189, 188, 189)),
    ("=granite", (149, 103, 86)), ("=tuff", (108, 109, 103)), ("=calcite", (223, 224, 220)),
    ("=deepslate", (80, 80, 83)), ("=cobblestone", (127, 127, 127)), ("=bedrock", (85, 85, 85)),
    ("=gravel", (131, 127, 126)), ("=sand", (219, 207, 163)), ("=red_sand", (190, 103, 33)),
    ("=sandstone", (216, 203, 155)), ("=clay", (160, 166, 179)), ("=water", (48, 84, 190)),
    ("=ice", (145, 183, 253)), ("=packed_ice", (141, 180, 250)), ("=blue_ice", (116, 167, 253)),
    ("=lava", (230, 110, 20)), ("=magma_block", (142, 63, 31)), ("=obsidian", (20, 18, 30)),
    ("=cherry_leaves", (229, 164, 196)), ("=spruce_leaves", (52, 82, 52)), ("=birch_leaves", (110, 150, 70)),
    ("=azalea_leaves", (90, 117, 44)), ("=flowering_azalea_leaves", (100, 112, 60)),
    ("*leaves", (60, 110, 45)), ("=cherry_log", (54, 33, 44)), ("=birch_log", (216, 215, 210)),
    ("=spruce_log", (58, 37, 16)), ("*_log", (102, 81, 50)), ("*_wood", (102, 81, 50)),
    ("*_planks", (162, 130, 78)), ("*terracotta", (152, 94, 67)), ("*_ore", (125, 125, 125)),
    ("=snowy_dummy", (0, 0, 0)),
]


def classify(names, biomes=None, colors=None):
    """Per-mapping-id arrays: is_empty (bool), thin height fraction (0 = full),
    sRGB color per face group [top, side, bottom] -> shape (n+1, 3, 3)."""
    n = len(names)
    empty = np.zeros(n + 1, bool)
    thin = np.zeros(n + 1, np.float32)
    col = np.zeros((n + 1, 3, 3), np.float32)
    empty[n] = True  # sentinel for out-of-range ids
    for i, full in enumerate(names):
        b = base_name(full)
        if b in EMPTY_EXACT or b.endswith(EMPTY_SUFFIX):
            empty[i] = True
            continue
        if b == "snow":
            try:
                layers = int(full.split("layers:")[1].split("}")[0])
            except (IndexError, ValueError):
                layers = 1
            thin[i] = min(layers, 8) / 8.0 if layers < 8 else 0.0
        elif b.endswith("_carpet"):
            thin[i] = 1.0 / 16.0
        if colors is not None:
            try:
                tc = colors.colors(full, biomes[i] if biomes else "minecraft:plains")
            except Exception:
                tc = None
            if tc is not None:
                col[i] = np.array(tc, np.float32)
                continue
        rgb = None
        for key, c in PALETTE:
            if (key[0] == "=" and b == key[1:]) or (key[0] == "*" and key[1:] in b):
                rgb = c
                break
        if rgb is None:  # stable pastel hash so unknown blocks are at least distinguishable
            h = 0
            for ch in b:
                h = (h * 131 + ord(ch)) & 0xFFFFFF
            rgb = (90 + (h & 0xFF) % 130, 90 + ((h >> 8) & 0xFF) % 130, 90 + ((h >> 16) & 0xFF) % 130)
        col[i] = np.array([rgb] * 3, np.float32)
    return empty, thin, col


def srgb_to_linear(c):
    c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)



def build_faces(P, cw, skip_enclosed):
    """Returns per-face arrays: kind (0 top,1 bottom,2 E,3 W,4 S,5 N), gx, gz, lo, hi, rgb, sec.
    A face is emitted where a solid run touches empty space (air/plant); with skip_enclosed only
    where that empty space has sky light > 0 (i.e. not sealed caves)."""
    n = len(P["b"])
    solid = ~P["empty"]
    vis_empty = P["empty"] & ((P["sky"] > 0) if skip_enclosed else True)
    faces = []
    # top: point above (index i-1, same column) is visible empty, or nothing above
    above_ok = np.zeros(n, bool)
    above_ok[1:] = vis_empty[:-1]
    top = solid & (P["first"] | above_ok)
    # bottom: point below is visible empty
    below_ok = np.zeros(n, bool)
    below_ok[:-1] = vis_empty[1:]
    bot = solid & ~P["last"] & below_ok
    for kind, m, y in ((0, top, P["t"]), (1, bot, P["b"])):
        idx = np.flatnonzero(m)
        faces.append((np.full(len(idx), kind), P["gx"][idx], P["gz"][idx], y[idx], y[idx], idx))
    # sides: intersect each solid run with visible empty runs of the neighbour column
    gx0, gz0 = P["gx"].min() - 1, P["gz"].min() - 1
    nzs = int(P["gz"].max() - gz0 + 2)
    key = (P["gx"] - gx0) * nzs + (P["gz"] - gz0)
    # open space next to a column = its visible empty runs, PLUS the unfilled upper part of thin
    # blocks (snow layers, carpets): a 1/8 snow layer must not hide a full block of its neighbour.
    e_idx = np.flatnonzero(vis_empty)
    e_b, e_t = P["b"][e_idx], P["t"][e_idx]
    if "tf" in P:
        above_open = np.ones(n, bool)
        above_open[1:] = vis_empty[:-1]
        thin_idx = np.flatnonzero(solid & (P["tf"] > P["t"] + 1e-4) & (P["first"] | above_open))
        e_idx = np.r_[e_idx, thin_idx]
        e_b = np.r_[e_b, P["t"][thin_idx]]
        e_t = np.r_[e_t, P["tf"][thin_idx]]
    order = np.argsort(key[e_idx], kind="stable")
    e_key = key[e_idx][order]
    e_b, e_t = e_b[order], e_t[order]
    s_idx = np.flatnonzero(solid)
    for kind, dx, dz in ((2, 1, 0), (3, -1, 0), (4, 0, 1), (5, 0, -1)):
        nkey = key[s_idx] + dx * nzs + dz
        start = np.searchsorted(e_key, nkey, "left")
        end = np.searchsorted(e_key, nkey, "right")
        cnt = end - start
        tot = int(cnt.sum())
        if tot == 0:
            continue
        rep = np.repeat(np.arange(len(s_idx)), cnt)
        off = np.arange(tot) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        ej = np.repeat(start, cnt) + off
        si = s_idx[rep]
        lo = np.maximum(P["b"][si], e_b[ej])
        hi = np.minimum(P["t"][si], e_t[ej])
        ok = lo < hi
        si, lo, hi = si[ok], lo[ok], hi[ok]
        faces.append((np.full(len(si), kind), P["gx"][si], P["gz"][si], lo, hi, si))
    kind = np.concatenate([f[0] for f in faces])
    gx = np.concatenate([f[1] for f in faces])
    gz = np.concatenate([f[2] for f in faces])
    lo = np.concatenate([f[3] for f in faces])
    hi = np.concatenate([f[4] for f in faces])
    src = np.concatenate([f[5] for f in faces])
    return kind, gx, gz, lo, hi, src




# =============================================================================== meshing

TILE_COLS = 256  # tile = 256 x 256 columns at the ring's level (= 4 x 4 sections)


SNOWY_CAPABLE = {"grass_block", "podzol", "mycelium"}
SNOW_COVER = {"snow", "snow_block", "powder_snow"}


def snowy_table(names, biomes, colors, col):
    """Colours for the snowy variant of each grass-like block (others unchanged)."""
    alt = col.copy()
    for i, full in enumerate(names):
        if base_name(full) in SNOWY_CAPABLE:
            snowy = full.replace("{snowy:false}", "{snowy:true}")
            if "{snowy:" not in snowy:
                snowy = full.split("_STATE_")[0] + "_STATE_{snowy:true}"
            tc = None
            if colors is not None:
                try:
                    tc = colors.colors(snowy, biomes[i] if biomes else "minecraft:plains")
                except Exception:
                    tc = None
            if tc is not None:
                alt[i] = np.array(tc, np.float32)
            else:  # palette mode: snow-dusted dirt side
                alt[i, 1] = (169, 151, 133)
    return alt


def section_arrays(px, pz, pts, names, biomes, level, min_y, colors):
    """Per-section point arrays (compact) + this section's colour table."""
    empty, thin, col = classify(names, biomes, colors)
    if len(pts["id"]) == 0:
        return None
    ids = np.minimum(pts["id"], len(names)).astype(np.int32)
    # Minecraft rule: grass/podzol/mycelium is "snowy" whenever snow sits directly on it.
    # DH sometimes stores snowy:false under snow, so apply the rule here.
    n1 = len(names) + 1
    capable = np.array([base_name(x) in SNOWY_CAPABLE for x in names] + [False])
    cover = np.array([base_name(x) in SNOW_COVER for x in names] + [False])
    same_col_above = np.r_[False, pts["col"][1:] == pts["col"][:-1]]
    above_id = np.r_[n1 - 1, ids[:-1]]
    snowy = capable[ids] & same_col_above & cover[above_id]
    if snowy.any():
        col = np.concatenate([col, snowy_table(names, biomes, colors, col)])
        ids = np.where(snowy, ids + n1, ids).astype(np.int32)
    base_ids = ids % n1
    c = pts["col"]
    b = (pts["bottom"] + min_y).astype(np.float32)
    h = pts["height"].astype(np.float32)
    th = thin[base_ids]
    t = np.where(th > 0, b + (h - 1) + th, b + h).astype(np.float32)
    first = np.ones(len(c), bool)
    first[1:] = c[1:] != c[:-1]
    last = np.ones(len(c), bool)
    last[:-1] = c[1:] != c[:-1]
    return {"gx": (px * W + c // W).astype(np.int64), "gz": (pz * W + c % W).astype(np.int64),
            "b": b, "t": t, "tf": (b + h).astype(np.float32), "empty": empty[base_ids],
            "sky": pts["sky"].astype(np.uint8),
            "pid": ids, "first": first, "last": last, "table": col}


class SectionCache:
    """Reads + decodes + classifies sections on demand, keeps the most recent ones (LRU)."""

    def __init__(self, db_path, min_y, colors, size=48):
        uri = "file:" + urllib.request.pathname2url(os.path.abspath(db_path)) + "?mode=ro"
        self.con = sqlite3.connect(uri, uri=True)
        self.min_y, self.colors, self.size = min_y, colors, size
        self.data = {}
        self.order = []
        self.skipped = {}  # sections this version can't read, by reason (format/compression changes)

    def get(self, level, px, pz):
        k = (level, px, pz)
        if k in self.data:
            self.order.remove(k)
            self.order.append(k)
            return self.data[k]
        row = self.con.execute(
            "SELECT Data, NorthAdjData, SouthAdjData, EastAdjData, WestAdjData, Mapping, CompressionMode, "
            "DataFormatVersion FROM FullData WHERE DetailLevel=? AND PosX=? AND PosZ=?", k).fetchone()
        arr = None
        if row is not None:
            if row[7] != 2:
                self.skipped[f"DataFormatVersion {row[7]}"] = self.skipped.get(f"DataFormatVersion {row[7]}", 0) + 1
            elif row[6] not in (0, 2, 3, 4):
                self.skipped[f"CompressionMode {row[6]}"] = self.skipped.get(f"CompressionMode {row[6]}", 0) + 1
            else:
                pts, (names, biomes) = decode_section(row[:7])
                arr = section_arrays(px, pz, pts, names, biomes, level, self.min_y, self.colors)
        self.data[k] = arr
        self.order.append(k)
        while len(self.order) > self.size:
            self.data.pop(self.order.pop(0), None)
        return arr

    def close(self):
        self.con.close()
        self.data.clear()
        self.order.clear()


def ring_layout(cx, cz, rings):
    """rings: [(outer_distance_blocks, level), ...] inner -> outer.
    Returns [(level, inner_box, outer_box)], boxes = (x0, x1, z0, z1) half-open block coords.
    Each outer box is snapped outward to the NEXT ring's column grid so rings tile exactly."""
    out = []
    prev = None
    for i, (dist, level) in enumerate(rings):
        snap = 2 ** (rings[i + 1][1] if i + 1 < len(rings) else level)
        snap = max(snap, 2 ** level)
        box = ((cx - dist) // snap * snap, -(-(cx + dist) // snap) * snap,
               (cz - dist) // snap * snap, -(-(cz + dist) // snap) * snap)
        if prev is not None:  # never smaller than the previous ring
            box = (min(box[0], prev[0]), max(box[1], prev[1]), min(box[2], prev[2]), max(box[3], prev[3]))
        out.append((level, prev, box))
        prev = box
    return out


def ring_tiles(level, inner, outer, cx, cz):
    """Tile boxes (block coords) of this ring, nearest first."""
    tb = TILE_COLS * 2 ** level
    tiles = []
    for tx in range(outer[0] // tb, -(-outer[1] // tb)):
        for tz in range(outer[2] // tb, -(-outer[3] // tb)):
            box = (tx * tb, tx * tb + tb, tz * tb, tz * tb + tb)
            if inner is not None and box[0] >= inner[0] and box[1] <= inner[1] and box[2] >= inner[2] and box[3] <= inner[3]:
                continue  # entirely inside the finer ring
            tiles.append(box)
    tiles.sort(key=lambda b: max(abs((b[0] + b[1]) / 2 - cx), abs((b[2] + b[3]) / 2 - cz)))
    return tiles


def estimate_faces(cx, cz, rings, hole, per_col=3.5, merge=1.5):
    est = []
    for level, inner, outer in ring_layout(cx, cz, rings):
        area = (outer[1] - outer[0]) * (outer[3] - outer[2])
        if inner:
            area -= (inner[1] - inner[0]) * (inner[3] - inner[2])
        if hole:
            hw = max(0, min(hole[1] + 1, outer[1]) - max(hole[0], outer[0]))
            hd = max(0, min(hole[3] + 1, outer[3]) - max(hole[2], outer[2]))
            if inner:
                iw = max(0, min(hole[1] + 1, inner[1]) - max(hole[0], inner[0]))
                idd = max(0, min(hole[3] + 1, inner[3]) - max(hole[2], inner[2]))
                hw_hd = hw * hd - iw * idd
            else:
                hw_hd = hw * hd
            area -= max(0, hw_hd)
        est.append(area / 4 ** level * per_col / merge)
    return est


def in_box(bx, bz, box):
    return (bx >= box[0]) & (bx < box[1]) & (bz >= box[2]) & (bz < box[3])


def greedy(kind, gx, gz, lo, hi, cid):
    """Merge unit faces into rectangles. Returns kind, gx0, gx1, gz0, gz1 (inclusive column ranges), lo, hi, cid."""
    out = []

    def runs(keys, seq):
        n = len(seq)
        if n == 0:
            return np.zeros(0, int), np.zeros(0, int)
        brk = np.ones(n, bool)
        brk[1:] = seq[1:] != seq[:-1] + 1
        for k in keys:
            brk[1:] |= k[1:] != k[:-1]
        s = np.flatnonzero(brk)
        e = np.r_[s[1:] - 1, n - 1]
        return s, e

    # top / bottom: merge along x, then along z
    for k in (0, 1):
        m = kind == k
        if not m.any():
            continue
        X, Z, Y, C = gx[m], gz[m], (hi[m] if k == 0 else lo[m]), cid[m]
        o = np.lexsort((X, Z, C, Y))
        X, Z, Y, C = X[o], Z[o], Y[o], C[o]
        s, e = runs((Y, C, Z), X)
        x0, x1, z, y, c = X[s], X[e], Z[s], Y[s], C[s]
        o = np.lexsort((z, x1, x0, c, y))
        x0, x1, z, y, c = x0[o], x1[o], z[o], y[o], c[o]
        s, e = runs((y, c, x0, x1), z)
        out.append((np.full(len(s), k), x0[s], x1[s], z[s], z[e], y[s], y[s], c[s]))
    # E / W walls: plane at x, merge along z
    for k in (2, 3):
        m = kind == k
        if not m.any():
            continue
        X, Z, L, H, C = gx[m], gz[m], lo[m], hi[m], cid[m]
        o = np.lexsort((Z, C, H, L, X))
        X, Z, L, H, C = X[o], Z[o], L[o], H[o], C[o]
        s, e = runs((X, L, H, C), Z)
        out.append((np.full(len(s), k), X[s], X[s], Z[s], Z[e], L[s], H[s], C[s]))
    # S / N walls: plane at z, merge along x
    for k in (4, 5):
        m = kind == k
        if not m.any():
            continue
        X, Z, L, H, C = gx[m], gz[m], lo[m], hi[m], cid[m]
        o = np.lexsort((X, C, H, L, Z))
        X, Z, L, H, C = X[o], Z[o], L[o], H[o], C[o]
        s, e = runs((Z, L, H, C), X)
        out.append((np.full(len(s), k), X[s], X[e], Z[s], Z[s], L[s], H[s], C[s]))
    if not out:
        return None
    return tuple(np.concatenate([o[i] for o in out]) for i in range(8))


def rect_vertices(kind, gx0, gx1, gz0, gz1, lo, hi, cw):
    """Blender coords in blocks (X = mcX, Y = -mcZ, Z = mcY), outward CCW winding. (F,4,3)"""
    x0 = gx0 * cw
    x1 = (gx1 + 1) * cw
    y0 = -((gz1 + 1) * cw)
    y1 = -(gz0 * cw)
    v = np.zeros((len(kind), 4, 3), np.float64)

    def setq(m, a, b, c, d):
        if m.any():
            for j, p in enumerate((a, b, c, d)):
                v[m, j] = np.stack(p, -1)[m]

    setq(kind == 0, (x0, y0, hi), (x1, y0, hi), (x1, y1, hi), (x0, y1, hi))
    setq(kind == 1, (x0, y1, lo), (x1, y1, lo), (x1, y0, lo), (x0, y0, lo))
    setq(kind == 2, (x1, y0, lo), (x1, y1, lo), (x1, y1, hi), (x1, y0, hi))
    setq(kind == 3, (x0, y1, lo), (x0, y0, lo), (x0, y0, hi), (x0, y1, hi))
    setq(kind == 4, (x0, y0, lo), (x1, y0, lo), (x1, y0, hi), (x0, y0, hi))
    setq(kind == 5, (x1, y1, lo), (x0, y1, lo), (x0, y1, hi), (x1, y1, hi))
    return v


def build_tile(cache, level, tile, inner, outer, hole, skip_enclosed, skirt=True, merge=True):
    """Mesh one tile of one ring. Returns (verts (F,4,3) in blocks, rgb (F,3) sRGB 0-255) or None."""
    cw = 2 ** level
    sw = W * cw
    # sections overlapping the tile plus a 1-column margin (for correct face culling at edges)
    px0, px1 = (tile[0] - cw) // sw, (tile[1] - 1 + cw) // sw
    pz0, pz1 = (tile[2] - cw) // sw, (tile[3] - 1 + cw) // sw
    parts = [a for px in range(px0, px1 + 1) for pz in range(pz0, pz1 + 1)
             if (a := cache.get(level, px, pz)) is not None]
    if not parts:
        return None
    # concatenate, offsetting palette ids
    off = 0
    pal = []
    P = {k: [] for k in ("gx", "gz", "b", "t", "tf", "empty", "sky", "pid", "first", "last")}
    for a in parts:
        bx, bz = a["gx"] * cw, a["gz"] * cw
        keep = (bx >= tile[0] - cw) & (bx < tile[1] + cw) & (bz >= tile[2] - cw) & (bz < tile[3] + cw)
        if not keep.any():
            continue
        for k in P:
            v = a[k][keep]
            P[k].append(v + off if k == "pid" else v)
        pal.append(a["table"])
        off += len(a["table"])
    if not pal:
        return None
    P = {k: np.concatenate(v) for k, v in P.items()}
    pal = np.concatenate(pal)
    kind, gx, gz, lo, hi, src = build_faces(P, cw, skip_enclosed)

    # keep faces whose column is in this tile, in this ring, and not in the hole
    bx, bz = gx * cw, gz * cw
    keep = in_box(bx, bz, tile) & in_box(bx, bz, outer)
    if inner is not None:
        keep &= ~in_box(bx, bz, inner)
    if hole is not None:
        mx, mz = bx + cw / 2.0, bz + cw / 2.0
        keep &= ~((mx >= hole[0]) & (mx < hole[1] + 1) & (mz >= hole[2]) & (mz < hole[3] + 1))
    kind, gx, gz, lo, hi, src = kind[keep], gx[keep], gz[keep], lo[keep], hi[keep], src[keep]
    slot = np.where(kind == 0, 0, np.where(kind == 1, 2, 1))
    rgb = pal[P["pid"][src], slot]

    # skirts: short walls on ring seams so level changes don't show cracks
    if skirt:
        solid = ~P["empty"]
        cbx, cbz = P["gx"] * cw, P["gz"] * cw
        # first solid point of each column
        colkey = P["gx"] * 1_000_003 + P["gz"]
        idx = np.flatnonzero(solid)
        _, firsts = np.unique(colkey[idx], return_index=True)
        tops = idx[firsts]
        tbx, tbz = cbx[tops], cbz[tops]
        own = in_box(tbx, tbz, tile) & in_box(tbx, tbz, outer)
        if inner is not None:
            own &= ~in_box(tbx, tbz, inner)
        if hole is not None:
            mx, mz = tbx + cw / 2.0, tbz + cw / 2.0
            own &= ~((mx >= hole[0]) & (mx < hole[1] + 1) & (mz >= hole[2]) & (mz < hole[3] + 1))
        depth = max(4.0, 3.0 * cw)
        sk = []
        for k, dx, dz in ((2, 1, 0), (3, -1, 0), (4, 0, 1), (5, 0, -1)):
            nx, nz = tbx + dx * cw, tbz + dz * cw
            other = ~in_box(nx, nz, outer)
            if inner is not None:
                other |= in_box(nx, nz, inner)
            m = own & other
            if m.any():
                t = tops[m]
                sk.append((np.full(m.sum(), k), P["gx"][t], P["gz"][t], P["t"][t] - depth, P["t"][t],
                           pal[P["pid"][t], 1]))
        if sk:
            kind = np.concatenate([kind] + [s[0] for s in sk])
            gx = np.concatenate([gx] + [s[1] for s in sk])
            gz = np.concatenate([gz] + [s[2] for s in sk])
            lo = np.concatenate([lo] + [s[3] for s in sk])
            hi = np.concatenate([hi] + [s[4] for s in sk])
            rgb = np.concatenate([rgb] + [s[5] for s in sk])
    if len(kind) == 0:
        return None

    c8 = np.clip(np.round(rgb), 0, 255).astype(np.int64)
    cid = (c8[:, 0] << 16) | (c8[:, 1] << 8) | c8[:, 2]
    if merge:
        r = greedy(kind, gx, gz, lo, hi, cid)
        kind, gx0, gx1, gz0, gz1, lo, hi, cid = r
    else:
        gx0, gx1, gz0, gz1 = gx, gx, gz, gz
    rgb = np.stack([(cid >> 16) & 255, (cid >> 8) & 255, cid & 255], -1).astype(np.float32)
    return rect_vertices(kind, gx0, gx1, gz0, gz1, lo, hi, cw), rgb


# =============================================================================== Mineways alignment

def mesh_heightmap(centers, normals, areas, scale, cx, cz, oy):
    """Up-facing polygon centres (Blender world coords) -> MC columns (x, z) and top heights (MC Y),
    using the CURRENT mapping (center/origin/scale). Keeps the highest face per column."""
    up = normals[:, 2] > 0.9
    small = areas < (1.6 * scale * scale)          # single-block faces only (merged faces are ambiguous)
    m = up & small
    c = centers[m]
    if len(c) == 0:
        return None
    mx = c[:, 0] / scale + cx
    mz = cz - c[:, 1] / scale
    my = c[:, 2] / scale + oy
    gx, gz = np.floor(mx).astype(np.int64), np.floor(mz).astype(np.int64)
    key = (gx - gx.min()) * (gz.max() - gz.min() + 1) + (gz - gz.min())
    o = np.lexsort((-my, key))
    first = np.r_[True, key[o][1:] != key[o][:-1]]
    sel = o[first]
    return gx[sel], gz[sel], my[sel]


def dh_surface_grid(cache, x0, x1, z0, z1):
    """Level-0 top-of-surface heights for blocks [x0,x1) x [z0,z1); NaN where no data."""
    grid = np.full((x1 - x0, z1 - z0), np.nan, np.float32)
    for px in range(x0 // W, (x1 - 1) // W + 1):
        for pz in range(z0 // W, (z1 - 1) // W + 1):
            a = cache.get(0, px, pz)
            if a is None:
                continue
            solid = ~a["empty"]
            idx = np.flatnonzero(solid)
            if len(idx) == 0:
                continue
            ck = a["gx"][idx] * 1_000_003 + a["gz"][idx]
            _, f = np.unique(ck, return_index=True)
            t = idx[f]
            gx, gz, h = a["gx"][t], a["gz"][t], a["t"][t]
            ok = (gx >= x0) & (gx < x1) & (gz >= z0) & (gz < z1)
            grid[gx[ok] - x0, gz[ok] - z0] = h[ok]
    return grid


def best_shift(mx, mz, mh, dh_grid, gx0, gz0, search, samples=6000, seed=1):
    """Find (sx, sz, dy) so that mineways_height(c) ~= dh_height(c + s) + dy.
    dh_grid covers blocks starting at (gx0, gz0). Returns sx, sz, dy, score, runner_up_score."""
    rng = np.random.default_rng(seed)
    if len(mx) > samples:
        pick = rng.choice(len(mx), samples, replace=False)
        mx, mz, mh = mx[pick], mz[pick], mh[pick]
    results = []
    nx, nz = dh_grid.shape
    for sx in range(-search, search + 1):
        ix = mx + sx - gx0
        okx = (ix >= 0) & (ix < nx)
        for sz in range(-search, search + 1):
            iz = mz + sz - gz0
            ok = okx & (iz >= 0) & (iz < nz)
            if ok.sum() < 50:
                continue
            d = mh[ok] - dh_grid[ix[ok], iz[ok]]
            d = d[~np.isnan(d)]
            if len(d) < 50:
                continue
            dy = np.median(d)
            score = float(np.mean(np.abs(d - dy) <= 0.6)) * min(1.0, len(d) / (0.5 * len(mx)))
            results.append((score, sx, sz, float(dy)))
    if not results:
        return None
    results.sort(reverse=True)
    best = results[0]
    # runner-up = best score that is not a direct neighbour of the winner
    runner = next((r[0] for r in results[1:] if max(abs(r[1] - best[1]), abs(r[2] - best[2])) > 2), 0.0)
    return best[1], best[2], best[3], best[0], runner


# =============================================================================== Blender

try:
    import bpy
    from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty
except ImportError:  # allows testing the decoder/mesher with plain Python
    bpy = None

if bpy is not None:
    _COLORS = None
    DEFAULT_RINGS = ((400, 0), (2000, 2), (8000, 4), (32000, 6), (64000, 7))

    def deps_dir():
        return bpy.utils.user_resource("SCRIPTS", path="modules", create=True)

    class DHI_OT_install_deps(bpy.types.Operator):
        bl_idname = "dhi.install_deps"
        bl_label = "Install zstandard"
        bl_description = "pip-install the zstandard package into your Blender user modules folder"

        def execute(self, context):
            import subprocess
            target = deps_dir()
            py = sys.executable
            if subprocess.run([py, "-m", "pip", "--version"], capture_output=True).returncode != 0:
                r = subprocess.run([py, "-m", "ensurepip", "--user"], capture_output=True, text=True)
                print("ensurepip:", r.stdout[-1500:], r.stderr[-1500:])
            c = [py, "-m", "pip", "install", "--upgrade", "--target", target, "zstandard"]
            r = subprocess.run(c, capture_output=True, text=True)
            print(" ".join(c), "\n", r.stdout[-2000:], r.stderr[-2000:])
            if r.returncode != 0:
                self.report({"ERROR"}, f"pip failed (Window > Toggle System Console for details): {r.stderr[-300:]}")
                return {"CANCELLED"}
            import importlib
            importlib.invalidate_caches()
            if target not in sys.path:
                sys.path.append(target)
            global _ZSTD
            _ZSTD = get_zstd()
            self.report({"INFO"}, "zstandard installed" if _ZSTD else "Installed - restart Blender")
            return {"FINISHED"}

    class DHI_Prefs(bpy.types.AddonPreferences):
        bl_idname = __name__

        def draw(self, context):
            col = self.layout.column()
            ok = get_zstd() is not None
            col.label(text=("zstandard: available" if ok else "zstandard: NOT installed"),
                      icon=("CHECKMARK" if ok else "ERROR"))
            col.operator("dhi.install_deps")
            col.label(text=f"Blender Python: {sys.version.split()[0]}   target: {deps_dir()}")

    def _ring_props():
        d = {}
        for i, (dist, lvl) in enumerate(DEFAULT_RINGS, 1):
            d[f"ring{i}_on"] = BoolProperty(name=f"Ring {i}", default=True)
            d[f"ring{i}_dist"] = IntProperty(name="Out to", default=dist, min=16, max=200000,
                                             description="Outer edge of this ring, in blocks from the centre")
            d[f"ring{i}_level"] = IntProperty(name="Level", default=lvl, min=0, max=9,
                                              description="0 = 1 block per column, 1 = 2, 2 = 4 ... 7 = 128")
        return d

    class DHI_Settings(bpy.types.PropertyGroup):
        __annotations__ = dict(
            db_path=StringProperty(name="Database", subtype="FILE_PATH",
                                   default="", description="DistantHorizons.sqlite - singleplayer: saves/<world>/data/"),
            center_x=IntProperty(name="Center X", default=0, description="Minecraft block X placed at the Blender origin"),
            center_z=IntProperty(name="Center Z", default=0, description="Minecraft block Z placed at the Blender origin"),
            origin_y=IntProperty(name="Origin Y", default=64, description="Minecraft Y placed at Blender Z = 0"),
            min_y=IntProperty(name="World bottom Y", default=-64),
            scale=FloatProperty(name="Scale (m per block)", default=1.0, min=0.001, max=10.0),
            skip_enclosed=BoolProperty(name="Skip sealed caves", default=True,
                                       description="Only build faces that touch sky-lit air"),
            merge=BoolProperty(name="Merge faces (greedy)", default=True),
            skirts=BoolProperty(name="Ring seam skirts", default=True,
                                description="Short walls along ring borders to hide cracks between detail levels"),
            face_cap=IntProperty(name="Face cap", default=10_000_000, min=1000,
                                 description="Stop importing when the total reaches this many faces"),
            tiles_per_tick=IntProperty(name="Tiles per update", default=1, min=1, max=16,
                                       description="Higher = faster import, less responsive UI"),
            align_search=IntProperty(name="Search radius", default=32, min=4, max=256,
                                     description="How many blocks off the Mineways placement may be (bigger = slower)"),
            use_hole=BoolProperty(name="Leave hole (Mineways area)", default=False,
                                  description="Skip DH terrain inside this rectangle so a Mineways export can fill it"),
            hole_min_x=IntProperty(name="Min X", default=-128),
            hole_max_x=IntProperty(name="Max X", default=127),
            hole_min_z=IntProperty(name="Min Z", default=-128),
            hole_max_z=IntProperty(name="Max Z", default=127),
            use_textures=BoolProperty(name="Texture colors (DH style)", default=True,
                                      description="Average each block's real textures + biome tint, like Distant Horizons"),
            jar_path=StringProperty(name="Minecraft jar", subtype="FILE_PATH",
                                    default="", description="Minecraft client jar, e.g. .minecraft/versions/1.20.1/1.20.1.jar"),
            pack_paths=StringProperty(name="Resource packs", default="",
                                      description="Optional: resource pack .zip files or folders, separated by ;  (first wins)"),
            **_ring_props(),
        )

    def active_rings(s):
        rings = []
        for i in range(1, len(DEFAULT_RINGS) + 1):
            if getattr(s, f"ring{i}_on"):
                rings.append((getattr(s, f"ring{i}_dist"), getattr(s, f"ring{i}_level")))
        rings.sort()
        return rings

    def hole_of(s):
        return (s.hole_min_x, s.hole_max_x, s.hole_min_z, s.hole_max_z) if s.use_hole else None

    def get_material():
        mat = bpy.data.materials.get("DH_VertexColor")
        if mat:
            return mat
        mat = bpy.data.materials.new("DH_VertexColor")
        try:
            mat.use_nodes = True  # deprecated (always on) in newer Blender
        except Exception:
            pass
        nt = mat.node_tree
        bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
        attr = nt.nodes.new("ShaderNodeVertexColor")
        attr.layer_name = "Col"
        attr.location = (-300, 200)
        nt.links.new(attr.outputs["Color"], bsdf.inputs["Base Color"])
        bsdf.inputs["Roughness"].default_value = 0.9
        return mat

    def make_object(name, verts, rgb_lin, coll, mat, location):
        F = len(verts)
        me = bpy.data.meshes.new(name)
        me.vertices.add(F * 4)
        me.vertices.foreach_set("co", verts.reshape(-1).astype(np.float32))
        me.loops.add(F * 4)
        me.loops.foreach_set("vertex_index", np.arange(F * 4, dtype=np.int32))
        me.polygons.add(F)
        me.polygons.foreach_set("loop_start", np.arange(0, F * 4, 4, dtype=np.int32))
        try:
            me.polygons.foreach_set("loop_total", np.full(F, 4, np.int32))
        except (AttributeError, RuntimeError, TypeError):
            pass  # read-only in Blender 4.0+, derived from loop_start
        me.update(calc_edges=True)
        ca = me.color_attributes.new(name="Col", type="BYTE_COLOR", domain="CORNER")
        rgba = np.ones((F, 4, 4), np.float32)
        rgba[:, :, :3] = rgb_lin[:, None, :]
        ca.data.foreach_set("color", rgba.reshape(-1))
        me.materials.append(mat)
        ob = bpy.data.objects.new(name, me)
        ob.location = location
        coll.objects.link(ob)
        return ob

    class DHI_OT_import(bpy.types.Operator):
        bl_idname = "dhi.import_rings"
        bl_label = "Import"
        bl_description = "Import all enabled rings around Center X/Z (Esc to cancel)"

        _timer = None

        def invoke(self, context, event):
            s = context.scene.dhi
            path = bpy.path.abspath(s.db_path)
            if not os.path.isfile(path):
                self.report({"ERROR"}, f"Database not found: {path}")
                return {"CANCELLED"}
            if get_zstd() is None:
                self.report({"ERROR"}, "zstandard missing - see add-on preferences")
                return {"CANCELLED"}
            rings = active_rings(s)
            if not rings:
                self.report({"ERROR"}, "No rings enabled")
                return {"CANCELLED"}
            hole = hole_of(s)
            if hole and (hole[0] > hole[1] or hole[2] > hole[3]):
                self.report({"ERROR"}, "Hole: Min must be <= Max")
                return {"CANCELLED"}
            colors = None
            if s.use_textures:
                jar = bpy.path.abspath(s.jar_path)
                packs = [bpy.path.abspath(x.strip()) for x in s.pack_paths.split(";") if x.strip()]
                if os.path.isfile(jar):
                    global _COLORS
                    key = (jar, tuple(packs))
                    if _COLORS is None or _COLORS[0] != key:
                        _COLORS = (key, BlockColors(jar, packs))
                    colors = _COLORS[1]
                else:
                    self.report({"WARNING"}, f"Minecraft jar not found ({jar}) - using the simple palette")

            self.s = s
            self.hole = hole
            self.cache = SectionCache(path, s.min_y, colors)
            self.layout_ = ring_layout(s.center_x, s.center_z, rings)
            self.jobs = []
            root = bpy.data.collections.get("DH Import") or bpy.data.collections.new("DH Import")
            if root.name not in context.scene.collection.children:
                context.scene.collection.children.link(root)
            self.colls = []
            for ri, (level, inner, outer) in enumerate(self.layout_):
                lo_d = 0 if ri == 0 else rings[ri - 1][0]
                name = f"DH Ring {ri + 1}  L{level} ({lo_d}-{rings[ri][0]})"
                c = bpy.data.collections.get(name) or bpy.data.collections.new(name)
                if c.name not in root.children:
                    root.children.link(c)
                self.colls.append(c)
                for tile in ring_tiles(level, inner, outer, s.center_x, s.center_z):
                    self.jobs.append((ri, level, inner, outer, tile))
            self.n_jobs = len(self.jobs)
            self.done = 0
            self.faces = 0
            self.objects = 0
            self.capped = False
            self.t0 = time.time()
            self.mat = get_material()
            self.area = context.area
            wm = context.window_manager
            wm.progress_begin(0, max(1, self.n_jobs))
            self._timer = wm.event_timer_add(0.01, window=context.window)
            wm.modal_handler_add(self)
            # viewport clip so the whole import is visible
            far = max(rings[-1][0], 1) * s.scale * 3
            for area in context.screen.areas:
                if area.type == "VIEW_3D":
                    for sp in area.spaces:
                        if sp.type == "VIEW_3D":
                            sp.clip_end = max(sp.clip_end, far)
                            sp.clip_start = min(sp.clip_start, max(0.01, s.scale / 10))
            # render camera(s) too: Blender's default Clip End is only 100 m
            for cam in {o.data for o in context.scene.objects if o.type == "CAMERA"}:
                cam.clip_end = max(cam.clip_end, far)
            print(f"[DH] {self.n_jobs} tiles in {len(rings)} rings")
            return {"RUNNING_MODAL"}

        def status(self, text):
            if self.area:
                self.area.header_text_set(text)

        def finish(self, context, msg, level="INFO"):
            wm = context.window_manager
            if self._timer:
                wm.event_timer_remove(self._timer)
                self._timer = None
            wm.progress_end()
            self.status(None)
            self.cache.close()
            self.report({level}, msg)
            print("[DH]", msg)

        def modal(self, context, event):
            if event.type == "ESC":
                self.finish(context, f"Cancelled after {self.done}/{self.n_jobs} tiles, {self.faces:,} faces", "WARNING")
                return {"CANCELLED"}
            if event.type != "TIMER":
                return {"PASS_THROUGH"}
            s = self.s
            try:
                for _ in range(s.tiles_per_tick):
                    if self.done >= self.n_jobs or self.capped:
                        break
                    ri, level, inner, outer, tile = self.jobs[self.done]
                    self.done += 1
                    res = build_tile(self.cache, level, tile, inner, outer, self.hole, s.skip_enclosed,
                                     skirt=s.skirts, merge=s.merge)
                    if res is None:
                        continue
                    verts, rgb = res
                    if self.faces + len(verts) > s.face_cap:
                        self.capped = True
                        break
                    # world -> Blender, object origin at the tile centre (keeps float precision)
                    verts[..., 0] = (verts[..., 0] - s.center_x) * s.scale
                    verts[..., 1] = (verts[..., 1] + s.center_z) * s.scale
                    verts[..., 2] = (verts[..., 2] - s.origin_y) * s.scale
                    loc = verts.reshape(-1, 3).mean(0)
                    loc[2] = 0.0
                    verts -= loc
                    make_object(f"DH_R{ri + 1}_L{level}_{tile[0]}_{tile[2]}", verts,
                                srgb_to_linear(rgb).astype(np.float32), self.colls[ri], self.mat, tuple(loc))
                    self.faces += len(verts)
                    self.objects += 1
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.finish(context, f"{type(e).__name__}: {e}", "ERROR")
                return {"CANCELLED"}
            context.window_manager.progress_update(self.done)
            self.status(f"DH import: tile {self.done}/{self.n_jobs}  ring {self.jobs[min(self.done, self.n_jobs) - 1][0] + 1}"
                        f"  {self.faces:,} faces  {time.time() - self.t0:.0f}s   (Esc to cancel)")
            if self.capped:
                self.finish(context, f"Face cap {s.face_cap:,} reached: {self.faces:,} faces, "
                                     f"{self.n_jobs - self.done + 1} tiles skipped (outer rings)", "WARNING")
                return {"FINISHED"}
            if self.done >= self.n_jobs:
                if self.cache.skipped:
                    why = ", ".join(f"{k} x{v}" for k, v in self.cache.skipped.items())
                    self.finish(context, f"Imported {self.faces:,} faces, but skipped sections this add-on "
                                         f"can't read ({why}) - DH format may have changed", "WARNING")
                else:
                    self.finish(context, f"Imported {self.faces:,} faces in {self.objects} objects "
                                         f"({time.time() - self.t0:.0f}s)")
                return {"FINISHED"}
            return {"RUNNING_MODAL"}


    class DHI_OT_align(bpy.types.Operator):
        bl_idname = "dhi.align_mineways"
        bl_label = "Align to selected Mineways"
        bl_description = ("Match the selected Mineways terrain against DH full-detail heights, then set "
                          "Center X/Z, Origin Y, Scale and the hole so DH lines up with it exactly")

        def execute(self, context):
            s = context.scene.dhi
            obs = [o for o in context.selected_objects if o.type == "MESH"]
            if not obs:
                self.report({"ERROR"}, "Select the Mineways terrain object(s) first")
                return {"CANCELLED"}
            path = bpy.path.abspath(s.db_path)
            if not os.path.isfile(path) or get_zstd() is None:
                self.report({"ERROR"}, "Database not found or zstandard missing")
                return {"CANCELLED"}
            dg = context.evaluated_depsgraph_get()
            C, N, A = [], [], []
            for ob in obs:
                ev = ob.evaluated_get(dg)
                me = ev.to_mesh()
                n = len(me.polygons)
                if n:
                    c = np.empty(n * 3, np.float32)
                    nr = np.empty(n * 3, np.float32)
                    ar = np.empty(n, np.float32)
                    me.polygons.foreach_get("center", c)
                    me.polygons.foreach_get("normal", nr)
                    me.polygons.foreach_get("area", ar)
                    M = np.array(ob.matrix_world, np.float64)
                    R = M[:3, :3]
                    c = c.reshape(-1, 3) @ R.T + M[:3, 3]
                    nr = nr.reshape(-1, 3) @ np.linalg.inv(R)
                    nr /= np.maximum(np.linalg.norm(nr, axis=1, keepdims=True), 1e-12)
                    sc = abs(np.linalg.det(R)) ** (1 / 3)
                    C.append(c)
                    N.append(nr)
                    A.append(ar * sc * sc)
                ev.to_mesh_clear()
            if not C:
                self.report({"ERROR"}, "Selected objects have no faces")
                return {"CANCELLED"}
            C, N, A = np.concatenate(C), np.concatenate(N), np.concatenate(A)
            up = N[:, 2] > 0.9
            if up.sum() < 100:
                self.report({"ERROR"}, "Not enough upward faces - is the Mineways mesh Z-up?")
                return {"CANCELLED"}
            # block size of the Mineways mesh = most common upward face edge length
            sizes = np.sqrt(A[up])
            vals, cnt = np.unique(np.round(sizes, 4), return_counts=True)
            block = float(vals[np.argmax(cnt)])
            scale = s.scale
            if abs(block / scale - 1) > 0.02:
                scale = block
            hm = mesh_heightmap(C, N, A, scale, s.center_x, s.center_z, s.origin_y)
            if hm is None:
                self.report({"ERROR"}, "No single-block upward faces found")
                return {"CANCELLED"}
            mx, mz, mh = hm
            S = s.align_search
            x0, x1, z0, z1 = int(mx.min()) - S, int(mx.max()) + S + 1, int(mz.min()) - S, int(mz.max()) + S + 1
            cache = SectionCache(path, s.min_y, None)
            try:
                grid = dh_surface_grid(cache, x0, x1, z0, z1)
            finally:
                cache.close()
            if np.isnan(grid).all():
                self.report({"ERROR"}, "DH has no full-detail (level 0) data under the selection")
                return {"CANCELLED"}
            r = best_shift(mx, mz, mh, grid, x0, z0, S)
            if r is None:
                self.report({"ERROR"}, "Could not compare - too little overlap with DH data")
                return {"CANCELLED"}
            sx, sz, dy, score, runner = r
            msg = (f"match {score:.0%} (next best {runner:.0%}); shift X {sx:+d}, Z {sz:+d}, Y {dy:+.2f}; "
                   f"Mineways block = {block:.4g} m")
            print("[DH align]", msg)
            if score < 0.6 or score - runner < 0.2:
                self.report({"WARNING"}, "No confident match - nothing changed. " + msg +
                            ". Try a larger search radius, or check the Mineways mesh isn't rotated/mirrored.")
                return {"CANCELLED"}
            s.scale = scale
            s.center_x += sx
            s.center_z += sz
            s.origin_y = int(round(s.origin_y - dy))
            s.hole_min_x, s.hole_max_x = int(mx.min()) + sx, int(mx.max()) + sx
            s.hole_min_z, s.hole_max_z = int(mz.min()) + sz, int(mz.max()) + sz
            s.use_hole = True
            if abs(dy - round(dy)) > 0.1:
                msg += " (non-integer Y shift: Origin Y rounded, check the seam height)"
            self.report({"INFO"}, "Aligned: " + msg)
            return {"FINISHED"}

    class DHI_PT_panel(bpy.types.Panel):
        bl_label = "Distant Horizons Import"
        bl_idname = "DHI_PT_panel"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "DH Import"

        def draw(self, context):
            s = context.scene.dhi
            col = self.layout.column()
            col.prop(s, "db_path")
            r = col.row(align=True)
            r.prop(s, "center_x")
            r.prop(s, "center_z")
            col.prop(s, "origin_y")
            col.prop(s, "min_y")
            col.prop(s, "scale")

            box = col.box()
            box.label(text="Rings (blocks from centre)")
            rings = active_rings(s)
            est = estimate_faces(s.center_x, s.center_z, rings, hole_of(s)) if rings else []
            ei = 0
            for i in range(1, len(DEFAULT_RINGS) + 1):
                r = box.row(align=True)
                r.prop(s, f"ring{i}_on", text=f"{i}")
                sub = r.row(align=True)
                sub.active = getattr(s, f"ring{i}_on")
                sub.prop(s, f"ring{i}_dist")
                sub.prop(s, f"ring{i}_level")
            tot = sum(est)
            box.label(text=f"Estimated ~{tot / 1e6:.1f}M faces (cap {s.face_cap / 1e6:.1f}M)",
                      icon=("ERROR" if tot > s.face_cap else "INFO"))

            col.prop(s, "face_cap")
            col.prop(s, "skip_enclosed")
            col.prop(s, "merge")
            col.prop(s, "skirts")
            col.prop(s, "tiles_per_tick")
            col.separator()
            r = col.row(align=True)
            r.operator("dhi.align_mineways", icon="SNAP_ON")
            r.prop(s, "align_search", text="Search")
            col.prop(s, "use_hole")
            if s.use_hole:
                r = col.row(align=True)
                r.prop(s, "hole_min_x")
                r.prop(s, "hole_max_x")
                r = col.row(align=True)
                r.prop(s, "hole_min_z")
                r.prop(s, "hole_max_z")
                col.label(text=f"hole: {s.hole_max_x - s.hole_min_x + 1} x {s.hole_max_z - s.hole_min_z + 1} blocks")
            col.separator()
            col.prop(s, "use_textures")
            if s.use_textures:
                col.prop(s, "jar_path")
                col.prop(s, "pack_paths")
            if get_zstd() is None:
                col.label(text="zstandard missing: see add-on prefs", icon="ERROR")
            col.operator("dhi.import_rings", icon="IMPORT")

    CLASSES = (DHI_OT_install_deps, DHI_Prefs, DHI_Settings, DHI_OT_import, DHI_OT_align, DHI_PT_panel)

    def register():
        mod = deps_dir()
        if mod not in sys.path:
            sys.path.append(mod)
        for c in CLASSES:
            bpy.utils.register_class(c)
        bpy.types.Scene.dhi = bpy.props.PointerProperty(type=DHI_Settings)

    def unregister():
        del bpy.types.Scene.dhi
        for c in reversed(CLASSES):
            bpy.utils.unregister_class(c)
