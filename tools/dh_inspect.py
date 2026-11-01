#!/usr/bin/env python3
"""
dh_inspect.py - read-only inspector for a Distant Horizons .sqlite database.

Read-only and memory-safe: it never loads a whole table, only a handful of rows.
It prints the schema (same as sqlite's .schema), row counts, which detail levels
exist, position ranges per level, and for a few sample rows per detail level it
hex-dumps the blob headers and tries every common decompressor so we can
identify the blob format together instead of guessing.

Usage (PowerShell or cmd):
    py dh_inspect.py "C:\\path\\to\\DistantHorizons.sqlite"

Options:
    --samples N    sample rows per detail level (default 2)
    --no-count     skip full-table COUNT / GROUP BY scans (use if they take too long)
    --out DIR      where to write dh_report.txt and dh_samples\\ (default: next to this script)

Outputs:
    dh_report.txt      everything printed to the console
    dh_samples\\        raw sample blobs (*.bin) and decompressed versions (*.dec.bin)
"""
import argparse
import gzip
import lzma
import math
import os
import platform
import re
import sqlite3
import struct
import sys
import time
import urllib.request
import zlib
from collections import Counter

MAX_BLOB_PROBE = 64 * 1024 * 1024   # never try to decompress anything larger than this
MAX_SAVE_BYTES = 4 * 1024 * 1024    # cap per saved sample file
HEX_HEAD = 96                       # bytes of each blob to hex-dump
STR_RE = re.compile(rb"[ -~]{6,}")  # printable ASCII runs (block names etc.)

LEVEL_RE = re.compile(r"(?i)detail|lod|level")
POS_RE = re.compile(r"(?i)pos|^[xyz]$|^(section|chunk|region)_?[xz]$")
META_RE = re.compile(r"(?i)compress|format|version|type|mode|worldgen|step")


# ----------------------------------------------------------------------------- output

