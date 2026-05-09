# Search Evaluation

This document defines how `findme-v2` evaluates search quality against the real DND asset library.

Search evaluation is a local benchmark and tuning aid. It is not a generic CI fixture. The benchmark may depend on the local DND source mount:

```text
/media/NAS/main/Documents/DND
```

Agents working on search behavior, ranking, indexing signals, domain vocabulary, or search evaluation must run the local DND search benchmark before considering the work complete.

## Purpose

The benchmark encodes user-observable search expectations, not search implementation details.

It should answer questions such as:

- Does this query find the asset a user would expect?
- Does a better match rank above a weaker match?
- Does a broad/domain query surface conceptually related assets?
- Are misleading partial matches suppressed?
- Did a search change improve or regress weighted search quality?

The benchmark is allowed to produce both hard failures and scored reports. Not every expectation should be a hard test.

## Benchmark Scopes

Expectations are scoped to user-observable search behaviors:

- `strict`: literal/token-accurate matching.
- `fuzzy`: typo, separator, plural, or order-tolerant matching.
- `domain`: conceptual or vocabulary-driven matching.
- `combined`: normal product search behavior.

These scopes are benchmark vocabulary. They do not require a one-to-one mapping with code modules or search engines.

## Asset Identity

Expected assets are identified by canonical indexed identity:

- `source_id`
- `relative_path`

Do not use absolute paths in benchmark judgments. Do not use derived `asset_id` values as durable judgment keys.

Example:

```yaml
source_id: dnd
relative_path: Tokens/Goblins/goblin_warrior_green.png
```

## Files And Sets

Search judgments should live in the repository under `search-eval/`, for example:

```text
search-eval/
  dnd.yaml
  sets/
    creatures.yaml
    terrain.yaml
    props.yaml
```

YAML has no portable built-in file include. The evaluation runner may load multiple YAML files or directories, but benchmark files should not depend on parser-specific `!include` behavior.

Named sets are allowed. Sets may contain concrete assets and may include other sets.

Example:

```yaml
sets:
  goblin_warriors:
    assets:
      - source_id: dnd
        relative_path: Tokens/Goblins/goblin_warrior_green.png
      - source_id: dnd
        relative_path: Tokens/Goblins/goblin_warrior_red.png

  goblin_archers:
    assets:
      - source_id: dnd
        relative_path: Tokens/Goblins/goblin_archer_green.png

  goblin_combatants:
    includes:
      - goblin_warriors
      - goblin_archers
```

The runner should fail clearly for missing set names, missing assets, or cyclic set inclusion.

## Query-Centered Expectations

Benchmark files are query-centered. Each query has text, optional intent/notes, and expectations.

Example:

```yaml
queries:
  - id: dragon-green
    text: dragon green
    intent: Find green dragon assets; avoid color-only green matches on unrelated creatures.
    expectations:
      - scope: combined
        assert: findable
        target: green_dragons
        importance: 1.0
        gate: true

      - scope: combined
        assert: group_ranks_above
        better: green_dragons
        worse: green_goblins
        rule: majority
        importance: 0.8

      - scope: combined
        assert: not_findable
        target: green_goblins
        importance: 0.6
```

Query metadata such as `tags`, `intent`, and `notes` is encouraged because benchmark reports should be able to explain which classes of search behavior improved or regressed.

## Assertion Types

The initial benchmark language should support these assertion concepts:

- `findable`: the target is inside the useful/high-confidence result mass for this scope.
- `not_findable`: the target is outside the useful/high-confidence result mass for this scope.
- `ranks_above`: one asset or set ranks above another asset or set.
- `group_ranks_above`: one set generally ranks above another set.

`findable` is not the same as `rank <= N`. Some search systems produce discrete candidate sets; others may rank every item or produce probability-like scores. The useful result mass should be interpreted per benchmark scope by evaluator configuration.

For sparse candidate systems, `findable` may mean inclusion in the candidate set used by product ranking. For rank-all or probabilistic systems, it may mean inclusion in the high-mass/high-confidence portion of the result distribution.

## Ranking Assertions

Prefer relative ranking assertions over raw score expectations. Raw scores are implementation details and may change when the search engine or ranking formula changes.

Example:

```yaml
- scope: fuzzy
  assert: ranks_above
  better:
    source_id: dnd
    relative_path: Tokens/Goblins/goblin_warrior_green.png
  worse:
    source_id: dnd
    relative_path: Tokens/Goblins/goblin_green.png
  importance: 1.0
  gate: true
```

Group ranking assertions may support rules such as:

- `majority`: most better-vs-worse comparisons pass.
- `all_pairs`: every better item outranks every worse item.
- `median`: the better group has a better median rank than the worse group.

The format may name rules before the runner implements all of them. Unsupported rules should fail clearly.

## Negative Expectations

Negative expectations should be explicit assertions, not negative relevance scores.

Use `not_findable` when a misleading result should be outside the useful result mass. Use `ranks_above` or `group_ranks_above` when the key requirement is suppression relative to better results.

Example:

```yaml
- scope: combined
  assert: group_ranks_above
  better: green_dragons
  worse: green_goblins
  rule: majority
  importance: 0.8
```

## Importance And Gates

Each expectation may define:

- `importance`: a float from `0.0` to `1.0` used for weighted benchmark scoring.
- `gate`: optional boolean. If `true`, failure is a hard benchmark failure regardless of aggregate score.

Suggested interpretation:

- `1.0`: core expectation.
- `0.5`: normal tuning expectation.
- `0.1`: weak or exploratory preference.
- `0.0`: observation only.

Do not overload `importance: 1.0` to mean hard failure. Hard failure should be encoded with `gate: true`.

## Reporting

The evaluator should report:

- hard gate failures
- weighted score by scope
- weighted score by tag
- failed expectations with query id, assertion, and relevant ranks/evidence
- changes from a prior run when available

The report should help tune search behavior. It should not only print a single pass/fail result.

## Current Command

The CLI evaluator can run the implemented `strict`, `fuzzy`, `domain`, and `combined` benchmark scopes:

```bash
uv run findme search-eval --config config/findme.server.toml --eval search-eval/dnd.yaml --scope combined
```

The configured data root must be writable and already indexed. Repeat `--scope` to evaluate multiple scopes in one run.
