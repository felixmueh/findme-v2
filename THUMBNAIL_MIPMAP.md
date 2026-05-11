# Thumbnail Mipmap Design Notes

This document captures the working design for multi-size thumbnails/posters.
It is a planning note, not an implemented contract.

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

Not every asset should have every size. The database/API must represent which
variants actually exist for each asset.

## Image Generation Rules

For still images:

- Generate `256` as the baseline thumbnail.
- Generate `512` only if the original image is larger than `512`.
- Generate `1024` only if the original image is meaningfully larger than
  `1024`.
- Do not upscale originals into larger thumbnail variants.
- If the original is smaller than a candidate size, treat the original as the
  effective upper bound instead of generating that variant.

The exact "meaningfully larger" threshold for `1024` is still open. It should
avoid producing a `1024` derivative when the original is only trivially larger.

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

Open formula note:

The phrase `current_thumbnail_size**2 + 100` was suggested as "not too much
larger than the current thumbnail." Taken literally as exponentiation, that
would make the threshold extremely large for pixel sizes. Before implementation,
confirm whether the intended formula is closer to `current_thumbnail_size * 2 +
100`, `current_thumbnail_size + 100`, or another bounded threshold.

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

Open performance question:

- Is it faster to ask `ffmpeg` to extract each target poster size separately?
- Or is it faster to extract the largest poster size we care about, then
  downscale that image into `512` and `256`?
- Can extraction/downscaling be batched efficiently in one ffmpeg invocation?

This should be measured locally before choosing the indexing pipeline.

## Storage And API Shape

The database should be canonical for variant availability. A likely model:

```text
asset_thumbnail_variants
- source_id
- relative_path
- kind: thumbnail | poster
- max_edge: 256 | 512 | 1024
- width
- height
- cache_path
- generated_from_fingerprint
```

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

