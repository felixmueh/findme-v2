# Search Profiles Design

This document captures the current design decisions for saved search profiles.
It supersedes the older assumption in `docs/SIDEBAR_FILTERING.md` that sidebar
filter state is temporary frontend state sent as `filter_rule` on every search
request.

## Intent

Search profiles are named bundles of folder preference state. A profile controls
both:

- folder scoring, which adjusts ranking
- folder selection, which hard-filters included assets through whitelist and
  blacklist rules

The goal is to make selecting and tuning a search scope fast enough for normal
interactive use. Searching inside a profile should not rebuild large filtered
corpora or re-scan broad sources when the same information can be derived from
precomputed directory-to-asset indexes.

## Built-In Profiles

Two profiles always exist:

- `None`: immutable empty profile with no scoring overrides and no selection
  rules.
- `Default`: editable persisted profile loaded first for a new search session.

`None` is a stable raw-search baseline. If the user edits scoring or selection
while `None` is active, the backend creates an unnamed unsaved working profile
for that session. Saving that unnamed profile requires a name and creates a new
custom profile.

`Default` is not a factory default. It is the user's normal persisted profile.

## Custom Profiles

Users can create and remove custom profiles through the UI.

Creating or saving-as a custom profile uses the current working state. This lets
the user select an existing profile, tune it, and then save the tuned state as a
new profile.

Editing an existing persisted profile and clicking save writes back to that same
profile. A separate duplicate/save-as action can be added when needed.

## Backend Ownership

The backend owns the active search profile state. The frontend sends commands,
not the complete folder settings on every search request.

The frontend keeps only enough local state to render the UI and identify the
browser search session. Profile selection, profile edits, reset actions, and
saves are API calls to the backend.

Search requests include the `search_session` id. The backend resolves that
session to the active working profile and applies the profile to search.

If a request arrives with an unknown `search_session`, the backend creates a new
in-memory session using the persisted `Default` profile as its active working
state.

## Session State

Unsaved working state is per `search_session` and lives in backend memory.

Dirty working state is not durable. If the backend restarts, unsaved profile
edits are discarded. A new or restarted backend session starts from `Default`.

The session runtime tracks:

- active profile id, or no persisted profile for an unnamed draft
- base profile version loaded from the database
- working folder settings
- `dirty_scoring`
- `dirty_selection`
- folder subtrees containing unsaved changes, for sidebar dirty indicators

The aggregate dirty state is:

```text
dirty = dirty_scoring or dirty_selection
```

The dirty flags are split because reset actions are split.

Dirty flags are based on normalized equivalence between the working settings and
the active persisted profile settings. If the user manually restores equivalent
settings, the matching dirty flag clears.

## Switching Profiles

If the active session has dirty edits, selecting another profile should fail
with a conflict unless the request explicitly discards the edits.

Expected flow:

1. User edits the active profile.
2. User selects another profile.
3. Backend returns `409 Conflict`.
4. UI offers Save, Discard, or Cancel.
5. If the user discards, the UI retries profile selection with an explicit
   discard flag.

This avoids silently losing unsaved backend-owned working state.

## Persistence

Persisted profiles are global/shared for v1. The app has no user accounts, so
there are no user-private profiles yet.

The database stores source settings, not derived search vectors:

```text
search_profiles
- id
- name
- kind: none | default | custom
- version
- sort_order
- created_at
- updated_at

search_profile_folder_settings
- profile_id
- source_id
- directory
- quality_override nullable
- selection_state nullable | whitelist | blacklist
```

`quality_override = null` means default folder quality. `selection_state = null`
means no explicit selection rule on that folder.

Multiple whitelist settings may overlap in the same source subtree. At runtime,
only bottom-most whitelist rules are effective. An ancestor whitelist with a
descendant whitelist remains stored and visible in the UI as a superseded
selection setting, but it does not include sibling assets outside the descendant
whitelist subtree.

The current `folder_quality_overrides` table should be migrated into the
`Default` profile when search profiles are introduced.

## API Shape

The frontend should interact with search profiles through command-style API
calls. It should not send complete folder settings with every search request.

Proposed endpoints:

```text
GET    /api/search-profiles
POST   /api/search-profiles/select
POST   /api/search-profiles
DELETE /api/search-profiles/{profile_id}

PATCH  /api/search-profile/settings
POST   /api/search-profile/save
POST   /api/search-profile/reset-scoring
POST   /api/search-profile/reset-selection
```

All mutating session/profile commands include `search_session`.

`GET /api/search-profiles` returns persisted profiles plus the current session
state for the supplied `search_session`, including active profile id, dirty
flags, and effective working settings needed to render the sidebar.

`POST /api/search-profiles/select` changes the active profile for the session.
If the session is dirty, it returns `409 Conflict` unless the request explicitly
sets a discard flag.

`PATCH /api/search-profile/settings` updates scoring and/or selection for a
single folder in the session working state. If the active profile is `None`, the
backend first creates an unnamed unsaved working profile for that session.

`POST /api/search-profile/save` persists the session working state. If the
working profile is unnamed, the request must include a profile name and creates
a new custom profile.

`POST /api/search-profile/reset-scoring` restores scoring settings from the
active persisted profile into the session working state.

`POST /api/search-profile/reset-selection` restores selection settings from the
active persisted profile into the session working state.

## Concurrency

Persisted profiles are shared, but unsaved working copies are per session.

Each session records the profile version it loaded. Saving checks that the
database profile version still matches the session's base version.

If another session saved the same profile first, the backend returns
`409 Conflict`. The UI can offer:

- reload latest
- overwrite anyway
- cancel

If the user explicitly overwrites, the whole persisted profile is replaced with
the session's working state. V1 does not merge profile changes at individual
folder-setting granularity.

