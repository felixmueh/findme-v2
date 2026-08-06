# Findme API Reference For Agents

This document is a consumer-facing reference for agents or integrations that need
to call the Findme HTTP API. It documents the current backend contract exposed by
`backend/findme/api.py`.

Findme is a private/trusted-user app. The v1 API has no authentication,
authorization, CSRF protocol, or public multi-user isolation.

## Runtime Docs

The backend is a FastAPI app. When the server is running, FastAPI also exposes:

- interactive docs: `/docs`
- raw OpenAPI schema: `/openapi.json`

Use this file as the stable agent-facing guide. If behavior differs, treat
`backend/findme/api.py` and `backend/tests/test_api.py` as the executable source
of truth and update this file.

## Conventions

- Use relative URLs such as `/api/search/query` from the bundled frontend.
- Request and response bodies are JSON unless the endpoint returns a file.
- Error responses use FastAPI's normal JSON shape:

```json
{ "detail": "message" }
```

- Common validation failures return `400`.
- Missing assets or profiles return `404`.
- Unsaved search profile conflicts return `409`.
- Superseded searches can return `204 No Content` when `search_session` and
  `search_revision` are used.

## Common Types

### Search Mode

```ts
type SearchMode = "strict" | "fuzzy" | "domain"
```

### Media Type

```ts
type MediaType = "image" | "video" | "audio" | "other"
```

### Tag

```ts
type Tag = {
  slug: string
  label: string
}
```

Tag input is normalized by the backend. Prefer sending human labels such as
`"Boss Fight"` and compare returned tags by `slug`.

### Tag Where Clause

`where` filters are tag-only boolean expressions.

```ts
type TagWhereClause =
  | { tag: string }
  | { not: TagWhereClause }
  | { and: TagWhereClause[] }
  | { or: TagWhereClause[] }
```

Rules:

- `and` and `or` arrays must not be empty.
- Maximum nesting depth is limited.
- Maximum total tag clauses is limited.
- Invalid grammar returns `400`.

Example:

```json
{
  "and": [
    { "tag": "combat" },
    { "or": [{ "tag": "boss" }, { "tag": "tough" }] },
    { "not": { "tag": "vocals" } }
  ]
}
```

### Thumbnail Variant

```ts
type ThumbnailVariant = {
  kind: string
  max_edge: number
  width: number
  height: number
  url: string
}
```

Thumbnail URLs include a version query parameter derived from file size and
mtime. Keep and reuse the returned URL rather than reconstructing it.

### Asset

Search and browse responses return cache-friendly assets. Detail responses add
actions and may include metadata-only fields.

```ts
type Asset = {
  asset_id: string
  source_id: string
  source_display_name: string
  relative_path: string
  filename: string
  stem: string
  extension: string
  directory: string
  size_bytes: number
  media_type: string
  media_width: number | null
  media_height: number | null
  duration_seconds: number | null
  score?: number
  tags?: Tag[]
  thumbnail_url?: string
  thumbnail_width?: number
  thumbnail_height?: number
  thumbnail_variants?: ThumbnailVariant[]
}
```

`thumbnail_*` fields are omitted when the asset has no generated thumbnail.
`score` is present on search/browse result assets, but not required for detail
callers.

Flat `result_shape: "assets"` responses include additional detail fields:

```ts
type AssetActions = {
  open: { id: "open"; label: string; url: string }
  copy: { id: "copy"; label: string; enabled: boolean; url: string }
  copy_thumbnail: {
    id: "copy_thumbnail"
    label: string
    enabled: boolean
    url: string | null
  }
  download: { id: "download"; label: string; url: string }
  copy_path: { id: "copy_path"; label: string; value: string }
  copy_foundryvtt_path: {
    id: "copy_foundryvtt_path"
    label: string
    value: string
  }
}

type AssetQueryResult = Asset & {
  mtime_ns: number
  absolute_path: string
  tags: Tag[]
  actions: AssetActions
}
```

### Result Card

The default search result shape is grouped cards, not a flat asset list.

```ts
type ResultCard =
  | { kind: "asset"; asset: Asset }
  | {
      kind: "collection"
      collection_key: string
      representative: Asset
      variants: Asset[]
      count: number
    }
```

Collection cards represent variant groups. Consumers that need every concrete
asset should inspect both `asset` cards and `collection.variants`, or request
`result_shape: "assets"`.

### Directory Row

Directory rows describe indexed source/directory state and current match counts.

