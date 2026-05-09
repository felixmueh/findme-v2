from pathlib import Path

from findme.search_eval import SearchResultRef, evaluate_search, load_search_evaluation


def test_load_search_evaluation_expands_named_sets(tmp_path: Path) -> None:
    eval_path = tmp_path / "eval.yaml"
    eval_path.write_text(
        """
version: 1
sets:
  goblin_warriors:
    assets:
      - source_id: tokens
        relative_path: goblin_warrior.png
  adventurers:
    includes:
      - goblin_warriors
queries:
  - id: goblin-warrior
    text: goblin warrior
    expectations:
      - scope: combined
        assert: findable
        target: adventurers
        importance: 1.0
""",
        encoding="utf-8",
    )

    evaluation = load_search_evaluation(eval_path)

    assert evaluation.expand_target("adventurers") == frozenset(
        {("tokens", "goblin_warrior.png")}
    )
    assert evaluation.queries[0].expectations[0].target == "adventurers"


def test_evaluate_search_reports_findable_and_ranking_results(tmp_path: Path) -> None:
    eval_path = tmp_path / "eval.yaml"
    eval_path.write_text(
        """
version: 1
sets:
  goblin_warriors:
    assets:
      - source_id: tokens
        relative_path: goblin_warrior.png
  generic_goblins:
    assets:
      - source_id: tokens
        relative_path: goblin.png
queries:
  - id: goblin-warrior
    text: goblin warrior
    expectations:
      - scope: combined
        assert: findable
        target: goblin_warriors
        importance: 1.0
        gate: true
      - scope: combined
        assert: group_ranks_above
        better: goblin_warriors
        worse: generic_goblins
        rule: majority
        importance: 0.5
""",
        encoding="utf-8",
    )
    evaluation = load_search_evaluation(eval_path)

    report = evaluate_search(
        evaluation,
        runner=lambda scope, query: [
            SearchResultRef("tokens", "goblin_warrior.png"),
            SearchResultRef("tokens", "goblin.png"),
        ],
    )

    assert report.total_expectations == 2
    assert report.failed_expectations == 0
    assert report.gate_failures == 0
    assert report.weighted_score == 1.0


def test_evaluate_search_reports_gate_failures(tmp_path: Path) -> None:
    eval_path = tmp_path / "eval.yaml"
    eval_path.write_text(
        """
version: 1
sets:
  green_dragons:
    assets:
      - source_id: tokens
        relative_path: green_dragon.png
  green_goblins:
    assets:
      - source_id: tokens
        relative_path: green_goblin.png
queries:
  - id: green-dragon
    text: green dragon
    expectations:
      - scope: combined
        assert: findable
        target: green_dragons
        importance: 1.0
        gate: true
      - scope: combined
        assert: not_findable
        target: green_goblins
        importance: 0.5
""",
        encoding="utf-8",
    )
    evaluation = load_search_evaluation(eval_path)

    report = evaluate_search(
        evaluation,
        runner=lambda scope, query: [
            SearchResultRef("tokens", "green_goblin.png"),
        ],
    )

    assert report.failed_expectations == 2
    assert report.gate_failures == 1
    assert report.weighted_score == 0.0
    assert not report.passed


def test_ranking_assertion_passes_when_worse_target_is_not_findable(
    tmp_path: Path,
) -> None:
    eval_path = tmp_path / "eval.yaml"
    eval_path.write_text(
        """
version: 1
sets:
  good:
    assets:
      - source_id: tokens
        relative_path: green_dragon.png
  bad:
    assets:
      - source_id: tokens
        relative_path: green_goblin.png
queries:
  - id: green-dragon
    text: green dragon
    expectations:
      - scope: combined
        assert: group_ranks_above
        better: good
        worse: bad
        rule: majority
        importance: 1.0
""",
        encoding="utf-8",
    )
    evaluation = load_search_evaluation(eval_path)

    report = evaluate_search(
        evaluation,
        runner=lambda scope, query: [
            SearchResultRef("tokens", "green_dragon.png"),
        ],
    )

    assert report.failed_expectations == 0
