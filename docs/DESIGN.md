# Findme V2 Design

This document captures the design goals and decisions for `findme-v2`. It is intentionally about product shape and system design, not implementation details.

This document is the source of truth for product and system design decisions. When decisions change, update this file.

Technology choices are tracked in `docs/STACK.md`. Search-specific design is tracked in `docs/SEARCH.md`.

## Product Intent

`findme-v2` is a personal asset finder hosted on the same server where the asset sources live. It replaces the original model of a local desktop app reading assets over a network share.

The primary purpose remains:

- find visual/tabletop assets quickly
- copy a usable path to the clipboard
- copy a transformed path for virtual tabletop software
- download or open the original asset when needed

This is personal-use software. It should be optimized for this server, this asset layout, and this workflow. General product configurability is not a goal when a simple server-side config or code edit is enough.

The implementation should stay small, maintainable, and tested. It should not carry fallback mechanisms for unknown environments. If this server is missing a required tool, directory, or dependency, the app should fail with a clear actionable error rather than silently degrading into a weaker generic path.

Performance work should simplify the actual data path. When a faster approach
introduces parallel contracts, compatibility layers, or speculative caches,
prefer reshaping callers around the simpler path unless the extra structure is
measured and still needed.

## Product Surface

The v1 product surface is a standalone browser web app served by the asset server.

FoundryVTT integration is a future goal. It should build on the same server API later, similar in spirit to tools like Moulinette FilePicker, but v1 should not be Foundry-first.

## Access Model

V1 is private/trusted-user only.

Assumptions:

- LAN/VPN/private access
- no public internet exposure
- no user accounts in v1
- no source-level permissions in v1

If the app is later exposed publicly or shared with players, authentication and authorization need a separate design pass.

## Core Model

The app indexes files plus source/directory structure as first-class data.

Each indexed asset should know:

- source id
- source display name
- source priority
- source-relative path
- absolute server path
- filename, stem, extension
- directory path segments
- normalized searchable tokens
- file size
- modified time
- image/video dimensions when available
- video/audio duration when available
- media type
- thumbnail/poster cache references
- current index membership

The transformed VTT path is not indexed. It is derived at request/display/copy time from the asset identity plus current source configuration. Changing path transform rules should not require re-indexing.

## Identity

The canonical identity fields are:

- `source_id`
- `relative_path`

V1 should also derive a short, URL-safe `asset_id` from those fields for API handles, thumbnail URLs, details requests, and client state.

The `asset_id` must be collision-resistant. It is an ergonomic handle, not the source of truth. No content hashing is required for v1.

Content-hash deduplication and move detection are future features.

User-authored metadata needs a stronger identity than the path-derived
`asset_id`. Tagged files should use a Hydrus-style content identity: the first
time a file receives durable user metadata, the app hashes that file and records
metadata against the content hash, not against the current indexed path. Normal
indexing should not hash every asset just to make this possible. Hashing is
lazy and metadata-driven for v1.

The path-derived `asset_id` remains the normal API, thumbnail, and client-state
handle for current indexed assets. Content hashes are durable metadata identity,
not a replacement for current asset handles.

## Sources And Configuration

Configuration is server-owned and personal. It can live in a config file checked into the repo or in server-side files. It does not need a UI editor.

Each source should conceptually define:

- stable source id
- display name
- filesystem root
- priority
- enabled media types
- indexing/thumbnail settings
- derived actions and output profiles

Path transformations should be source-owned with named output profiles. A global default can exist, but source-specific rules should win.

Example actions include:

- open original in a new tab
- open the containing source directory or subdirectory in the configured File Browser instance
- copy the original image to the clipboard when the browser supports it
- copy the generated thumbnail to the clipboard
- download the original file
- copy the absolute filesystem path
- copy the FoundryVTT path

## Derived Resources And Actions

Path transformation is a special case of a broader concept: derived resources/actions.

An action can produce:

- a clipboard string
- a transformed VTT path
- a download URL
- an open/preview URL
- future Foundry-specific payloads

Derived actions are computed from the indexed asset plus current source configuration. They should not require re-indexing when changed.

Folder File Browser links are also source-owned configuration. A source can
provide a `file_browser_url_prefix`, and directory rows derive their external
URL from that prefix plus the source-relative directory path. Changing that
prefix should not require re-indexing.