Deleting a custom profile is allowed only for persisted custom profiles. `None`
and `Default` cannot be deleted. If a session tries to delete its active custom
profile while that session has dirty edits for that profile, the backend should
return a conflict unless the request explicitly discards the dirty working
state. After successful deletion of the active profile, that session switches to
`Default`.

## Runtime Representation

Persisted DB state is normalized folder settings. Runtime state derives fast
search data from those settings.

A session working profile should have a runtime representation such as:

```text
SearchProfileRuntime
- profile id, if backed by a persisted profile
- base profile version
- normalized folder settings
- preference_adjustment_array: numeric score adjustment per asset index
- included_asset_indexes: profile selection compiled to asset indexes, or `None`
  when all assets are included
- selection rules for UI display/fingerprinting
- effective selection rules after superseded whitelist ancestors are removed
- settings fingerprint
- dirty flags
```

`preference_adjustment_array` is derived from the current search corpus and must
not be persisted. Asset indexes can change after indexing or corpus rebuilds, so
the database should store only user-authored folder settings.

## SearchIndex Boundary

The shared `SearchIndex` should become stable indexed data plus shared caches.
Mutable user preference state should move out of the singleton `SearchIndex` and
into per-session `SearchProfileRuntime` values.

Folder quality is compiled into the active session preferences. Search calls
receive that runtime preference data directly.

Selection also belongs in the profile runtime. It remains a hard filter before
pagination, but search and browse should test `asset.asset_index` against the
compiled runtime inclusion set rather than rebuilding a filtered search corpus.

## Ranking Simplification

Search profiles supersede source-priority ranking. Source priority can remain
configuration/display metadata for ordering sources in the UI, but it should not
contribute to result score or tie-breaking once profile ranking is introduced.

The current media-intent penalty should also be removed. For example, audio
sources should not receive a special penalty for visual-looking queries. If the
user wants to prefer or suppress a source, that should be expressed through a
search profile's scoring or selection settings.

The `combined` search mode should be removed from the product search path. The
product lanes are:

- `strict`
- `fuzzy`
- `domain`

There should be no lane merging in normal search. Each selected lane retrieves
its own scored candidates, and the profile layer applies user preferences to
that lane's scored result set.

The target search flow is:

```text
retrieve scored candidates for the selected lane
apply profile preferences
cache the preference-adjusted result by query, mode, scope, vocabulary, and profile fingerprint
```

The shared post-retrieval step should be named around what it actually does,
such as `apply_preferences`, not "rerank" or "rank fusion". It takes lane-native
scores as input and adds profile preference adjustments. It should not merge
lanes or apply hidden product heuristics.

Strict/Tantivy, fuzzy, and domain retrieval may still use different retrieval
engines, but they should feed a common preference and caching boundary.

## Caching

Search caches should be keyed by semantic profile state, not by mutable object
identity or monotonic revision counters.

The profile cache component should be derived from normalized effective
settings:

- omit default quality values
- omit empty selection states
- sort by `(source_id, directory)`
- normalize equivalent settings to the same fingerprint

The ranked search cache key should include:

```text
query
source_id
directory
directory_prefix
mode
domain vocabulary identity
profile settings fingerprint
```

This allows equivalent profile states to reuse cached results. For example,
undoing a folder filter should restore the previous fingerprint and allow the
previous cached result to be reused.

Future work:

- [ ] Validate moving caching into retrieval before `apply_preferences`, or using
  separate retrieval and preference-adjusted result caches. This could let the
  backend reuse lane search results when only profile ranking changes.

## Strict Search

Strict search currently does not use the `_ranked_search` cache path. It uses
Tantivy directly, then applies a lightweight preference adjustment to each
strict match score.

Search profiles must preserve that behavior while making the preference
adjustment source profile-specific.

The current strict filtered path can be slow because filtering creates a new
filtered corpus for large selected scopes. Profile filtering should avoid
rebuilding large corpora when a profile can answer inclusion through precomputed
directory/profile data.

The refactor should make strict coherent with the other product lanes:

```text
Tantivy strict retrieval -> scored candidates -> apply_preferences -> cache
```

Fuzzy and domain should follow the same shape with their own retrieval engines.
The semantic difference between lanes is candidate retrieval, not a separate
ranking architecture.

## Directory Counts

Cards and directory counts for a search response should come from the same
resolved search scope.

Where practical, directory counts should be derived from the already resolved
matched result set instead of launching a second broad search. This is especially
important for large sources such as `MapMaking`.

The standalone `/api/directories` endpoint is not used by the current frontend
and should be removed during the search-profile refactor unless a concrete UI
need appears. Directory data for the sidebar should come from the profile-aware
search-with-directory-counts response.

The removal of `combined` search mode means search evaluation should stop using
`combined` as the required product benchmark scope. Evaluation should cover the
actual product lanes (`strict`, `fuzzy`, and `domain`) or a clearly named
workbench-only fusion mode if one is deliberately kept outside normal product
search.

## Reset And Save Actions

The UI should expose:

- profile selector
- save profile
- reset scoring
- reset selection
- create profile
- delete custom profile

Reset scoring restores scoring values from the persisted active profile into
the session working profile and recomputes scoring-derived runtime data.

Reset selection restores selection values from the persisted active profile into
the session working profile and recomputes selection-derived runtime data.

Save profile persists the full current working state. If the working profile is
unnamed because it started from `None`, save requires a profile name and creates
a custom profile.

## V1 Scope

Search profiles do not introduce real user accounts. All persisted profiles are
trusted and shared.

Future user accounts can add profile ownership or sharing, but v1 should not add
that complexity.