```ts
type DirectoryRow = {
  id: number
  source_id: string
  path: string
  name: string
  depth: number
  asset_count: number
  matched_asset_count: number
  included_asset_count: number
  quality: number
  quality_override: number | null
  selection_state: "whitelist" | "blacklist" | null
  selection_effective: boolean
  profile_dirty: boolean
  has_descendant_whitelist: boolean
  has_descendant_blacklist: boolean
  file_browser_url?: string
}
```

`file_browser_url` is present only when the source config has a File Browser URL
prefix.

For lightweight refreshes, `directory_state_only: true` returns compact arrays
instead of full rows:

```ts
type DirectoryState = [
  id: number,
  matched_asset_count: number,
  included_asset_count: number,
  quality: number,
  quality_override: number | null,
  selection_state: "whitelist" | "blacklist" | null,
  selection_effective: boolean,
  profile_dirty: boolean,
  has_descendant_whitelist: boolean,
  has_descendant_blacklist: boolean,
]
```

### Search Profile

```ts
type SearchProfile = {
  id: string
  name: string
  kind: "none" | "default" | "custom"
  version: number
  sort_order: number
}

type SearchProfileSession = {
  search_session: string
  active_profile_id: string | null
  active_profile_name: string | null
  active_profile_kind: "none" | "default" | "custom" | null
  base_profile_version: number | null
  dirty_scoring: boolean
  dirty_selection: boolean
  dirty: boolean
  settings_fingerprint: string
}

type FolderSetting = {
  source_id: string
  directory: string
  quality_override: number | null
  selection_state: "whitelist" | "blacklist" | null
}
```

Use one stable `search_session` string per browser tab or integration workflow.
If omitted, the backend uses a shared default browser session.

### Index Job

Folder re-indexing runs as one server-managed background job. Timestamps are
Unix epoch seconds.

```ts
type IndexJob = {
  id: string
  status: "queued" | "running" | "succeeded" | "failed"
  scope: {
    kind: "folder"
    source_id: string
    directory: string
  }
  phase:
    | "queued"
    | "indexing"
    | "rebuilding-search"
    | "reloading-search"
    | "complete"
    | "failed"
  detail: string | null
  scanned_files: number
  new_assets: number
  written_assets: number
  generated_thumbnails: number
  failed_thumbnails: number
  current_source_id: string | null
  current_relative_path: string | null
  current_action: string | null
  elapsed_seconds: number
  error: string | null
  started_at: number
  finished_at: number | null
}
```

`detail` is a human-readable description of the current work. During search
preprocessing it distinguishes snapshot building/writing, lexical-index
building, artifact publication, and runtime reload.

## Endpoints

### `GET /api/health`

Health check.

Response:

```json
{ "status": "ok" }
```

### `GET /api/sources`

Lists configured asset sources, sorted by descending priority.

Response:

```ts
{
  sources: Array<{
    id: string
    display_name: string
    root: string
    priority: number
  }>
}
```

### `POST /api/index-jobs`

Starts a folder-scoped incremental index job.

Request:

```ts
{
  scope: {
    kind: "folder"
    source_id: string
    directory: string
  }
}
```

`directory` is a normalized source-relative directory; use `""` for the source
root. Unknown sources, paths outside the configured source, files, and
non-normalized paths are rejected. The server allows one queued/running job at a
time and returns `409` if another active job exists.

Successful response: `202 Accepted`

```ts
{ job: IndexJob }
```

### `GET /api/index-jobs/current`

Returns the current or most recently completed server-managed job. Before any
job has started, `job` is `null`.

```ts
{ job: IndexJob | null }
```

Clients should restore a returned queued/running job after refresh and poll its
id through every phase until it reaches `succeeded` or `failed`.

### `GET /api/index-jobs/{job_id}`

Returns the current job by id.

```ts
{ job: IndexJob }
```

Returns `404` when the id is not the manager's current/most recent job. Polling
clients should tolerate transient request failures without discarding an
otherwise active job.

### `GET /api/tags`

Lists all known metadata tags. Returns an empty list when the metadata database
does not exist yet.

Response:

```ts
{ tags: Tag[] }
```

### `POST /api/search/query`

Unified search and browse endpoint. This is the main API for search results,
browse results, folder rows, and folder-state refreshes.

Request:

```ts
type SearchQueryRequest = {
  q?: string | null
  mode?: SearchMode
  source_id?: string | null
  directory?: string | null
  directory_prefix?: string | null
  view_source_id?: string | null
  view_directory?: string | null
  search_session?: string | null
  search_revision?: number | null
  media_type?: "image" | "video" | "audio" | "other" | null
  where?: TagWhereClause | null
  result_shape?: "cards" | "assets" | "directories"
  include_directories?: boolean
  directory_state_only?: boolean
  limit?: number
  offset?: number
}
```

