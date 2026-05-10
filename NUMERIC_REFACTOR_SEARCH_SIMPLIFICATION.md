# Numeric Search Refactor / Simplification

## Why

Search/profile performance is currently limited less by algorithms and more by
Python object churn:

- building `_RankedCandidate` objects for huge result sets
- filtering via Python sets and per-object callbacks
- counting directories through dict/string tuple aggregation
- materializing `DirectoryMatch` objects and then converting them to API dicts

The data is simple and numeric:

- asset index
- raw lane score
- whitelist/blacklist inclusion
- folder importance adjustment
- asset leaf directory id
- directory parent id / sort order

This should be represented as arrays for the hot path.

## Current Direction

Keep the product contract simple:

- strict, fuzzy, and domain search use the same numeric serving path
- return one page of result cards
- return directory rows for the sidebar
- do not maintain extra internal compatibility layers unless still needed by a
  caller

Preferred runtime shape:

```text
retrieval lane -> asset_indexes + raw_scores arrays
profile mask -> filter included assets
importance array -> add score adjustment
top-k page -> only materialize returned assets
directory ids -> bincount + parent rollup
API directory rows -> build dicts directly
```

Strict search naturally returns `(asset_index, score)` from Tantivy. Fuzzy and
domain now emit the same shape before filtering, scoring, top-k, and directory
counting.

## Measured Findings

Real DND corpus:

- assets: 257,967
- directories: 5,520
- broad `map` strict query matches: 208,352

Before numeric strict path, broad selected searches were mostly Python object
work:

- current warm root whitelist strict `map`: about 30 ms
- vectorized whitelist count prototype: about 0.6 ms
- vectorized top 100 prototype: about 2-3 ms

After wiring search to the numeric path, rough timings:

- empty profile compile: about 0.2 ms
- large whitelist profile compile: about 9-11 ms
- browse directory response, all 5,520 rows: about 12-13 ms
- `map` combined strict page + directory rows: about 22-29 ms
- `castle` combined strict page + directory rows: about 6-10 ms
- `goblin` combined strict page + directory rows: about 6-10 ms
- fuzzy `goblin warror` combined page + directory rows: about 18-20 ms
- domain `adventurer` combined page + directory rows: about 155-225 ms

The API query path now asks for:

- one numeric result page
- one numeric directory-count response

It no longer calls the old full-materialization helper that requested
`limit=len(corpus)` before paging.

The remaining 9-11 ms large-whitelist compile cost is acceptable for now. Do not
add more machinery just to chase it down unless the change also simplifies the
main path.

The remaining broad `map` cost is now mostly:

- Tantivy retrieving many matches
- building thousands of directory response dictionaries
- some repeated array work between result page and directory rows

## Simplification Notes

Avoid carrying both old and new hot-path shapes long-term.

Good simplifications:

- API should ask for directory response dicts directly.
- `DirectoryMatch` can remain only if non-API tests/callers still need it.
- search should not build full ranked candidate objects for normal API
  responses.
- directory counts should use numeric directory ids and parent rollup.
- profile state should compile to arrays: inclusion mask, adjustment array, and
  included directory counts.
- `/search-with-directory-counts` should use one numeric call that shares
  the query match arrays between page ranking and directory counts.

Possible next cleanup:

- remove or narrow old object-based directory APIs once tests are moved to
  public API behavior.
