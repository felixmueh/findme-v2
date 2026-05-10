# Search Design

This document captures the current search and ranking decisions for `findme-v2`.

Search quality is a core product requirement. The goal is not merely to match filenames, but to find the asset the user means while staying fast enough for interactive browsing.

## Core Requirements

V1 search must support:

- fast indexed-only search
- strong filename/path matching
- domain-aware expansion such as `adventurer` finding `warrior`, `mage`, `rogue`, etc.
- source/directory filters
- hand-tunable ranking per lane where the engine defaults are not sufficient
- search diagnostics for development/tuning
- a lightweight search evaluation set

The normal UI/API should stay fast and lean. Detailed ranking diagnostics belong in a workbench/debug path, not normal search responses.

## Retrieval Lanes

Search is designed as multiple retrieval lanes.

Each lane answers: "Which assets match this query by this method?"

Planned lanes:

1. **Lexical lane**
   - filename/stem/path-token search
   - backed by an off-the-shelf search engine
   - must support high-quality fuzzy/prefix/infix-style matching through existing tooling
   - no hand-rolled fuzzy/prefix/infix implementation

2. **Domain expansion lane**
   - expands user terms through a hand-editable domain vocabulary
   - critical for broad concept searches
   - external to the lexical search engine in v1 so expansion signals remain visible and tunable

3. **Semantic/AI lane**
   - future lane for image/text embeddings, likely CLIP-style or similar
   - not required for v1
   - architecture should allow adding it later without rewriting search/ranking

The API and frontend request and display individual lanes directly. The UI may expose this as tabs,
parallel columns, or an aggregated result stream that is grouped horizontally or
sequentially by lane. A plausible default result shape is:

- exact/token lexical matches first
- fuzzy typo-tolerant matches next
- domain-expanded matches next

Keeping lanes visibly separate is conceptually simpler than forcing every signal
into one global score. It also allows fast exact results to appear immediately
without waiting for broader or slower lanes.

Current implementation status:

- `strict`: token-accurate filename/stem/path matching backed by Tantivy. The strict benchmark scope requires every query token to match, and normal strict UI/API results use Tantivy ranking plus profile preferences.
- `fuzzy`: typo/separator-tolerant matching using RapidFuzz over indexed filename/path data.
- `domain`: query-time vocabulary expansion from `config/search-vocabulary.yaml` or the configured `[search].domain_vocabulary` path.

SQLite is durable storage for indexed assets, not the interactive search path. The
running app loads the indexed rows into a process-local in-memory search
representation at startup. Interactive search, ranking, and matched directory
counts operate on that representation. After running the indexer, restart the app
before expecting the served search UI/API to reflect newly indexed data.

Do not add lazy or partial index loading for v1. If the real data set becomes too
large to keep in memory, revisit this design explicitly.

Domain vocabulary changes are applied at query time and do not require re-indexing.

## Runtime Performance

Search-serving performance is built around a single numeric hot path. The data
needed for interactive search is simple: asset indexes, lane scores, profile
inclusion, folder quality adjustments, asset directory ids, and directory parent
ids. Hot-path code should keep that data in arrays and avoid creating per-match
Python objects.

The normal serving shape is:

```text
retrieval lane -> asset_indexes + raw_scores arrays
profile mask -> filter included assets
importance array -> add score adjustment
top-k page -> only materialize returned assets
directory ids -> bincount + parent rollup
API directory rows -> build response dicts directly
```

Strict, fuzzy, and domain search all feed this same shape. Their semantic
difference is retrieval, not separate ranking/paging/directory-count machinery.

Keep the code simple by removing old hot-path contracts instead of preserving
parallel implementations. Normal API search should not build full ranked
candidate objects, should not materialize the whole corpus just to return one
page, and should not convert through intermediate directory match objects before
building JSON rows. Result-card paging should use asset indexes directly.

Directory counts should use numeric directory ids and parent rollup. For scoped
browse/view requests, derive the scoped asset-index array first, then use the
same count-array response path as unscoped browse/search. Do not keep a second
dict/string-tuple aggregation path unless a concrete new requirement makes the
numeric path unsuitable.

The remaining broad-query cost is expected to be dominated by retrieval,
building thousands of directory response dictionaries, JSON serialization, and
network transfer. Optimize those only when it also simplifies the main path or
when measurements show they are the bottleneck for an actual workflow.

Concurrent identical strict searches are coalesced in process: the first request
owns the retrieval work, and later identical requests wait for the same result
unless they are superseded. This is an optimization only; SQLite remains the
durable source and the in-memory cache can be discarded at any time.

The frontend sends monotonically increasing search revisions per browser session.
When a newer request supersedes an older one, backend ranking loops check
cooperatively and return `204 No Content` for obsolete work. Do not forcibly kill
threads for search cancellation; keep cancellation checks in long-running search
loops and make new retrieval lanes participate in the same control mechanism.

Near-repeat cache reuse, such as using a cached plural query when the user edits
back to the singular form, is intentionally deferred. The current cache is exact
key reuse with a bounded memory footprint.

Open direction: search does not necessarily need every retrieval lane to finish
before the UI can show useful results. Exact lexical matches may be returned
first for the first result page, with fuzzy typo-tolerant matches and
domain-expanded matches fetched later as enrichment, when the user scrolls far
enough, when a lane tab becomes active, or when the user explicitly switches or
expands the mode. This keeps the lane model intact while allowing the
fastest/highest-precision evidence to drive initial interaction latency.

