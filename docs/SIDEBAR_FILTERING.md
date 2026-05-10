# Sidebar Directory Tree Filtering

This document specifies the final-product behavior for the sidebar source/directory tree as a search and browse filter control.

The tree is not a filesystem browser. It is an indexed-data control surface backed by source and directory metadata already present in the Findme index. It must never perform live filesystem walking from the UI.

## Goals

- Show the source/directory context of the currently hovered or keyboard-focused asset.
- Let the user include and exclude whole source or directory subtrees with explicit green and red rule states.
- Apply subtree filters as hard filters to search and empty-query browsing.
- Keep the model small enough to implement directly and test through public search/browse interfaces.

## Tree Nodes

Every filterable node is identified by:

```text
source_id + directory_path
```

The source root is represented as `directory_path = ""` and is a real rule node. This allows an entire source to be whitelisted or blacklisted.

Directory paths are source-relative directory paths using `/` separators. Assets inherit rules from their source root and directory ancestors. The asset filename itself is not a filter node.

Example asset:

```text
source_id = dnd
relative_path = Creatures/Goblins/Token/goblin.png
asset directory = Creatures/Goblins/Token
```

Candidate rule ancestors, from broadest to nearest:

```text
dnd:
dnd:Creatures
dnd:Creatures/Goblins
dnd:Creatures/Goblins/Token
```

## Rule States

Each tree node has exactly one of three states:

```text
unspecified
whitelist
blacklist
```

UI language:

- `whitelist`: green, includes the subtree unless a nearer blacklist overrides it.
- `blacklist`: red, excludes the subtree unless a nearer whitelist overrides it.
- `unspecified`: no explicit rule on that node.

The UI should expose focus and block actions alongside the folder ranking control so filtering and ranking feel like one folder-preference surface. The current rule state must remain legible on the row. Shift-clicking a folder row should toggle whitelist/focus for that row; Alt-clicking a folder row should toggle blacklist/block for that row.

Whitelists act as focus points. There must never be two whitelist rules in the same source subtree. A whitelist on one branch and another whitelist on a sibling branch are valid, and whitelist rules in different sources are valid. If the user whitelists a child of an already-whitelisted node, the whitelist moves from the parent to the child. If the user whitelists a parent of an already-whitelisted descendant, the whitelist moves to the parent.

## Effective Filter Algorithm

The rule set is global for the current browser session.

Whitelist mode becomes active when at least one whitelist rule exists anywhere in the tree. In whitelist mode, otherwise-unspecified assets are excluded.

For each asset:

1. Build the ordered ancestor list from source root through the asset's directory.
2. Find the nearest ancestor with an explicit rule.
3. If the nearest rule is `whitelist`, include the asset.
4. If the nearest rule is `blacklist`, exclude the asset.
5. If no explicit ancestor rule exists:
   - include the asset when there are no whitelist rules globally
   - exclude the asset when at least one whitelist rule exists globally

In pseudocode:

```text
has_global_whitelist = any(rule.state == whitelist for rule in rules)

function includes(asset):
    nearest = nearest_rule_on_asset_ancestor_chain(asset, rules)
    if nearest == whitelist:
        return true
    if nearest == blacklist:
        return false
    return !has_global_whitelist
```

This creates a simple "deepest specified parent wins" model:

- Green source, red child: include the source except that child subtree.
- Red source, green child: exclude the source except that child subtree.
- One green node anywhere: hide all unrelated unspecified assets.
- Only red nodes: include everything except red subtrees.

## Replacement Of Single Directory Selection

The red/green subtree rule set replaces the current single selected directory filter.

There should not be a separate hard filter for "selected directory" in the final model. A row can still be clicked for focus, expansion, preview context, or keyboard navigation, but only red/green rules affect result inclusion.

This avoids unclear combinations such as:

```text
selected directory = Maps
green rule = Tokens
red rule = Tokens/Unused
```

Instead, the equivalent hard filter is represented entirely by rule state:

```text
Maps = whitelist
```

## Scope Of Effects

The same rule set applies to:

- text search results
- empty-query browsing
- search result counts
- directory match counts
- infinite/range result loading

The asset grid must always honor the effective include/exclude decision, regardless of whether the backend serves results through search or browse-style endpoints.

## Hover And Focus Path Highlighting

When an asset is hovered in the result grid, the sidebar highlights the full ancestor path from source root through the asset's direct directory.

Keyboard focus on an asset uses the same path highlight behavior as mouse hover.

Highlight levels:

- source root and intermediate ancestors: subtle path highlight
- direct asset directory: strongest path highlight
- rule state colors remain visible and should not be obscured by hover highlighting

When the hover/focus asset is cleared, the path highlight clears. Pinned preview state can remain independent of the transient tree highlight.

The tree should not auto-apply filters merely because an asset is hovered. Hover highlighting is visual context only.

## Tree Visibility

Excluded branches remain visible in the tree. The tree is a control surface, so hiding a blacklisted branch would make it harder to understand or reverse the active rule set.

Manual collapse still works normally. Collapse controls tree display only; collapse does not affect filtering.