Defaults:

- `mode`: `"strict"`
- `result_shape`: `"cards"`
- `include_directories`: `false`
- `directory_state_only`: `false`
- `limit`: `100`, valid range `1..500`
- `offset`: `0`

Scope rules:

- Empty or omitted `q` means browse.
- `source_id`, `directory`, and `directory_prefix` are direct source-relative
  filters.
- `view_source_id` and `view_directory` must be provided together.
- `view_*` scope cannot be combined with `source_id`, `directory`, or
  `directory_prefix`.
- `directory` is an exact directory path. `directory_prefix` is a subtree.
- `media_type` and `where` are applied to both search and browse.
- Use `search_revision` with a stable `search_session` to let the backend return
  `204` for older superseded searches.

Cards response:

```ts
{
  cards: ResultCard[]
  total: number
  next_offset: number
  has_more: boolean
  directories?: DirectoryRow[]
  directory_states?: DirectoryState[]
}
```

Flat assets response, with `result_shape: "assets"`:

```ts
{
  assets: AssetQueryResult[]
  total: number
  next_offset: number
  has_more: boolean
  directories?: DirectoryRow[]
  directory_states?: DirectoryState[]
}
```

Directories-only response, with `result_shape: "directories"`:

```ts
{
  directories?: DirectoryRow[]
  directory_states?: DirectoryState[]
}
```

Example search:

```json
{
  "q": "boss fight",
  "mode": "strict",
  "media_type": "audio",
  "source_id": "music",
  "directory_prefix": "combat",
  "where": {
    "and": [
      { "tag": "combat" },
      { "not": { "tag": "vocals" } }
    ]
  },
  "result_shape": "cards",
  "include_directories": true,
  "limit": 50,
  "offset": 0
}
```

Example directory-state refresh:

```json
{
  "q": "castle",
  "mode": "domain",
  "search_session": "tab-1",
  "result_shape": "directories",
  "directory_state_only": true
}
```

### `GET /api/assets`

Browse endpoint for paginated cards. Prefer `POST /api/search/query` for new
code because it supports the full search/browse/filter contract.

Query parameters:

```ts
{
  source_id?: string
  directory?: string
  directory_prefix?: string
  view_source_id?: string
  view_directory?: string
  search_session?: string
  limit?: number
  offset?: number
}
```

Response:

```ts
{
  cards: ResultCard[]
  total: number
  next_offset: number
  has_more: boolean
}
```

### `GET /api/assets/{asset_id}`

Returns one asset plus computed actions.

Response:

```ts
{
  asset: Asset
  actions: AssetActions
}
```

Notes:

- `asset.tags` is always included in the detail response.
- `actions.open.url` streams the original file.
- `actions.download.url` adds `?download=1`.
- `copy_path.value` is the server filesystem path.
- `copy_foundryvtt_path.value` is derived from source output profile config.

### `GET /api/assets/{asset_id}/file`

Streams the original file.

Query parameters:

```ts
{ download?: boolean }
```

When `download=true`, the response includes a download filename. Missing files
return `404`.

### `GET /api/assets/{asset_id}/thumb`

Streams a generated thumbnail as `image/webp`.

Query parameters:

```ts
{ max_edge?: number }
```

Use the returned `thumbnail_url` or `thumbnail_variants[].url` from asset
responses. Those URLs include `max_edge` and cache version parameters when
needed.

Missing thumbnails return `404`. Successful responses include long-lived
immutable cache headers.

### `PUT /api/assets/{asset_id}/tags`

Replaces all tags for one asset.

Request:

```ts
{ tags: string[] }
```

Response:

```ts
{ tags: Tag[] }
```

The asset file must exist because tagging lazily establishes durable metadata
identity for the file. Missing files return `404`; invalid tags return `400`.

### `POST /api/tags/batch`

Adds and/or removes tags for many assets. Provide exactly one of `asset_ids` or
`scope`.

Request:

```ts
type BatchTagsRequest = {
  asset_ids?: string[]
  scope?: BatchTagsScope
  add_tags?: string[]
  remove_tags?: string[]
}

type BatchTagsScope =
  | {
      kind: "folder"
      source_id: string
      directory?: string
      search_session?: string | null
      where?: TagWhereClause | null
    }
  | {
      kind: "search"
      q?: string | null
      mode?: SearchMode
      source_id?: string | null
      directory_prefix?: string | null
      view_source_id?: string | null
      view_directory?: string | null
      search_session?: string | null
      where?: TagWhereClause | null
    }
```

Rules:

- `asset_ids` and `scope` are mutually exclusive.
- At least one of `add_tags` or `remove_tags` is required.
- Duplicate `asset_ids` are deduplicated in request order.
- Folder scope requires `source_id`; `directory` is treated as the subtree root.
- Search scope follows the same `view_*` exclusivity rules as
  `POST /api/search/query`.

Response:

```ts
{
  matched: number
  tagged: number
  failed: Array<{ asset_id: string; message: string }>
}
```

Per-asset failures are returned in `failed`; the endpoint can still return `200`
when some assets failed.

### `GET /api/search-profiles`

Lists profiles and returns the active session state.

Query parameters:

```ts
{ search_session?: string }
```

Response:

```ts
{
  profiles: SearchProfile[]
  session: SearchProfileSession
  settings: FolderSetting[]
}
```

### `POST /api/search-profiles/select`

Selects a profile for a search session.

Request:

```ts
{
  search_session?: string | null
  profile_id: string
  discard?: boolean
}
```

Response:

```ts
{ session: SearchProfileSession }
```

If the current session has unsaved changes and `discard` is not true, returns
`409`.

### `POST /api/search-profiles`

Creates a custom profile from the session's current working settings and selects
it.

Request:

```ts
{
  search_session?: string | null
  name?: string | null
  overwrite?: boolean
}
```

`overwrite` is accepted by the shared request model but is not used by profile
creation.

Response:

```ts
{
  profiles: SearchProfile[]
  session: SearchProfileSession
  settings: FolderSetting[]
}
```

### `DELETE /api/search-profiles/{profile_id}`

Deletes a custom profile.

Query parameters:

```ts
{
  search_session?: string
  discard?: boolean
}
```

Response:

```ts
{ session: SearchProfileSession }
```

Only custom profiles can be deleted. If deleting the active profile with unsaved
changes and `discard` is not true, returns `409`.

### `PATCH /api/search-profile/settings`

Updates one folder setting in the active search profile session.

Request:

```ts
{
  search_session?: string | null
  source_id: string
  directory?: string
  quality_override?: number | null
  selection_state?: "whitelist" | "blacklist" | null
}
```

Rules:

- `directory` defaults to the source root (`""`).
- `quality_override` must be `0..10` or `null`.
- `selection_state` must be `"whitelist"`, `"blacklist"`, or `null`.
- Omitting `quality_override` leaves the current quality setting unchanged.
- Sending `quality_override: null` clears the current quality override.
- Omitting `selection_state` leaves the current selection setting unchanged.
- Sending `selection_state: null` clears the selection rule.
- Unknown or non-normalized directories return `400`.

Response:

```ts
{ session: SearchProfileSession }
```

### `POST /api/search-profile/save`

Saves the active working settings.

Request:

```ts
{
  search_session?: string | null
  name?: string | null
  overwrite?: boolean
}
```

Behavior:

- If the session has no active profile, this creates a custom profile.
- If the session has an active profile, this saves into that profile.
- Version conflicts return `409` unless `overwrite` is true.

Response:

```ts
{
  session: SearchProfileSession
  profiles: SearchProfile[]
  settings: FolderSetting[]
}
```

### `POST /api/search-profile/reset-scoring`

Resets unsaved quality overrides to the active profile's persisted scoring
settings.

Request:

```ts
{ search_session?: string | null }
```

Response:

```ts
{ session: SearchProfileSession }
```

### `POST /api/search-profile/reset-selection`

Resets unsaved whitelist/blacklist selection changes to the active profile's
persisted selection settings.

Request:

```ts
{ search_session?: string | null }
```

Response:

```ts
{ session: SearchProfileSession }
```

## Implementation Guidance

- Prefer `POST /api/search/query` for all new search, browse, folder, media
  type, and tag-filtered result loading.
- Request `result_shape: "assets"` when an integration needs flat concrete
  assets and does not want to understand result cards.
- Request `include_directories: true` with cards/assets when the UI needs result
  rows and folder counts from the same search scope.
- Request `result_shape: "directories"` plus `directory_state_only: true` for
  cheap folder-state refreshes.
- Keep `search_session` stable for a tab/workflow so profile settings and
  search supersession work correctly.
- Do not infer filesystem paths from `relative_path`; use asset detail actions
  for server paths, Foundry paths, downloads, and file URLs.
- Do not write to source directories. The API reads source assets and stores
  metadata/cache state under the configured app data root.
- Do not depend on legacy routes. `GET /api/search`,
  `GET /api/search-with-directory-counts`, `GET /api/tags/{slug}/assets`, and
  `POST /api/assets/query` were removed.
