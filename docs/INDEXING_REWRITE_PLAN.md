# Indexing Rewrite Plan

## Goal

Keep indexing correctness simple:

- the database stores the current index, not current plus stale rows
- a source index replaces that source's index rows
- a full index replaces the whole index-owned store
- thumbnails are generated after the fresh asset rows are visible
- the search corpus can detect when it no longer matches the indexed database

Search and browse should never depend on a cleanup command for correctness.

## Tables

User/profile state stays durable:

- `search_profiles`
- `search_profile_folder_settings`
- configured `sources` rows, because profile settings reference sources

Index-owned rows are replaceable:

- `assets`
- `asset_search`

The existing `stale` column can remain in the schema for now, but normal code should not use it as an invalidation mechanism. Newly written rows should use `stale = 0`.

Add a small metadata table:

```sql
CREATE TABLE index_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

Use it for:

- `index_fingerprint`
- `search_corpus_fingerprint`

The corpus is stale when those values differ.

## Index Flow

1. Scan selected directories and collect file metadata.
2. Load existing asset rows for the selected sources before deleting anything.
3. Prepare fresh asset rows:
   - reuse existing thumbnail references when source-relative path, media type, size, and mtime still match and the thumbnail file exists
   - otherwise store `NULL` thumbnail fields
4. In one DB transaction, replace index rows for the scope:
   - full index: delete all `asset_search` and `assets` rows
   - source index: delete `asset_search` and `assets` rows for selected sources
   - upsert configured `sources`
   - insert the fresh asset rows
   - rebuild `asset_search` for the replaced sources
5. Generate missing thumbnails after the fresh asset rows are committed.
6. As each thumbnail completes, update its asset row only if the asset still has the same file fingerprint.
7. Rebuild the search corpus from the final DB state.
8. Store `search_corpus_fingerprint = index_fingerprint` when the rebuild succeeds.

The frontend sees old data during scanning, then a short transaction, then current data with thumbnails filling in.

## Thumbnail Cleanup

Thumbnail cleanup is a separate maintenance command. It scans generated thumbnails and deletes files that are not referenced by current `assets.thumbnail_path` values.

Indexing gets a `--cleanup-thumbnails` flag that runs the same maintenance step after a successful index and search-corpus rebuild.

## Non-Goals

- no generation system unless the short DB transaction proves too disruptive
- no source-directory writes
- no generic fallback for unknown environments
- no compatibility layer around stale rows
