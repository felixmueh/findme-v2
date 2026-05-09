from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


AssetRef = tuple[str, str]
SearchRunner = Callable[[str, str], Iterable["SearchResultRef"]]


@dataclass(frozen=True)
class AssetTarget:
    source_id: str
    relative_path: str

    def as_ref(self) -> AssetRef:
        return (self.source_id, self.relative_path)


@dataclass(frozen=True)
class SearchResultRef:
    source_id: str
    relative_path: str

    def as_ref(self) -> AssetRef:
        return (self.source_id, self.relative_path)


@dataclass(frozen=True)
class Expectation:
    scope: str
    assertion: str
    importance: float
    target: str | AssetTarget | None = None
    better: str | AssetTarget | None = None
    worse: str | AssetTarget | None = None
    rule: str | None = None
    gate: bool = False


@dataclass(frozen=True)
class EvaluationQuery:
    id: str
    text: str
    expectations: tuple[Expectation, ...]
    tags: tuple[str, ...] = ()
    intent: str | None = None


@dataclass(frozen=True)
class SearchEvaluation:
    sets: dict[str, frozenset[AssetRef]]
    queries: tuple[EvaluationQuery, ...]

    def expand_target(self, target: str | AssetTarget) -> frozenset[AssetRef]:
        if isinstance(target, AssetTarget):
            return frozenset({target.as_ref()})
        try:
            return self.sets[target]
        except KeyError as exc:
            raise SearchEvaluationError(f"unknown set: {target}") from exc


class SearchEvaluationError(ValueError):
    pass


@dataclass(frozen=True)
class ExpectationResult:
    query_id: str
    query_text: str
    scope: str
    assertion: str
    passed: bool
    importance: float
    gate: bool
    message: str


@dataclass(frozen=True)
class EvaluationReport:
    results: tuple[ExpectationResult, ...]

    @property
    def total_expectations(self) -> int:
        return len(self.results)

    @property
    def failed_expectations(self) -> int:
        return sum(1 for result in self.results if not result.passed)

    @property
    def gate_failures(self) -> int:
        return sum(1 for result in self.results if result.gate and not result.passed)

    @property
    def weighted_score(self) -> float:
        total = sum(result.importance for result in self.results)
        if total == 0:
            return 1.0
        passed = sum(result.importance for result in self.results if result.passed)
        return passed / total

    @property
    def passed(self) -> bool:
        return self.gate_failures == 0


def load_search_evaluation(path: Path) -> SearchEvaluation:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SearchEvaluationError("search evaluation file must contain a mapping")

    raw_sets = raw.get("sets", {})
    if not isinstance(raw_sets, dict):
        raise SearchEvaluationError("sets must be a mapping")
    sets = _load_sets(raw_sets)

    raw_queries = raw.get("queries", [])
    if not isinstance(raw_queries, list):
        raise SearchEvaluationError("queries must be a list")
    queries = tuple(_load_query(raw_query) for raw_query in raw_queries)

    return SearchEvaluation(sets=sets, queries=queries)


def evaluate_search(
    evaluation: SearchEvaluation,
    *,
    runner: SearchRunner,
) -> EvaluationReport:
    result_cache: dict[tuple[str, str], tuple[SearchResultRef, ...]] = {}
    expectation_results: list[ExpectationResult] = []
    for query in evaluation.queries:
        for expectation in query.expectations:
            key = (expectation.scope, query.text)
            if key not in result_cache:
                result_cache[key] = tuple(runner(expectation.scope, query.text))
            results = result_cache[key]
            expectation_results.append(
                _evaluate_expectation(evaluation, query, expectation, results)
            )
    return EvaluationReport(results=tuple(expectation_results))


def _evaluate_expectation(
    evaluation: SearchEvaluation,
    query: EvaluationQuery,
    expectation: Expectation,
    results: tuple[SearchResultRef, ...],
) -> ExpectationResult:
    ranks = {result.as_ref(): index for index, result in enumerate(results)}
    if expectation.assertion == "findable":
        passed, message = _evaluate_findable(evaluation, expectation, ranks)
    elif expectation.assertion == "not_findable":
        passed, message = _evaluate_not_findable(evaluation, expectation, ranks)
    elif expectation.assertion in {"ranks_above", "group_ranks_above"}:
        passed, message = _evaluate_ranks_above(evaluation, expectation, ranks)
    else:
        raise SearchEvaluationError(
            f"query {query.id}: unsupported assertion {expectation.assertion}"
        )
    return ExpectationResult(
        query_id=query.id,
        query_text=query.text,
        scope=expectation.scope,
        assertion=expectation.assertion,
        passed=passed,
        importance=expectation.importance,
        gate=expectation.gate,
        message=message,
    )


def _evaluate_findable(
    evaluation: SearchEvaluation,
    expectation: Expectation,
    ranks: dict[AssetRef, int],
) -> tuple[bool, str]:
    if expectation.target is None:
        raise SearchEvaluationError("findable expectation requires target")
    targets = evaluation.expand_target(expectation.target)
    found = targets.intersection(ranks)
    if found:
        return True, f"found {len(found)}/{len(targets)} target assets"
    return False, f"found 0/{len(targets)} target assets"