FoundryVTT paths are configured per source using output profiles. On the server, the Foundry `Data/library` directory contains symlinks into `/media/NAS/main/Documents/DND`, so copied Foundry paths are relative to `Data` and start with `library/...`.

V1 interaction model:

- click selects the asset
- Enter copies the configured FoundryVTT path for the selected asset
- secondary actions live in a compact per-result menu or details panel
- v1 focuses on single-asset actions, not bulk actions

Source narrowing is handled through the folder tree. Each source appears as a top-level folder row, and focusing that row applies the same whitelist filtering model as any other folder. The toolbar should not carry a separate source selector.

## Indexing Model

Search and browsing read only indexed data. There is no slow search path for v1.

Indexing is manual for v1. Live filesystem watching is deferred.

Manual indexing should still be incremental:

- compare current filesystem entries against stored fingerprints
- avoid regenerating expensive metadata/thumbnails when size and mtime are unchanged
- detect missing files
- add newly discovered files

The app must never write index files into source directories. Unlike original `findme`, v2 keeps all index state and generated cache files under the app/server data directory.

Legacy `.findme` directories and index files from previous versions are source artifacts only. V2 should ignore them and should not import, update, delete, or depend on them.

Legacy JSON metadata dictionaries from previous versions are out of scope for v1. V2 should ignore them; they may be removed from the source tree later.

## Index Replacement And Cleanup Behavior

The database should store the current index, not current rows plus stale rows.

A full index replaces the index-owned rows for all configured sources. A
source-limited index replaces only the selected source rows. Search and browse
correctness must not depend on a later cleanup command.

Thumbnail/poster cleanup is separate maintenance. It deletes generated thumbnail
files that are not referenced by current database records. Indexing may run this
maintenance step after a successful index when requested explicitly.

## Storage Model

The server owns a local persistent index database under the app data directory. The production app data root is expected to be:

```text
/var/lib/findme-v2
```

Development may override this path.

The index database is canonical for current indexed metadata:

- assets
- sources
- directory tree and counts
- token/search metadata
- file fingerprint state
- current index membership
- generated thumbnail/poster references
- future embeddings
- future content hash/dedup records

Generated thumbnails and poster frames are binary cache files, not database blobs. They should be stored under the app cache directory and referenced by DB records.

Thumbnail/poster cache identity should use a cheap file fingerprint such as:

```text
source_id + relative_path + size + mtime
```

This avoids content hashing in v1 while still invalidating derived files when an asset changes.

Durable user-authored metadata, including tags, should live in a separate
metadata database under the app data root rather than in the index database. The
index database and search corpus are rebuild-sensitive views of the current
filesystem. The metadata database is not an indexing artifact and should remain
valid across index rebuilds and missing indexed files.

The metadata database should store:

- content identities for files that have user-authored metadata
- known current and historical source-relative locations for those files
- tag definitions
- tag assignments keyed by content identity

The initial tagging path should hash only files that are actually tagged. V1
should not discover moved tagged files, either during normal indexing or through
a separate reconciliation command. If a tagged file disappears from its recorded
location, keep its content identity and tags in the metadata database but mark
the known location missing. Move discovery and broader file migration need a
separate design pass later.

### Thumbnail Variant Transition

The normalized `asset_thumbnail_variants` table is canonical for generated
thumbnail/poster mipmap availability. It records the available derivative sizes,
dimensions, cache paths, and source fingerprint for each asset.

The search corpus, API, and frontend consume thumbnail variants directly for
result cards. The existing `assets.thumbnail_path`, `assets.thumbnail_width`,
and `assets.thumbnail_height` fields intentionally remain as denormalized
baseline `256` fields during the transition. Revisit whether those baseline
fields should be removed or derived from `asset_thumbnail_variants`.

Search-serving data is a derived corpus, not canonical metadata. The main database remains the source of truth for indexed assets, sources, folder quality, and thumbnail references. `search_corpus.py` owns the rebuild-sensitive corpus format and preprocessing signals. It builds a separate versioned pickle snapshot under the app data root, with the active filename derived from the preprocessing version, plus the matching Tantivy lexical index. Serving and search evaluation consume that prepared corpus and fail clearly if it is missing or stale; they do not rebuild it implicitly.