class Tee:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")

    def __call__(self, *args):
        s = " ".join(str(a) for a in args)
        print(s)
        self.f.write(s + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def hexdump(b, indent=""):
    lines = []
    for i in range(0, len(b), 16):
        c = b[i:i + 16]
        hx = " ".join(f"{x:02x}" for x in c)
        asc = "".join(chr(x) if 32 <= x < 127 else "." for x in c)
        lines.append(f"{indent}{i:04x}  {hx:<47}  {asc}")
    return "\n".join(lines)


def entropy(b):
    if not b:
        return 0.0
    n = len(b)
    return -sum(v / n * math.log2(v / n) for v in Counter(b).values())


def q(name):
    return '"' + name.replace('"', '""') + '"'


def fmt_val(v, maxlen=300):
    if isinstance(v, str) and len(v) > maxlen:
        return repr(v[:maxlen]) + f"... ({len(v)} chars)"
    return repr(v)


# ----------------------------------------------------------------------------- decoders

MAGICS = [  # (bytes, name, search anywhere in header?)
    (b"\x04\x22\x4d\x18", "lz4-frame", True),
    (b"\x28\xb5\x2f\xfd", "zstd", True),
    (b"\xfd7zXZ\x00", "xz", True),
    (b"LZ4Block", "lz4-java LZ4BlockOutputStream", True),
    (b"\x1f\x8b\x08", "gzip", True),
    (b"\x78\x01", "zlib (level low)", False),
    (b"\x78\x5e", "zlib (fast)", False),
    (b"\x78\x9c", "zlib (default)", False),
    (b"\x78\xda", "zlib (best)", False),
]


def find_magics(b, window=64):
    hits = []
    head = b[:window + 8]
    for magic, name, anywhere in MAGICS:
        if anywhere:
            start = 0
            while True:
                i = head.find(magic, start)
                if i < 0 or i > window:
                    break
                hits.append((i, name))
                start = i + 1
        elif head.startswith(magic):
            hits.append((0, name))
    return sorted(hits)


def lz4java_decode(b, lz4block):
    """lz4-java LZ4BlockOutputStream: repeated 21-byte headers
    ('LZ4Block' + token + compLen LE + decompLen LE + checksum LE) followed by data."""
    out = bytearray()
    pos = 0
    while pos < len(b):
        if b[pos:pos + 8] != b"LZ4Block":
            raise ValueError(f"no LZ4Block magic at offset {pos}")
        token = b[pos + 8]
        clen, dlen, _chk = struct.unpack_from("<iii", b, pos + 9)
        if clen < 0 or dlen < 0 or dlen > MAX_BLOB_PROBE:
            raise ValueError("implausible LZ4Block sizes")
        data = b[pos + 21:pos + 21 + clen]
        method = token & 0xF0
        if clen == 0 and dlen == 0:          # end-of-stream marker
            pos += 21
            continue
        if method == 0x10:                   # stored raw
            out += data
        elif method == 0x20:                 # LZ4 compressed
            if lz4block is None:
                raise ValueError("LZ4Block stream found but the lz4 module is not installed")
            out += lz4block.decompress(data, uncompressed_size=dlen)
        else:
            raise ValueError(f"unknown LZ4Block method 0x{method:02x}")
        pos += 21 + clen
    return bytes(out)


def build_decoders():
    decs, missing = [], []
    decs += [
        ("zlib", zlib.decompress),
        ("deflate-raw", lambda b: zlib.decompress(b, -15)),
        ("gzip", gzip.decompress),
        ("xz/lzma", lzma.decompress),
    ]

    lz4block = None
    try:
        import lz4.block
        import lz4.frame
        lz4block = lz4.block

        def le_prefixed(b):
            n = struct.unpack_from("<I", b, 0)[0]
            if not 0 < n <= MAX_BLOB_PROBE:
                raise ValueError("implausible size prefix")
            return lz4.block.decompress(b)

        def be_prefixed(b):
            n = struct.unpack_from(">I", b, 0)[0]
            if not 0 < n <= MAX_BLOB_PROBE:
                raise ValueError("implausible size prefix")
            return lz4.block.decompress(b[4:], uncompressed_size=n)

        decs.append(("lz4-frame", lz4.frame.decompress))
        decs.append(("lz4-block, 4-byte LE size prefix", le_prefixed))
        decs.append(("lz4-block, 4-byte BE size prefix", be_prefixed))
    except ImportError:
        missing.append("lz4        ->  py -m pip install lz4")
    decs.append(("lz4-java LZ4Block stream", lambda b: lz4java_decode(b, lz4block)))

    zstd_ok = False
    try:
        import zstandard
        decs.append(("zstd (zstandard)",
                     lambda b: zstandard.ZstdDecompressor().decompressobj().decompress(b)))
        zstd_ok = True
    except ImportError:
        pass
    try:
        from compression import zstd as czstd  # Python 3.14+ stdlib
        decs.append(("zstd (stdlib compression.zstd)", czstd.decompress))
        zstd_ok = True
    except ImportError:
        pass
    if not zstd_ok:
        missing.append("zstandard  ->  py -m pip install zstandard")
    return decs, missing


# ----------------------------------------------------------------------------- probing

def describe_bytes(log, b, indent):
    log(f"{indent}length: {len(b):,} bytes   entropy(first 4 KB): {entropy(b[:4096]):.2f} bits/byte"
        "   (~8 = compressed/random, lower = structured)")
    log(hexdump(b[:HEX_HEAD], indent=indent))
    if len(b) >= 32:
        log(f"{indent}first 8 int32 BE: {list(struct.unpack_from('>8i', b, 0))}")
        log(f"{indent}first 8 int32 LE: {list(struct.unpack_from('<8i', b, 0))}")
    strings = [m.group().decode("ascii") for m in STR_RE.finditer(b[:2_000_000])]
    mc = b[:2_000_000].count(b"minecraft:")
    if strings:
        log(f"{indent}'minecraft:' occurrences: {mc}   first strings: {strings[:12]}")


def probe_blob(log, b, decoders, save_base):
    ind = "        "
    describe_bytes(log, b, ind)
    with open(save_base + ".bin", "wb") as f:
        f.write(b[:MAX_SAVE_BYTES])

    hits = find_magics(b)
    log(f"{ind}magic numbers in header: {hits if hits else 'none'}")
    if len(b) > MAX_BLOB_PROBE:
        log(f"{ind}(blob larger than {MAX_BLOB_PROBE:,} bytes - skipping decompression probe)")
        return

    offsets = sorted({0} | {o for o, _ in hits})
    successes = []
    for off in offsets:
        chunk = b[off:]
        for name, fn in decoders:
            try:
                out = fn(chunk)
            except Exception:
                continue
            if out:
                successes.append((off, name, out))

    if not successes:
        log(f"{ind}DECOMPRESSION: no decoder succeeded at offsets {offsets}")
        return
    for off, name, out in successes:
        log(f"{ind}DECOMPRESSION OK: {name} at offset {off} -> {len(out):,} bytes "
            f"(ratio {len(out) / max(1, len(b) - off):.2f}x)")
    off, name, out = successes[0]
    with open(save_base + ".dec.bin", "wb") as f:
        f.write(out[:MAX_SAVE_BYTES])
    log(f"{ind}decompressed data ({name}):")
    describe_bytes(log, out, ind + "  ")


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Inspect a Distant Horizons sqlite file (read-only).")
    ap.add_argument("db")
    ap.add_argument("--samples", type=int, default=2)
    ap.add_argument("--no-count", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    db = os.path.abspath(args.db)
    if not os.path.isfile(db):
        sys.exit(f"File not found: {db}")
    outdir = os.path.abspath(args.out or os.path.dirname(os.path.abspath(__file__)))
    sampledir = os.path.join(outdir, "dh_samples")
    os.makedirs(sampledir, exist_ok=True)
    log = Tee(os.path.join(outdir, "dh_report.txt"))

    log("=" * 78)
    log("DH SQLITE INSPECTION REPORT")
    log("=" * 78)
    log(f"python      : {sys.version.split()[0]}  ({sys.executable})")
    log(f"platform    : {platform.platform()}")
    log(f"sqlite lib  : {sqlite3.sqlite_version}")
    log(f"database    : {db}")
    log(f"db size     : {os.path.getsize(db) / 1024**3:.2f} GB")
    for ext in ("-wal", "-shm", "-journal"):
        side = db + ext
        if os.path.exists(side):
            log(f"sidecar     : {os.path.basename(side)}  {os.path.getsize(side) / 1024**2:.1f} MB"
                "  (if large, Minecraft may not have closed cleanly)")

    decoders, missing = build_decoders()
    log(f"decoders    : {[n for n, _ in decoders]}")
    if missing:
        log("MISSING     : " + " | ".join(missing))
        log("              (install these and re-run so the probe can test every format)")

    uri = "file:" + urllib.request.pathname2url(db) + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.OperationalError as e:
        sys.exit(f"Could not open database: {e}\nClose Minecraft completely and try again.")

    log("\n--- PRAGMAS " + "-" * 66)
    for p in ("page_size", "page_count", "freelist_count", "user_version",
              "application_id", "encoding", "journal_mode"):
        try:
            log(f"{p:15}: {con.execute(f'PRAGMA {p}').fetchone()[0]}")
        except sqlite3.Error as e:
            log(f"{p:15}: error {e}")

    log("\n--- SCHEMA (.schema) " + "-" * 57)
    objs = con.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type DESC, name").fetchall()
    for typ, name, tbl, sql in objs:
        log(f"[{typ}] {name}" + (f"  (on {tbl})" if typ != "table" else ""))
        log(f"    {sql}" if sql else "    (auto)")
    tables = [n for t, n, _, _ in objs if t == "table"]

    for t in tables:
        log("\n" + "=" * 78)
        log(f"TABLE {t}")
        log("=" * 78)
        cols = con.execute(f"PRAGMA table_info({q(t)})").fetchall()
        for cid, name, ctype, notnull, dflt, pk in cols:
            log(f"  col {cid}: {name:28} {ctype or '(no type)':12} pk={pk} notnull={notnull}")
        colnames = [c[1] for c in cols]
        blob_cols = {c[1] for c in cols if "BLOB" in (c[2] or "").upper()}
        level_col = next((c for c in colnames if LEVEL_RE.search(c) and c not in blob_cols), None)
        pos_cols = [c for c in colnames if POS_RE.search(c) and c not in blob_cols]
        meta_cols = [c for c in colnames if META_RE.search(c) and c not in blob_cols
                     and c != level_col and c not in pos_cols]
        log(f"  guessed detail-level column: {level_col}   position columns: {pos_cols}")

        count = None
        levels = []
        if not args.no_count:
            t0 = time.time()
            count = con.execute(f"SELECT COUNT(*) FROM {q(t)}").fetchone()[0]
            log(f"  rows: {count:,}   ({time.time() - t0:.1f}s)")

            for c in ([level_col] if level_col else []) + meta_cols:
                t0 = time.time()
                rows = con.execute(
                    f"SELECT {q(c)}, COUNT(*) FROM {q(t)} GROUP BY {q(c)} ORDER BY {q(c)} LIMIT 51"
                ).fetchall()
                more = " (showing first 50 distinct values)" if len(rows) > 50 else ""
                log(f"  distinct {c}{more}  ({time.time() - t0:.1f}s):")
                for v, n in rows[:50]:
                    log(f"      {fmt_val(v, 80):30} {n:>12,}")
                if c == level_col:
                    levels = [v for v, _ in rows[:50]]

            if level_col and pos_cols:
                agg = ", ".join(f"MIN({q(p)}), MAX({q(p)})" for p in pos_cols)
                rows = con.execute(
                    f"SELECT {q(level_col)}, {agg} FROM {q(t)} GROUP BY {q(level_col)} "
                    f"ORDER BY {q(level_col)}").fetchall()
                log(f"  position ranges per {level_col}:")
                for r in rows:
                    parts = [f"{p}=[{r[1 + 2 * i]}..{r[2 + 2 * i]}]" for i, p in enumerate(pos_cols)]
                    log(f"      {level_col}={r[0]}: " + "  ".join(parts))

            if count <= 50 and not blob_cols:
                log("  (small table - dumping all rows)")
                for r in con.execute(f"SELECT * FROM {q(t)}"):
                    log("      " + ", ".join(f"{n}={fmt_val(v, 120)}" for n, v in zip(colnames, r)))
                continue
        elif level_col:
            for lv in range(0, 16):  # cheap existence probe without a full scan
                if con.execute(f"SELECT 1 FROM {q(t)} WHERE {q(level_col)}=? LIMIT 1",
                               (lv,)).fetchone():
                    levels.append(lv)
            log(f"  detail levels found (probe 0-15): {levels}")

        # ---- samples
        groups = [(lv, f"SELECT * FROM {q(t)} WHERE {q(level_col)}=? LIMIT ?", (lv, args.samples))
                  for lv in levels] if level_col and levels else \
                 [(None, f"SELECT * FROM {q(t)} LIMIT ?", (args.samples,))]
        for lv, sql, params in groups:
            tag = f"{level_col}={lv}" if lv is not None else "first rows"
            for i, row in enumerate(con.execute(sql, params)):
                log(f"\n  --- sample {i} ({tag}) ---")
                for name, v in zip(colnames, row):
                    if isinstance(v, (bytes, bytearray)):
                        log(f"    {name}: <BLOB>")
                        safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{t}_{tag}_{i}_{name}")
                        probe_blob(log, bytes(v), decoders, os.path.join(sampledir, safe))
                    else:
                        log(f"    {name} = {fmt_val(v)}")
                del row

    con.close()
    total = sum(os.path.getsize(os.path.join(sampledir, f)) for f in os.listdir(sampledir))
    log("\n" + "=" * 78)
    log(f"Done. Report: {os.path.join(outdir, 'dh_report.txt')}")
    log(f"Samples: {sampledir}  ({total / 1024**2:.1f} MB)")
    log.close()


if __name__ == "__main__":
    main()
