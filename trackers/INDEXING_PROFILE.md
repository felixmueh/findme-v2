# Indexing Profile Notes

## 2026-05-12 Temp Fixture Baseline

Fixture and artifacts:

- root: `/tmp/findme-index-profile.2OKqI1`
- config: `/tmp/findme-index-profile.2OKqI1/profile.toml`
- fixture: `/tmp/findme-index-profile.2OKqI1/fixture`
- cProfile output: `/tmp/findme-index-profile.2OKqI1/index-internal-cold.cprofile`

Fixture contents were symlinks into the DND source tree:

- total: 528 files
- images: 452 files (`220 webp`, `140 png`, `80 jpg`, `12 gif`)
- videos: 16 files (`mp4`)
- audio: 60 files (`mp3`)

Cold run with empty database and no thumbnails:

- count indexable files: 0.7-0.8s
- index including thumbnail generation: 6.9-7.2s
- rebuild search corpus: 0.06-0.10s
- total internal run: 7.7-8.0s
- generated thumbnails: 468

Artificial no-thumbnail lower bound on the same fixture:

- count indexable files: 0.794s
- scan/write DB/fingerprint with thumbnails disabled: 1.488s
- rebuild search corpus: 0.058s
- total: 2.341s

Thumbnail worker timing from the cold fixture run:

- images: 452 thumbnails, 13.343s total worker time, p50 0.021s, p90 0.048s, max 0.337s
- videos: 16 thumbnails, 8.125s total worker time, p50 0.497s, p90 0.538s, max 0.605s

Observation: thumbnail generation dominated the cold fixture. The CLI default
progress logger was also noisy because it emitted a complete progress line for
every thumbnail completion after scanning reached `total_files`; that has a
separate fix in progress.

## 2026-05-12 Production Fast-Path Timing

Environment:

- config: `config/findme.server.toml`
- database: `/var/lib/findme-v2/findme.sqlite3`
- source root family: `/media/NAS/main/Documents/DND`
- starting database state: 287112 assets, 281504 thumbnail references
- starting index and search fingerprints matched

This run used the existing production database and did not delete the database
or thumbnail cache. It exercised the same high-level phases as `findme index`:
count, index replacement, then search corpus rebuild.

Results:

- count indexable files: 156.503s
- index sources: 1191.436s
- rebuild search corpus: 61.170s
- total: 1421.117s
- scanned: 287112 files
- written: 287112 database rows
- generated thumbnails: 0
- thumbnail failures: 0
- ending database state: 287112 assets, 281504 thumbnail references
- ending index and search fingerprints matched

Observation: even with no thumbnail generation, the current full fast-path
index is slow. The pre-count alone is expensive, and `index_sources` dominates.
This points at directory traversal, per-file stat/path work, thumbnail cache
existence checks for unchanged files, full database replacement, and/or index
fingerprint computation as the next areas to isolate before optimizing
thumbnail generation.

## 2026-05-12 Fast-Path Simplification Benchmarks

Changes validated:

- removed the CLI pre-count from default indexing
- default indexing preserves existing DB rows by relative path
- default indexing stats only newly discovered files
- default indexing no longer checks every existing thumbnail file
- search preprocessing is skipped when the search corpus fingerprint is current
- `initialize_database()` no longer drops and recreates the current directory index

Production timing progression on `/var/lib/findme-v2/findme.sqlite3`:

- previous no-thumbnail run: 1421.117s total
- after path-preserving index flow, first run found one missing DB row:
  - index sources: 69.855s
  - rebuild search corpus: 61.529s
  - total: 138.259s
- after generated thumbnail for that new row and corpus rebuild, true no-change run:
  - index sources: 54.415s
  - written: 0
  - generated thumbnails: 0
  - search corpus stale: false
- after removing the repeated DB directory-index rebuild:
  - index sources: 42.366s
  - written: 0
  - generated thumbnails: 0
  - search corpus stale: false
- after loading only existing relative paths in the normal fast path:
  - index sources: 29.194s
  - written: 0
  - generated thumbnails: 0
  - search corpus stale: false
- actual CLI no-change run after the same changes:
  - wall time: 33s
  - scanned: 287113
  - written: 0
  - generated thumbnails: 0
  - search preprocessing skipped because corpus was current
- `powerwalk` benchmark:
  - installed `powerwalk==1.0.0` in the local venv
  - MIT licensed, Python >=3.12
  - auto thread count was slow: 134.718s and was not used
  - explicit 32-thread traversal with Python lowercase extension filtering:
    15.018s, 287113 assets found
- after wiring `powerwalk` into source enumeration:
  - internal no-change index sources: 17.470s
  - actual CLI no-change wall time: 21s
  - scanned: 287113
  - written: 0
  - generated thumbnails: 0
  - search preprocessing skipped because corpus was current

Current `powerwalk` source breakdown:

- load existing paths: about 2.1s total
- source scan/update:
  - tokens: 0.73s
  - maps: 1.16s
  - mapmaking: 12.24s
  - music: 0.05s
  - sounds: 0.42s
  - adventures: 0.70s

Scanner probes:

- Python sequential path-only count after scanner simplification: 25.339s
- Python top-level parallel scanner prototype: about 20.4s
- GNU `find` no-stat command in this sandbox: about 26.7s
- GNU `find -printf` with size/mtime stat in this sandbox: about 126.4s

Observation: the default fast path is now dominated by source path enumeration,
especially `MapMaking`. The active sandbox does not have `fdfind` installed and
the agent user cannot install apt packages, so the user's host-side `fdfind`
result could not be reproduced directly inside this environment.
