# Findme V2 Project Tracker

This tracker is a working implementation outline. Product and system decisions belong in
[`docs/DESIGN.md`](../docs/DESIGN.md), technology choices in
[`docs/STACK.md`](../docs/STACK.md), and search-specific design in
[`docs/SEARCH.md`](../docs/SEARCH.md).

Keep this document at the level of major implementation blocks. Expand a section only when
work on that section starts.

## Current Focus

- [ ] Finish the v1 indexed asset finder loop: index, search/browse, preview, copy/open/download.
- [ ] Tune search quality against real indexed data.
- [ ] Keep source directories read-only and all generated state under the app data directory.

References: [Design: Product Intent](../docs/DESIGN.md#product-intent), [Design: Product Surface](../docs/DESIGN.md#product-surface), [Stack: Repository And Deployment Shape](../docs/STACK.md#repository-and-deployment-shape)

## Foundations And Configuration

- [x] Monorepo shape with backend, frontend, docs, config, and sample assets.
- [x] Python backend package and CLI entry point.
- [x] Svelte/Vite frontend app.
- [x] TOML app/source configuration.
- [x] Server-owned data root configuration.
- [x] Source definitions with id, display name, root, priority, media extensions, and output profiles.
- [ ] Final production configuration review.
- [ ] Document any production deployment assumptions that are not already captured.

References: [Design: Sources And Configuration](../docs/DESIGN.md#sources-and-configuration), [Stack: Configuration](../docs/STACK.md#configuration), [Stack: Deployment](../docs/STACK.md#deployment)

## Storage And Identity

- [x] SQLite database initialization.
- [x] WAL mode.
- [x] Source and asset tables.
- [x] FTS search table.
- [x] Canonical source id plus relative path identity.
- [x] Short asset id generation.
- [x] Stale flag on assets.
- [ ] Migration policy for future schema changes.
- [ ] Cleanup command for stale records and unreferenced cache files.

References: [Design: Core Model](../docs/DESIGN.md#core-model), [Design: Identity](../docs/DESIGN.md#identity), [Design: Storage Model](../docs/DESIGN.md#storage-model), [Stack: Database](../docs/STACK.md#database)

## Indexing And Media Pipeline

- [x] Manual CLI indexing.
- [x] Incremental file fingerprint checks using size and mtime.
- [x] Missing files marked stale.
- [x] Legacy `.findme` directories ignored.
- [x] Static image thumbnail generation.
- [x] Thumbnail cache stored outside source directories.
- [x] Bounded worker pool for thumbnail generation.
- [x] Indexing progress output.
- [x] Clear thumbnail failure reporting.
- [x] Video poster generation with `ffmpeg`.
- [ ] Index status and errors surfaced through the API/UI.
- [ ] Benchmark indexing against realistic source data.

References: [Design: Indexing Model](../docs/DESIGN.md#indexing-model), [Design: Media Scope](../docs/DESIGN.md#media-scope), [Design: Indexing Controls](../docs/DESIGN.md#indexing-controls), [Stack: Indexing And Media Pipeline](../docs/STACK.md#indexing-and-media-pipeline)

## Derived Actions And Asset Access

- [x] Asset detail API.
- [x] Open original asset endpoint.
- [x] Download original asset endpoint.
- [x] Thumbnail endpoint.
- [x] Source-owned output profiles.
- [x] Primary derived path selection.
- [x] Asset path safety validation.
- [ ] Secondary actions polished in the UI.
- [ ] Confirm final v1 output profiles for the real asset sources.

References: [Design: Derived Resources And Actions](../docs/DESIGN.md#derived-resources-and-actions), [Design: Sources And Configuration](../docs/DESIGN.md#sources-and-configuration), [Stack: API Contract](../docs/STACK.md#api-contract)

## Search And Ranking

- [x] Indexed-only search path.
- [x] Basic SQLite FTS-backed lexical search.
- [x] Process-local in-memory search corpus loaded from SQLite at app startup.
- [x] Running app may require restart after indexing before new search data is visible.
- [x] Source filter.
- [x] Directory and directory-prefix filters.
- [x] Stale assets hidden from normal search.
- [x] Source priority included in current scoring.
- [x] User-defined source/directory quality included in current scoring.
- [x] Bounded, process-local result cache for repeated exact queries.
- [ ] Near-repeat search cache reuse, e.g. `Goblins` then `Goblin`, with a max memory size.
- [ ] Real crawl and search engine comparison.
- [ ] Final lexical search engine choice.
- [ ] Domain vocabulary file.
- [ ] Domain expansion lane.
- [ ] Candidate interface between retrieval lanes and final ranking.
- [ ] Centralized final ranker.
- [ ] Ranking diagnostics/workbench.
- [ ] Lightweight search evaluation set.

### Ranking Inputs

- [x] Text match score.
- [x] Source priority.
- [x] User-defined source/directory quality.
- [x] Source/directory filter scope.
- [ ] Exact token/stem match signals.
- [ ] Prefix/infix/fuzzy match signals.
- [ ] Domain expansion signals.
- [ ] Media type signals.
- [ ] Recency or other metadata signals if useful.

References: [Design: Search And Ranking](../docs/DESIGN.md#search-and-ranking), [Design: Performance Goals](../docs/DESIGN.md#performance-goals), [Search: Retrieval Lanes](../docs/SEARCH.md#retrieval-lanes), [Search: Ranking](../docs/SEARCH.md#ranking), [Stack: Search Engine](../docs/STACK.md#search-engine)

## Browsing And Source Tree

- [x] Asset listing API for indexed assets.
- [x] Directory tree API.
- [x] Directory asset counts.
- [x] Matched directory counts for active searches.
- [x] Source filtering.
- [x] Directory filtering.
- [x] Folder folding controls.
- [x] Preserve folder selection across search changes.
- [ ] Confirm empty-query browse behavior against real usage.
- [x] Range-based result loading beyond the initial slice.

References: [Design: Result Loading](../docs/DESIGN.md#result-loading), [Design: Source Tree And Browsing](../docs/DESIGN.md#source-tree-and-browsing)

## Frontend Experience

- [x] Search input with debounce.
- [ ] Heavier debounce in search.
- [x] Stale response ignoring through request ids.
- [x] Thumbnail result grid.
- [x] Selected asset preview panel.
- [x] Hover preview.
- [x] Image and video preview handling.
- [x] Copy primary path.
- [x] Open and download actions.
- [x] Arrow-key result navigation.
- [x] Enter copies the selected primary path.
- [x] Escape clears selection.
- [x] `/` focuses search.
- [x] Vim-style movement where useful.
- [ ] Virtualized result grid.
- [ ] Secondary action shortcuts.
- [x] Middle-click opens image in a new tab.
- [ ] UI polish pass after real asset data is indexed.

References: [Design: Preview Experience](../docs/DESIGN.md#preview-experience), [Design: Layout](../docs/DESIGN.md#layout), [Design: Keyboard Interaction](../docs/DESIGN.md#keyboard-interaction), [Stack: Frontend](../docs/STACK.md#frontend)

## API And Server

- [x] FastAPI app.
- [x] Health endpoint.
- [x] Sources endpoint.
- [x] Assets endpoint.
- [x] Directories endpoint.
- [x] Search endpoint.
- [x] Asset detail/file/thumbnail endpoints.
- [x] Static frontend serving from the backend.
- [ ] Cache headers for thumbnails/posters.
- [ ] API shape review before frontend surface grows further.

References: [Stack: Backend](../docs/STACK.md#backend), [Stack: API Contract](../docs/STACK.md#api-contract), [Design: Performance Goals](../docs/DESIGN.md#performance-goals)

## Testing And Quality

- [x] Backend pytest coverage for core implemented behavior.
- [x] API tests.
- [x] Indexing tests.
- [x] Database tests.
- [x] Config tests.
- [x] Search tests for current behavior.
- [x] Thumbnail tests.
- [ ] Frontend tests where useful.
- [ ] Search evaluation set after real crawl data exists.
- [ ] Focused UI smoke/performance checks when virtualization lands.

References: [Design: Testing](../docs/DESIGN.md#testing), [Search: Evaluation Set](../docs/SEARCH.md#evaluation-set), [Stack: Testing](../docs/STACK.md#testing)

## Operations And Deployment

- [x] Development config.
- [x] Example config.
- [x] Server config placeholder.
- [x] CLI serve command.
- [ ] Production systemd service setup.
- [ ] Production app data root setup at `/var/lib/findme-v2`.
- [ ] Production indexing workflow.
- [ ] Logging level review for deployment.
- [ ] Optional search-engine service setup if selected.

References: [Design: Storage Model](../docs/DESIGN.md#storage-model), [Design: Logging](../docs/DESIGN.md#logging), [Stack: Deployment](../docs/STACK.md#deployment)

## Deferred Or Future Goals

- [ ] FoundryVTT integration.
- [ ] AI/embedding search lane.
- [ ] Content-hash deduplication.
- [ ] Move detection.
- [ ] PDF indexing and preview.
- [ ] Richer VTT-specific action profiles.
- [ ] Optional ranking/debug explanations beyond the v1 workbench needs.

References: [Design: Future Goals](../docs/DESIGN.md#future-goals), [Design: Non-Goals For V1](../docs/DESIGN.md#non-goals-for-v1), [Search: AI Search](../docs/SEARCH.md#ai-search)
