# Distant Horizons `FullData` format (DataFormatVersion 2)

This describes what the importer reads. It was reimplemented from DH core (`FullDataSourceV2DTO`,
`FullDataPointIdMap`, `VarintUtil`, `FullDataSourceV2Repo`, `DhSectionPos`) and checked against DH 2.3.3 data.

## Table

```
FullData(DetailLevel, PosX, PosZ,                -- primary key
         Data, NorthAdjData, SouthAdjData, EastAdjData, WestAdjData,
         ColumnGenerationStep, ColumnWorldCompressionMode, Mapping,
         DataFormatVersion, CompressionMode, ...)
```

- **DetailLevel** `L`: each column is `2^L` blocks wide (DH section detail level = `6 + L`).
- **PosX / PosZ:** the section's position. A section is 64 × 64 columns, so it spans `64·2^L` blocks, and its
  minimum block corner is `PosX · 64 · 2^L`.
- **CompressionMode:** 0 uncompressed, 1 LZ4, 2 Zstandard stream, 3 LZMA2, 4 Zstandard block. Modes 2 and 4
  are both plain zstd frames. Every blob in the row uses the same mode.

## Column data blobs

Columns are indexed `relX * 64 + relZ`. To avoid duplicating edges, the data is split across blobs:

| Blob | Columns |
|---|---|
| `Data` | x 1..62, z 1..62 |
| `NorthAdjData` | z = 0 (all x) |
| `SouthAdjData` | z = 63 |
| `EastAdjData` | x = 63 |
| `WestAdjData` | x = 0 |

Each blob, after decompression, is five consecutive streams. Columns go x-outer, z-inner, and points within
a column go **top to bottom**. *varint* = unsigned LEB128, 7 bits per byte, low bits first.

1. **Point count** per column (varint).
2. **Per point:** `(mappingId << 2) | (hasLight << 1) | hasDiscontinuity` (varint).
3. **Per point:** `height`, the run thickness in blocks (varint).
4. **Per point that has hasDiscontinuity:** `zigzag(bottomY − expected)` (varint), where
   `expected = previousBottomY − height`. `previousBottomY` starts at 0 for the blob and carries across
   columns. `zigzag(n) = (n << 1) ^ (n >> 31)`.
5. **Per point that has hasLight:** one byte `(blockLight << 4) | skyLight`.

`bottomY` is relative to the world's minimum Y. Solid blocks usually store light 0; the light of the air
above them is what tells you whether a surface is open to the sky.

## Mapping blob

```
int32 BE  count
count × { uint16 BE length, bytes }     -- Java writeUTF, effectively ASCII
```

Each entry is `"<biome>_DH-BSW_<blockstate>"`, e.g.
`minecraft:cherry_grove_DH-BSW_minecraft:grass_block_STATE_{snowy:false}`. Air is `AIR`. Block-state
properties follow `_STATE_` as `{key:value}{key:value}`. The mapping id in stream 2 indexes this list.

## ColumnGenerationStep blob

4096 signed bytes, one per column: −1 down-sampled (filler copied from coarser levels), 0 empty,
5 surface, 7 terrain/carvers, 8 features, 9 light. Values ≥ 5 mean real data at this detail level.