If a hovered asset is under a collapsed branch, the product should prefer preserving the user's collapse state. A later implementation may optionally reveal a compact breadcrumb elsewhere, but hover alone should not force-expand the tree.

## Counts

The tree should continue to show counts for source and directory nodes.

Recommended count model:

- `asset_count`: total indexed, non-stale assets under the subtree before the current text query and before red/green rules.
- `matched_asset_count`: assets under the subtree matching the current text query before red/green rules.
- `included_asset_count`: assets under the subtree that remain after applying the current red/green rules, and after the text query when one is active.

For v1 of this feature, `included_asset_count` can be added only where needed for user clarity. The critical behavioral requirement is that result grids are filtered correctly. Counts should not drive filtering semantics.

Rows with zero included assets should remain visible if they have an explicit rule, contain an explicit descendant rule, or are needed as ancestors for visible rule context.

## Temporary State

Filter rules are temporary client state for v1.

They should survive ordinary in-app interactions such as typing, changing search lanes, scrolling, expanding/collapsing folders, and opening previews. They do not need to survive page reloads or browser restarts.

The state shape should still be serializable so persistence can be added later without changing semantics:

```json
{
  "rules": [
    { "source_id": "dnd", "path": "", "state": "whitelist" },
    { "source_id": "dnd", "path": "Creatures/Unused", "state": "blacklist" }
  ]
}
```

Unspecified nodes should be omitted from the serialized rule list.

## API Shape

Because rule state is temporary client state, the client sends the current rule set with search requests. The backend does not persist these rules.

Recommended request representation:

```text
filter_rule={"source_id":"dnd","path":"","state":"whitelist"}
filter_rule={"source_id":"dnd","path":"Creatures/Unused","state":"blacklist"}
```

Each `filter_rule` value is a small JSON object encoded through normal URL query encoding. This avoids inventing path separator escaping rules.

Notes:

- `directory_path` may be empty for a source root.
- The server must validate source IDs, state values, and normalized directory paths.
- Unknown sources or malformed rules should produce a clear 400 error.
- Duplicate rules for the same node should be rejected or last-write-wins consistently; rejecting is preferred because it surfaces client bugs.
- Multiple whitelist rules in the same source subtree should be rejected because the client is responsible for normalizing whitelist focus before sending rules.

The same parameter should be accepted by:

- `GET /api/search`
- `GET /api/search-with-directory-counts`

The older `directory_prefix` and exact `directory` parameters become implementation details or compatibility-only paths once the sidebar rule model is built.

## Backend Evaluation

The backend should parse request rules into a normalized filter object:

```text
DirectoryFilterRule(source_id, path, state)
DirectoryFilter(rules, has_whitelist)
```

Core behavior should live in a small pure function that can be tested without HTTP:

```text
includes_asset(source_id, directory, filter) -> bool
```

Search and browse should apply this function as a hard filter before returning a page of results. Search ranking should rank only included candidates, or filter ranked candidates before pagination. Pagination must always operate over the filtered result set so `total`, `offset`, and `limit` remain coherent.

Directory counts should be computed from the same effective filter logic when included counts are returned.

## Frontend State Model

The frontend should maintain a map keyed by source and path:

```text
filterRules: Map<TreeNodeKey, "whitelist" | "blacklist">
```

Suggested derived values:

- `hasWhitelistRule`
- `hoveredAssetAncestorKeys`
- `directHoveredDirectoryKey`
- `explicitRuleAncestorKeys`

The row renderer should combine classes from independent concerns:

- expansion/collapse
- hover path highlight
- explicit whitelist/blacklist state
- contains explicit descendant rule
- matched/included count state

Filtering state should not be stored on directory rows returned by the API. Directory rows describe indexed structure and counts; active rule state is client interaction state.

## Testing Requirements

Use test-first implementation for core behavior.

Backend tests should cover:

- no rules includes all assets
- only blacklist excludes that subtree
- one whitelist globally excludes unrelated unspecified assets
- nearest ancestor whitelist overrides broader blacklist
- nearest ancestor blacklist overrides broader whitelist
- source root rules apply to the whole source
- rules are source-scoped even when directory paths match across sources
- pagination totals are computed after filtering
- malformed API rule parameters return clear 400 errors

Frontend behavior tests should cover:

- toggling green/red/unspecified updates request parameters
- rules survive search text and search lane changes
- single selected-directory filtering is no longer sent
- hovered or focused asset highlights its full source-to-directory path
- excluded branches remain visible unless manually collapsed

When this work touches search behavior, run the local DND search benchmark/evaluation command before considering it complete:

```bash
uv run findme search-eval --config config/findme.server.toml --eval search-eval/dnd.yaml --scope combined
```

## Open Non-Blocking Details

These details do not change the filtering semantics and can be decided during UI implementation:

- exact iconography for green/red controls
- whether row click focuses a row, expands a row, or both
- whether keyboard shortcuts toggle whitelist/blacklist on the focused tree row
- whether included counts are shown immediately or added after the first implementation
- whether hover path highlighting should show a compact breadcrumb when ancestors are collapsed