Default indexing is optimized for the common "files were added or removed"
case. During the first phase it scans source-relative paths, preserves existing
database rows for paths that are still present, stats only newly discovered
files, deletes rows for missing paths, and schedules thumbnails/posters only
for new files or explicit retry/rebuild requests. It does not verify size/mtime
for every existing path on every run; a full verification mode can be added if
in-place source-file mutation becomes an important workflow.

Indexing runs in three phases:

- scan source directories and update the canonical database, preserving existing metadata and thumbnail references for paths that are still present
- generate missing thumbnails/posters and update matching database rows as each generated file completes
- search preprocessing snapshots current assets into the versioned search corpus, builds the lexical index used by search, and precomputes result-card grouping metadata

The search corpus is disposable and may be rebuilt from the canonical database
with the explicit `rebuild-search-corpus` command. The database records an
index fingerprint and the fingerprint captured by the current search corpus, so
the backend can detect when the corpus is stale. Folder quality overrides stay
in the main database and are applied at search time so UI quality changes do not
require corpus rebuilds.

## Media Scope

V1 should support common image and VTT media assets:

- `.png`
- `.jpg`
- `.jpeg`
- `.webp`
- `.gif`
- `.webm`
- `.mp4`
- `.mp3`
- `.ogg`
- `.wav`
- `.aif`
- `.aiff`
- `.m4a`
- `.flac`

Static images should get generated thumbnails.

Animated/video assets should get representative poster thumbnails. Playback/preview can happen only when explicitly opened or selected.

Audio assets should have duration indexed when `ffprobe` can read it. They do
not need generated thumbnails.

PDF support is a future goal. The media pipeline should remain extensible and not hard-code assumptions that every asset is a raster image.

External tools may be required for media processing. For example, video poster generation can require `ffmpeg`, and future PDF extraction can require a PDF-specific tool. Missing tools should produce clear errors; v1 should not implement broad fallback behavior for incomplete environments.

## Search And Ranking

Search internals are tracked in `docs/SEARCH.md`.

Settled design constraints:

- search reads indexed data only
- no live filesystem walking during search
- the hot path is numeric: retrieval lanes return asset indexes and scores,
  profile preferences are arrays, paging materializes only returned assets, and
  directory counts roll up numeric directory ids
- search lanes are directly requestable user-facing modes
- each lane may use its engine's own ranking when that is faster and good enough
- selected source/directory filters should be hard filters
- result cards and matched directory counts should come from the same search scope; clients should use a search-with-directory-counts response when both are needed
- ranking explainability is optional; ranking quality and tunability matter more

Initial search lanes should include strict lexical evidence, fuzzy lexical evidence, and domain-expanded evidence. Future semantic/image-text evidence should fit the same lane model. Strict and fuzzy lexical search may share implementation machinery, but they should remain separate logical lanes because they have different precision, recall, and ranking weight expectations.

Potential ranking inputs:

- text match score
- user-defined source/directory quality
- selected source/directory filters
- media type
- recency
- file/path signals
- future embedding similarity

The exact search and ranking model will be discussed separately.

## Performance Goals

Initial page load is less important than interaction speed.

Important performance goals:

- typing must never be blocked by search
- keystroke-to-updated-results should be as fast as practical
- thumbnail display should be extremely fast
- scrolling through large thumbnail result sets should be extremely fast
- search should start quickly, but old searches can be aborted or superseded

Search interaction model:

- client debounce, default around 150 ms and configurable
- each search has a query id or cancellation mechanism
- stale responses must be ignored by the client
- server-side cancellation is useful where practical
- results return lightweight metadata first
- thumbnails/posters are served separately from precomputed cache files
- browser/cache headers should make thumbnails cheap to revisit
- result grids must use virtualization so the DOM only contains visible and near-visible items

## Result Loading

Search results should be range-based over a ranked result set.

The client can request slices such as:

```text
offset=0&limit=100
offset=100&limit=100
```

This fits virtualized grids and huge thumbnail sets better than traditional page navigation.