def _evaluate_not_findable(
    evaluation: SearchEvaluation,
    expectation: Expectation,
    ranks: dict[AssetRef, int],
) -> tuple[bool, str]:
    if expectation.target is None:
        raise SearchEvaluationError("not_findable expectation requires target")
    targets = evaluation.expand_target(expectation.target)
    found = targets.intersection(ranks)
    if not found:
        return True, f"found 0/{len(targets)} unwanted assets"
    return False, f"found {len(found)}/{len(targets)} unwanted assets"


def _evaluate_ranks_above(
    evaluation: SearchEvaluation,
    expectation: Expectation,
    ranks: dict[AssetRef, int],
) -> tuple[bool, str]:
    if expectation.better is None or expectation.worse is None:
        raise SearchEvaluationError(
            f"{expectation.assertion} expectation requires better and worse"
        )
    if expectation.rule not in {None, "majority"}:
        raise SearchEvaluationError(f"unsupported ranking rule: {expectation.rule}")
    better = evaluation.expand_target(expectation.better)
    worse = evaluation.expand_target(expectation.worse)
    if not any(better_ref in ranks for better_ref in better):
        return False, "no better assets found"
    missing_rank = len(ranks)
    comparisons = 0
    passed = 0
    for better_ref in better:
        better_rank = ranks.get(better_ref, missing_rank)
        for worse_ref in worse:
            worse_rank = ranks.get(worse_ref, missing_rank)
            comparisons += 1
            if better_rank < worse_rank:
                passed += 1
    if comparisons == 0:
        return False, "no comparable ranked assets found"
    if passed > comparisons / 2:
        return True, f"{passed}/{comparisons} pairwise comparisons passed"
    return False, f"{passed}/{comparisons} pairwise comparisons passed"


def _load_sets(raw_sets: dict[str, Any]) -> dict[str, frozenset[AssetRef]]:
    expanded: dict[str, frozenset[AssetRef]] = {}
    visiting: set[str] = set()

    def expand(name: str) -> frozenset[AssetRef]:
        if name in expanded:
            return expanded[name]
        if name in visiting:
            raise SearchEvaluationError(f"cyclic set include: {name}")
        if name not in raw_sets:
            raise SearchEvaluationError(f"unknown set: {name}")
        visiting.add(name)
        raw_set = raw_sets[name]
        if not isinstance(raw_set, dict):
            raise SearchEvaluationError(f"set {name} must be a mapping")
        assets = {_load_asset(asset).as_ref() for asset in raw_set.get("assets", [])}
        for included in raw_set.get("includes", []):
            if not isinstance(included, str):
                raise SearchEvaluationError(f"set {name} includes must be set names")
            assets.update(expand(included))
        visiting.remove(name)
        expanded[name] = frozenset(assets)
        return expanded[name]

    for set_name in raw_sets:
        expand(set_name)
    return expanded


def _load_query(raw_query: Any) -> EvaluationQuery:
    if not isinstance(raw_query, dict):
        raise SearchEvaluationError("each query must be a mapping")
    query_id = _required_str(raw_query, "id", "query")
    text = _required_str(raw_query, "text", f"query {query_id}")
    raw_expectations = raw_query.get("expectations", [])
    if not isinstance(raw_expectations, list):
        raise SearchEvaluationError(f"query {query_id} expectations must be a list")
    tags = raw_query.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise SearchEvaluationError(f"query {query_id} tags must be a list of strings")
    intent = raw_query.get("intent")
    if intent is not None and not isinstance(intent, str):
        raise SearchEvaluationError(f"query {query_id} intent must be a string")
    return EvaluationQuery(
        id=query_id,
        text=text,
        tags=tuple(tags),
        intent=intent,
        expectations=tuple(
            _load_expectation(raw_expectation, query_id)
            for raw_expectation in raw_expectations
        ),
    )


def _load_expectation(raw_expectation: Any, query_id: str) -> Expectation:
    if not isinstance(raw_expectation, dict):
        raise SearchEvaluationError(f"query {query_id} expectation must be a mapping")
    importance = raw_expectation.get("importance", 1.0)
    if not isinstance(importance, int | float) or not 0 <= importance <= 1:
        raise SearchEvaluationError(
            f"query {query_id} expectation importance must be between 0.0 and 1.0"
        )
    return Expectation(
        scope=_required_str(raw_expectation, "scope", f"query {query_id} expectation"),
        assertion=_required_str(
            raw_expectation,
            "assert",
            f"query {query_id} expectation",
        ),
        target=_load_optional_target(raw_expectation.get("target")),
        better=_load_optional_target(raw_expectation.get("better")),
        worse=_load_optional_target(raw_expectation.get("worse")),
        rule=raw_expectation.get("rule"),
        importance=float(importance),
        gate=bool(raw_expectation.get("gate", False)),
    )


def _load_optional_target(raw_target: Any) -> str | AssetTarget | None:
    if raw_target is None:
        return None
    if isinstance(raw_target, str):
        return raw_target
    return _load_asset(raw_target)


def _load_asset(raw_asset: Any) -> AssetTarget:
    if not isinstance(raw_asset, dict):
        raise SearchEvaluationError(f"asset target must be a mapping: {raw_asset!r}")
    return AssetTarget(
        source_id=_required_str(raw_asset, "source_id", "asset target"),
        relative_path=_required_str(raw_asset, "relative_path", "asset target"),
    )


def _required_str(mapping: dict[str, Any], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SearchEvaluationError(f"{context} requires string field {key}")
    return value
