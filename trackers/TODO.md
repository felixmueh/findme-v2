# Small random TODOs
## Frontend
## Backend
### Do we cache query requests? What if different parts of the frontent do the same query a coup

## Search ranking consistency

- [x] Resolve the current inconsistency around Python ranking usage.
  Exact findings from the current code:
  - Source priority is applied in the Python ranker with `score += asset.source_priority * 0.01`.
  - Source priority is also used as a tie-breaker when sorting Python-ranked candidates.
  - Source priority is used for default browse/loading order from SQLite.
  - The frontend defaults to `strict` search mode.
  - Normal strict search bypasses the Python ranker when the Tantivy lexical index exists and returns Tantivy scores directly.
  - Therefore source priority affects browse/no-query ordering and fuzzy/domain/combined Python-ranked search, but it mostly does not affect the current frontend default strict search.
  - Combined ranking exists server-side and is the API default when `mode` is omitted, but the frontend exposes only strict, fuzzy, and domain tabs.

- [x] Remove the strict-mode Python ranking fallback path.
  The old `_rank_candidates()` object-ranking path has been removed.
