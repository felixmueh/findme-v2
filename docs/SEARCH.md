# Search Design

This document captures the current search and ranking decisions for `findme-v2`.

Search quality is a core product requirement. The goal is not merely to match filenames, but to find the asset the user means while staying fast enough for interactive browsing.

## Core Requirements

V1 search must support:

- fast indexed-only search
- strong filename/path matching
- domain-aware expansion such as `adventurer` finding `warrior`, `mage`, `rogue`, etc.
- source/directory filters
- centralized hand-tunable ranking
- search diagnostics for development/tuning
- a lightweight search evaluation set

The normal UI/API should stay fast and lean. Detailed ranking diagnostics belong in a workbench/debug path, not normal search responses.

## Retrieval Lanes

Search is designed as multiple retrieval lanes plus a final ranker.

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

4. **Final ranking/fusion**
   - combines candidates from all lanes
   - implementation formula is intentionally not finalized
   - must be localized in one easy-to-edit function/module

Current implementation status:

- `strict`: token-accurate filename/path matching. The strict benchmark scope requires every query token to match.
- `fuzzy`: typo/separator-tolerant matching using RapidFuzz over indexed filename/path data.
- `domain`: query-time vocabulary expansion from `config/search-vocabulary.yaml` or the configured `[search].domain_vocabulary` path.
- `combined`: normal product search, combining strict, fuzzy, and domain candidates through the centralized ranker in `findme.search`.

SQLite is durable storage for indexed assets, not the interactive search path. The
running app loads the indexed rows into a process-local in-memory search
representation at startup. Interactive search, ranking, and matched directory
counts operate on that representation. After running the indexer, restart the app
before expecting the served search UI/API to reflect newly indexed data.

Do not add lazy or partial index loading for v1. If the real data set becomes too
large to keep in memory, revisit this design explicitly.

Domain vocabulary changes are applied at query time and do not require re-indexing.

## Runtime Performance

The API keeps a bounded process-local LRU cache of fully ranked results for exact
query/scope/mode/vocabulary combinations. Asset results and matched directory
counts are both projected from the same ranked result, so a single query should
not rank the corpus twice just because the frontend asks for both assets and
directories.

Concurrent identical searches are coalesced in process: the first request owns
the ranking work, and later identical requests wait for the same result unless
they are superseded. This is an optimization only; SQLite remains the durable
source and the in-memory cache can be discarded at any time.

The frontend sends monotonically increasing search revisions per browser session.
When a newer request supersedes an older one, backend ranking loops check
cooperatively and return `204 No Content` for obsolete work. Do not forcibly kill
threads for search cancellation; keep cancellation checks in long-running search
loops and make new retrieval lanes participate in the same control mechanism.

Near-repeat cache reuse, such as using a cached plural query when the user edits
back to the singular form, is intentionally deferred. The current cache is exact
key reuse with a bounded memory footprint.

## Candidate Interface

Retrieval lanes should return normalized candidate data.

Conceptually, a candidate includes:

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
- source priority is applied in the final ranker, not inside retrieval lanes
- selected source/directory scope is a hard filter before or during retrieval
- stale assets are hidden from normal search
- diagnostics expose enough signals to tune ranking

A likely starting point is rank fusion plus explicit boosts/penalties, but this remains an implementation detail. Raw scores from different lanes should not be assumed comparable.

Potential ranking inputs:

- lane ranks
- raw lane scores
- exact token/stem matches
- prefix/infix/fuzzy match signals
- domain-expanded match signals
- source priority
- media type
- recency or other file metadata if useful
- future semantic similarity

## Workbench

A search/ranking workbench is required for v1 development.

It does not need to be polished user-facing UI. CLI or dev UI is fine.

It should allow:

- running a query against indexed data
- seeing final ranked results
- seeing per-lane candidates, ranks, and raw scores
- seeing domain expansions used
- seeing source priority and final ranking inputs
- inspecting why good/bad results appeared

The workbench exists because ranking quality will be tuned against real crawl data.

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