Result ranges are over visible result cards, not necessarily raw assets. Similar
assets may fold into a single collection card. For v1, collections are detected
within the same source and directory by stripping recognized variant tokens from
filename stems. Variants include numeric tokens such as `1`, `5b`, and `7C`,
short letter tokens such as `A`, `B`, and `BC`, common color names such as
`Blue`, `green`, and `MAGENTA`, and letter-number tokens such as `A2`. Variant
tokens may appear at the end or in delimited middle positions, and more than one
variant token may appear in a stem. Accidental embedded image extensions in
double-extension names are ignored before grouping, so names like
`Map_01.png.jpg` fold with adjacent map variants.

Examples include `Goblin2.png`, `Goblin 03.webp`, `Goblin (4).png`,
`Goblin [5].jpg`, `Portal_Blue_Glow.png`, and
`Human_Male_Blacksmith_A2_Spirit_02.png.jpg`.

Collections are computed from indexed metadata when the server loads the
in-memory search data, not persisted as indexer output. This keeps the rule easy
to replace later with server-side domain rules without rewriting source
directories or requiring a re-index for rule changes.

Search should group after ranking and before applying API range slicing so a
collection cannot be split across pages. A collection result includes all known
variants in that source/directory collection, even when only one variant matched
the query. The representative is the highest-scored matching variant for search
and the first naturally sorted variant for browsing.

## Source Tree And Browsing

The source/directory tree is first-class UI, not decoration.

It should:

- show sources ordered by configured priority/name
- show expandable indexed directories
- show asset counts per source/directory
- act as a hard filter for search and browse through active search profile include/exclude subtree rules
- never perform live filesystem walking from the UI
- expose a persistent user-defined quality label from 0 to 10 for each indexed
  source/directory

Directory quality and selection are user-edited search profile data and live in
the app database for persisted profiles, not in source folders or server config.
Quality is a ranking signal. Selection is a hard filter. Unsaved profile edits
live in backend memory per browser search session.

The active sidebar profile model is tracked in `SEARCH_PROFILES_DESIGN.md`.
Source roots and directories can be marked as green whitelist or red blacklist
rules. Overlapping whitelist rules are allowed, but only the bottom-most
whitelist rules are effective; ancestor whitelists with descendant whitelists
remain visible as superseded settings. The nearest specified effective ancestor
decides whether an asset is included, and the existence of any effective
whitelist rule globally excludes otherwise-unspecified assets. This rule model
replaces a single selected-directory hard filter.

The compact folder control should indicate when that folder's subtree contains
an unsaved difference from the currently saved active profile. This is a dirty
working-state cue, not a cue for persisted settings that already match the
profile. The same compact control also indicates when explicit focus or block
rules exist below the folder, separate from the folder's own direct rule state.

A row click may apply a transient directory view scope for browsing within a
folder without changing the active search profile. That view scope is not a
second persistent filter model: the backend compiles it into the same numeric
selection data used by profile rules, while preserving the user-authored
profile rule states for display. Expanding/collapsing the tree is a separate
row control. Global empty-query behavior can be conservative, such as showing
nothing, recent assets, or preferred assets.

## Preview Experience

V1 should have a lightweight details/preview panel, not a full editor.

The panel can show:

- larger preview
- source
- path/breadcrumbs
- basic metadata
- available derived actions

The original `findme` tint/border image editor is deferred unless it becomes actively needed again.

For animated/video assets, the UI can show a poster thumbnail first and only play/open the original when explicitly requested.

V1 precomputes named derivatives rather than arbitrary sizes. Exact sizes are implementation decisions; for example, v1 might start with a single thumbnail size named by its actual dimensions, such as `thumb_512`, plus a separate larger preview derivative. The derivative pipeline should stay open to additional named sizes later.

## Tagging Experience

Tags are durable user-authored metadata. V1 tagging should focus on applying
and removing tags in the browser UI, with exact tag queries available through
the server API for integrations and automation. Exact tag queries are separate
from text search: a tag predicate matches normalized tag slugs, while `/api/search`
remains for manual fuzzy/domain search and discovery.

The UI should support tagging the selected asset and tagging a multi-selection
of assets. Multi-selection is in scope specifically so a user can tag a range of
visible assets, all assets in the current result set, or all assets in a folder
view. This does not imply general bulk file actions such as bulk downloads,
bulk copy actions, deletes, moves, or edits.

