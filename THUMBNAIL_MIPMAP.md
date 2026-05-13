# Thumbnail Mipmap Design Notes

This document captures the implemented backend/indexer design for multi-size
thumbnails/posters and the remaining caller work.

## Current Status

Done:

- Backend config supports app-wide extra sizes with
  `additional_thumbnail_mipmaps = [512, 1024]`.
- The canonical `asset_thumbnail_variants` table records available thumbnail
  and poster sizes.
- Still images generate variants with Pillow, BICUBIC resampling, WebP quality
  `82`, method `2`.
- Video posters generate eligible variants with `ffmpeg` in one multi-output
  invocation.
- Indexing writes thumbnail DB updates in batches and keeps
  `assets.thumbnail_*` populated as the temporary `256` baseline path.
- The search corpus snapshots compact thumbnail/poster variant metadata.
- Search and asset API responses expose `thumbnail_variants` with size,
  dimensions, and cacheable variant URLs.
- The frontend loads the smallest thumbnail first, promotes to larger cached
  variants after layout is known, and falls back to originals only when an image
  card is much larger than every available thumbnail variant.
- Production DND rebuild completed successfully on 2026-05-13:
  `287113` assets indexed, `281505` baseline thumbnails/posters generated,
  `415923` variant rows/files written, thumbnail cache size about `12G`.

Next step:

- Revisit whether `assets.thumbnail_path`, `assets.thumbnail_width`, and
  `assets.thumbnail_height` should be removed or derived from the variant table.

Do not continue evaluating alternative still-image thumbnail generators for now.
Pillow is the chosen implementation path.

## Goal

Card rendering should stay fast while still becoming sharper when the user
chooses larger grid cards.

The intended loading sequence is:

- Always load a small thumbnail first, currently expected to be `256`.
- After layout is known, decide whether a larger derivative is worth loading.
- Prefer generated thumbnail/poster variants over the original file.
- Fall back to the original only when the available variants are too small for
  the displayed card size.

## Candidate Sizes

Initial generated sizes:

- `256`
- `512`
- `1024`

`256` is the required baseline size. Additional generated sizes are configured
app-wide:

```toml
[app]
additional_thumbnail_mipmaps = [512, 1024]
```

Not every asset should have every size. The database/API must represent which
variants actually exist for each asset.

## Image Generation Rules

For still images:

- Generate `256` as the baseline thumbnail.
- Generate `512` only if the original image is larger than `512`.
- Generate `1024` only if the original image is larger than `1024`.
- Do not upscale originals into larger thumbnail variants.
- If the original is smaller than a candidate size, treat the original as the
  effective upper bound instead of generating that variant.

The chosen implementation uses Pillow with BICUBIC resampling. It opens the
source once, generates the largest eligible derivative first, then downscales
from that derivative to smaller variants.

## Frontend Selection Heuristic

The frontend should track the current displayed card image width and the size of
the currently loaded image source.

Base behavior:

- Load `256` first when available.
- On card resize, re-evaluate the needed source.
- Never downgrade a card instance once a larger source has been loaded.
- Pick the smallest available source that is good enough for the current
  displayed width.

Proposed fallback behavior when a larger thumbnail variant is missing:

- If a next thumbnail size is available and is not too much larger than the
  current thumbnail, load it.
- If no suitable thumbnail size exists, load the next available source only when
  the displayed width is much larger than the currently loaded thumbnail.
- "Much larger" is currently proposed as:

```text
display_width > 2 * current_thumbnail_size
```

Examples:

- Current source is `256`, display width is `400`, and `512` exists:
  load `512`.
- Current source is `256`, display width is `600`, and `512` is missing but
  `1024` exists:
  consider loading `1024` because the display is more than `2 * 256`.
- Current source is `256`, display width is `1000`, and no larger generated
  thumbnail exists:
  load the original, even if the original is a large map, because the display is
  far beyond the current thumbnail.
- Current source is `256`, display width is `400`, and only the original exists
  as the next source:
  keep `256`; do not jump straight to a large original for a modest display
  increase.

The "not too much larger" threshold is currently proposed as:

```text
next_source_size <= current_thumbnail_size * 2 + 100
```

## Original Fallback

The original file should remain a possible source.

Use it when:

- the displayed card size is far larger than the largest available thumbnail;
- the original is smaller than or close to the requested display size;
- there is no generated variant that can reasonably satisfy the card size.

Avoid it when:

- the card is only moderately larger than the `256` thumbnail;
- loading the original would mean pulling a very large map just to improve a
  small card.

## Video Posters

For video assets, the mipmap concept applies to poster images, not to loading
the full video in card grids.

Implemented behavior:

- Video posters use the same configured sizes as still-image thumbnails.
- `ffmpeg` generates all eligible poster sizes in one multi-output invocation.

## Storage And API Shape

The database should be canonical for variant availability. A likely model:

```text
asset_thumbnail_variants
- asset_id
- source_id
- relative_path
- kind: thumbnail | poster
- max_edge: 256 | 512 | 1024
- width
- height
- cache_path
- generated_from_size_bytes
- generated_from_mtime_ns
```

During the transition, `assets.thumbnail_path`, `assets.thumbnail_width`, and
`assets.thumbnail_height` remain as denormalized baseline `256` fields for the
current search/API/frontend path. `asset_thumbnail_variants` is the canonical
mipmap table, and the duplicated baseline path should be revisited after callers
consume variant metadata directly.

The search result/card payload should include compact variant metadata, for
example:

```text
thumbnail_variants: [
  { max_edge: 256, width: 256, height: 144, url: "..." },
  { max_edge: 512, width: 512, height: 288, url: "..." }
]
```

The search corpus likely needs this compact display metadata too, because the
hot path should not hydrate every card from the canonical database just to know
which thumbnails exist.

## Completed Benchmark Notes

The exploratory benchmark work is complete. Rough conclusions:

- Pillow was fastest enough and simplest enough for still images.
- Generating the largest eligible still-image derivative first and cascading
  down to smaller variants avoided a near-3x cost for three configured sizes.
- Fixture benchmark: single-size cold run was about `12s`; mipmap cold runs
  were about `13s` to `18s` for the same fixture.
- Production rebuild runtime was about `2h20m` for the real DND corpus.
- The bottleneck was image decode/resize/write work, not SQLite writes.
