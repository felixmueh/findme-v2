# Search Profile Scope Review

Exploratory notes for the combined `WIP search profiles rewrite` and
`Optimize search profile scoping` changes.

## Observations

- Moving folder quality and selection into compiled `SearchPreferences` is the
  right simplification. It removes mutable ranking state from `SearchIndex` and
  gives strict, fuzzy, and domain search one preference path.
- `directory_subtree_asset_indexes` is the right primitive for both inherited
  folder quality and profile selection.
- The API-level scoped browse caches were compensating for full-scope
  materialization. Browse/card pagination now uses compiled asset indexes
  directly.
- Directory responses should keep matched folders separate from included
  folders. Blacklisted folders now remain visible with `included_asset_count`
  set to zero.

## Candidate Direction

- Keep `SearchPreferences` as the boundary object.
- For browse pages, page over `included_asset_index_order` directly when profile
  selection is present; avoid building all scoped `SearchResult` objects for
  every first page. Done.
- For directory counts, compute:
  - `asset_count` from the full indexed directory counts
  - `matched_asset_count` from the active query or browse scope before profile
    inclusion
  - `included_asset_count` from the same match set after profile inclusion
- Keep blacklisted directories visible when they have matched assets, even if
  their included count is zero. Done.
- Remove `_ScopedValueCache` only after direct pagination and count derivation
  make it unnecessary. Done.

## Probe Coverage

- `test_api_profile_blacklisted_folder_remains_visible_for_unblocking` covers
  empty-query browse.
- `test_api_profile_blacklisted_search_match_keeps_directory_match_count` covers
  active search queries.