Applying a tag to many assets should hash only the assets that receive metadata.
The operation can be explicit and progress-reporting if needed; it should not
turn normal browsing or indexing into a full-library hashing pass.

Machine-oriented asset selection should use `POST /api/assets/query`. The query
can combine top-level indexed filters, such as media type, source ids, and a
directory subtree, with a tag-only boolean `where` expression:

```json
{
  "media_type": "audio",
  "source_ids": ["music"],
  "directory_prefix": "combat_boss",
  "where": {
    "and": [
      {"tag": "combat"},
      {"or": [{"tag": "boss"}, {"tag": "tough"}]},
      {"not": {"tag": "vocals"}}
    ]
  },
  "limit": 50,
  "offset": 0
}
```

The supported tag expression grammar is deliberately small: `tag`, `and`, `or`,
and `not`. Empty `and`/`or` clauses are invalid. Ranking, preference boosts,
least-recently-used rotation, and anti-repetition policy belong in the calling
application, not in FindMe's exact catalog query API.

`GET /api/tags/{slug}/assets` remains available as a simple single-tag,
card-shaped lookup and debugging endpoint. New integrations should prefer
`POST /api/assets/query` for exact tag selection, including the single-tag case,
because it returns flat cache-ready assets and supports boolean tag logic. Do
not add new selector behavior to `/api/tags/{slug}/assets`; if it stops serving
a concrete UI/debugging need, it can be removed in a later cleanup.

## Layout

The suggested v1 desktop layout is two-pane:

- main pane: search input and virtualized image-result grid
- right pane: selected/hovered asset preview and metadata above the source tree filter

Other layouts can be prototyped and reviewed if they preserve the same fast search, preview, filtering, and keyboard-focused workflows.

Grid cards should stay visually minimal. For v1, result cards show only the thumbnail image. Filename, source, path, and actions appear in the preview area below the larger preview image.

Folded collection cards should be visually distinct from single-asset cards and
show a variant count. Hovering a folded collection cycles through its variants
about every 600 ms in the hover preview, while the grid card itself stays stable;
it should not select or copy an asset. Clicking a folded collection expands it
in place. Expanded variants occupy normal grid positions, lose the folded count
badge, share a collection-colored background treatment, and behave like normal
asset cards. Double-clicking an expanded variant folds its collection again.

Preview behavior:

- hover updates preview after a short configurable delay
- keyboard focus updates preview immediately
- click pins/updates the preview and Enter copies the configured FoundryVTT path
- moving hover away returns to the pinned preview when there is one
- no modal is used for the primary browsing flow

## Keyboard Interaction

Keyboard-first operation is a core v1 goal.

Expected interaction style:

- `/` focuses search
- Vim-style movement where applicable
- arrow keys or Vim keys navigate results
- Enter copies the default derived path
- Enter in the search field refreshes results only; it must not auto-select or
  auto-expand the first result
- shortcuts for secondary actions can be added for raw path, download, and preview

Mouse interaction should still be efficient, but keyboard flow should be designed from the start.

## Indexing Controls

V1 should use server-side admin/CLI indexing controls first.

Examples:

- index all sources
- index one source
- cleanup unreferenced thumbnail cache files

The web UI should show lightweight index status and errors per source so it is clear when results may be incomplete. A full admin console is not required for v1.

## Testing

Testing is a hard requirement for v1 implementation. Use the `tdd` skill when building core behavior.

Tests should prioritize observable behavior through public interfaces, especially:

- indexing
- source and asset identity
- derived actions/path transforms
- index replacement and thumbnail cleanup
- search and ranking once finalized
- key API behavior

## Logging

Logging should be configurable.

Development can use debug logging. Normal server deployment should use info/error logging. Logs should go to stdout/stderr so systemd/journald can collect them.

## Future Goals

Future goals include:

- FoundryVTT integration
- AI/embedding search over indexed images
- content-hash deduplication
- move detection
- PDF indexing/preview
- richer action profiles for VTT-specific workflows
- optional ranking/debug explanations if useful during tuning

## Non-Goals For V1

- public multi-user product
- account system
- public internet hardening
- source permissions
- live filesystem watching
- slow filesystem search fallback
- full image editor
- bulk asset actions other than tag assignment
- general-purpose configuration UI
- legacy JSON metadata dictionary support