## Candidate Interface

Normal retrieval lanes should return numeric candidate arrays:

- asset indexes
- lane-native scores

Workbench/debug paths may enrich those candidates with diagnostic metadata.

Conceptually, diagnostic candidate metadata can include:

- asset id
- lane name
- lane rank
- raw lane score when available
- matched terms/fields when available
- query variant or expansion information when relevant
- other tuning signals useful to the workbench

The final ranker should not need to know engine-specific query mechanics.

## Lexical Search Engine

The lexical lane should use an existing search engine rather than custom matching logic.

Candidate engines to compare after crawling real data:

- Typesense
- Meilisearch
- SQLite FTS5/trigram as a simpler embedded baseline
- other candidates if they look clearly useful

Evaluation should use real indexed asset data and representative queries. Engine choice is deferred until after the crawl and comparison.

Important lexical behavior:

- exact filename/stem/token matches should be strong
- prefix and infix/substring-style matches should be supported by the engine/tooling
- typo tolerance should come from the engine/tooling
- path and directory tokens can help recall but should not automatically dominate filename/stem matches

## Domain Vocabulary

Domain expansion is a v1 requirement.

The vocabulary is:

- global for v1
- hand-editable
- server-side
- reloadable without re-indexing
- tuned against real crawl results

The default editable vocabulary lives at:

```text
config/search-vocabulary.yaml
```

Server configs may point at a different file with:

```toml
[search]
domain_vocabulary = "search-vocabulary.yaml"
```

Relationship types to support conceptually:

- **Aliases**: equivalent names or normalized variants.
- **Expansions / related terms**: a query broadens to related terms.
- **Parents / implications**: child terms imply broader parent concepts.

Example:

```text
adventurer => warrior, mage, rogue, ranger, cleric
warrior -> adventurer
mage -> adventurer
```

Domain expansion should remain outside the lexical search engine in v1. Expanded terms may be searched through the lexical engine, but candidates must retain signals that they came from expansion.

Changing vocabulary should not require re-indexing because it affects query-time behavior.

## Query Behavior

V1 uses simple natural search, not advanced query syntax.

Default behavior should be soft-ranked rather than strict Boolean filtering:

- multi-word queries should strongly prefer assets matching more query terms
- exact and all-token matches should rank highly
- partial, fuzzy, infix, and domain-expanded matches can still appear lower
- tree/source selection is a hard filter

Advanced query syntax, explicit Boolean operators, and quoted phrase behavior are deferred until real usage shows they are needed.

## Ranking

Final ranking is intentionally hand-tunable.

Hard requirements:

- final ranking logic is centralized and localized
- changing ranking should not require touching indexing, API, UI, or lane implementations
- user-defined source/directory quality is applied as a final ranking
  adjustment, not inside retrieval lanes
- selected source/directory scope is a hard filter before or during retrieval
- stale assets are hidden from normal search
- diagnostics expose enough signals to tune ranking

Raw scores from different lanes should not be assumed comparable.

Potential ranking inputs:

- lane ranks
- raw lane scores
- exact token/stem matches
- prefix/infix/fuzzy match signals
- domain-expanded match signals
- user-defined source/directory quality
- recency or other file metadata if useful
- future semantic similarity

Strict lexical search still uses Tantivy for retrieval, but profile preferences
are applied after retrieval:

```text
strict candidates + Tantivy score -> preference adjustment -> sorted results
```

Do not use the full Python multi-lane ranker as a fallback for strict search.
If the strict lexical index is unavailable, fail clearly rather than silently
switching to different ranking semantics.

## Workbench

A search/ranking workbench is required for v1 development.

It does not need to be polished user-facing UI. CLI or dev UI is fine.

It should allow:

- running a query against indexed data
- seeing final ranked results
- seeing per-lane candidates, ranks, and raw scores
- seeing domain expansions used
- seeing profile preference and final ranking inputs
- inspecting why good/bad results appeared

The workbench exists because ranking quality will be tuned against real crawl data.

## Measured Baseline

The real DND corpus used during the numeric refactor had about 258k assets and
5.5k directory rows. A broad strict `map` query matched more than 200k assets.

After the numeric refactor, representative warm timings were:

- empty profile compile: under 1 ms
- large whitelist profile compile: about 8-11 ms
- browse directory response for all 5.5k rows: about 12-13 ms internally
- strict `map` page plus directory rows: about 20-30 ms internally
- typical strict page plus directory rows: about 6-10 ms internally
- fuzzy page plus directory rows: about 18-20 ms for typo-style queries
- domain `adventurer` page plus directory rows: about 155-225 ms

The 8-11 ms large-whitelist compile cost is acceptable. Do not add caches,
preprocessing, or compatibility scaffolding just to chase it unless the change
also simplifies the runtime model.

## Evaluation Set

Search evaluation is specified in `docs/EVALUATE_SEARCH.md`.

The evaluation set is not a heavy ML benchmark. It is a local, real-DND regression and tuning aid for search behavior, vocabulary, and ranking.

## AI Search

AI/semantic search is planned but not required for v1.

Likely future direction:

- generate image embeddings during indexing/preprocessing
- store embedding metadata in the app index/database or adjacent vector store
- add a semantic retrieval lane
- combine semantic candidates through the same final ranker/workbench path

Do not make v1 search depend on embeddings.
