from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

from tqdm import tqdm
import uvicorn

from findme.api import create_app
from findme.config import ConfigError, load_config
from findme.db import connect, initialize_database
from findme.indexer import count_indexable_files, index_sources
from findme.search import SearchMode, load_domain_vocabulary, search_assets
from findme.search_eval import (
    EvaluationQuery,
    SearchEvaluation,
    SearchResultRef,
    evaluate_search,
    load_search_evaluation,
)


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


def run(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        db_path = config.data_root / "findme.sqlite3"

        if args.command == "init-db":
            with connect(db_path) as db:
                initialize_database(db)
            print(f"initialized database: {db_path}")
            return 0

        if args.command == "index":
            thumbnail_workers = (
                args.thumbnail_workers
                if args.thumbnail_workers is not None
                else config.thumbnail_workers
            )
            if thumbnail_workers < 1:
                print("--thumbnail-workers must be a positive integer", file=sys.stderr)
                return 2
            if args.rebuild_thumbnails:
                shutil.rmtree(config.data_root / "cache" / "thumbs", ignore_errors=True)
            progress = _IndexProgressBar(total_files=count_indexable_files(config))
            with connect(db_path) as db:
                initialize_database(db)
                summary = index_sources(
                    db,
                    config,
                    thumbnail_workers=thumbnail_workers,
                    progress=progress,
                )
            progress.finish()
            print(
                "indexed "
                f"{summary.scanned_files} files, "
                f"upserted {summary.upserted_assets}, "
                f"generated {summary.generated_thumbnails} thumbnails, "
                f"failed {len(summary.thumbnail_failures)} thumbnails, "
                f"marked stale {summary.marked_stale}"
            )
            for failure in summary.thumbnail_failures:
                print(
                    "thumbnail failed: "
                    f"{failure.source_id}:{failure.relative_path}: {failure.message}",
                    file=sys.stderr,
                )
            return 0

        if args.command == "serve":
            app = create_app(config, db_path, static_dir=args.static_dir)
            uvicorn.run(app, host=args.host, port=args.port)
            return 0

        if args.command == "search-eval":
            evaluation = load_search_evaluation(args.eval)
            selected_scopes = frozenset(args.scope or ["combined"])
            evaluation = _filter_evaluation_scopes(evaluation, selected_scopes)
            domain_vocabulary = (
                load_domain_vocabulary(config.domain_vocabulary_path)
                if config.domain_vocabulary_path is not None
                else None
            )
            with connect(db_path) as db:
                report = evaluate_search(
                    evaluation,
                    runner=lambda scope, query: [
                        SearchResultRef(result.source_id, result.relative_path)
                        for result in search_assets(
                            db,
                            query,
                            domain_vocabulary=domain_vocabulary,
                            mode=_search_mode(scope),
                            limit=args.limit,
                        )
                    ],
                )
            passed = report.total_expectations - report.failed_expectations
            print(
                "search evaluation: "
                f"{passed}/{report.total_expectations} expectations passed"
            )
            print(f"weighted score: {report.weighted_score:.3f}")
            print(f"gate failures: {report.gate_failures}")
            for result in report.results:
                if not result.passed:
                    print(
                        "failed: "
                        f"{result.query_id} [{result.scope}] "
                        f"{result.assertion}: {result.message}"
                    )
            return 0 if report.passed else 1
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"filesystem error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="findme")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="initialize the index database")
    init_db.add_argument("--config", type=Path, required=True)

    index = subparsers.add_parser("index", help="index configured asset sources")
    index.add_argument("--config", type=Path, required=True)
    index.add_argument(
        "--rebuild-thumbnails",
        action="store_true",
        help="delete cached thumbnails before indexing so they are regenerated",
    )
    index.add_argument(
        "--thumbnail-workers",
        type=int,
        help="override configured thumbnail worker count for this index run",
    )

    serve = subparsers.add_parser("serve", help="serve the API and built frontend")
    serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--static-dir", type=Path, default=Path("frontend/dist"))

    search_eval = subparsers.add_parser(
        "search-eval",
        help="run local search quality evaluation",
    )
    search_eval.add_argument("--config", type=Path, required=True)
    search_eval.add_argument("--eval", type=Path, required=True)
    search_eval.add_argument(
        "--scope",
        action="append",
        choices=["strict", "fuzzy", "domain", "combined"],
        help="benchmark scope to evaluate",
    )
    search_eval.add_argument(
        "--limit",
        type=int,
        default=500,
        help="number of ranked results considered findable for the current search",
    )

    return parser


def _search_mode(scope: str) -> SearchMode:
    if scope not in {"strict", "fuzzy", "domain", "combined"}:
        raise ValueError(f"unsupported search scope: {scope}")
    return scope


def _filter_evaluation_scopes(
    evaluation: SearchEvaluation,
    scopes: frozenset[str],
) -> SearchEvaluation:
    return SearchEvaluation(
        sets=evaluation.sets,
        queries=tuple(
            EvaluationQuery(
                id=query.id,
                text=query.text,
                tags=query.tags,
                intent=query.intent,
                expectations=tuple(
                    expectation
                    for expectation in query.expectations
                    if expectation.scope in scopes
                ),
            )
            for query in evaluation.queries
        ),
    )


class _IndexProgressBar:
    def __init__(self, *, total_files: int) -> None:
        self._last_scanned = 0
        self._bar = tqdm(
            total=total_files,
            desc="indexing",
            unit="file",
            dynamic_ncols=True,
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}, {postfix}]",
            file=sys.stderr,
        )

    def __call__(self, progress) -> None:
        delta = progress.scanned_files - self._last_scanned
        if delta > 0:
            self._bar.update(delta)
            self._last_scanned = progress.scanned_files
        rate = (
            progress.generated_thumbnails / progress.elapsed_seconds
            if progress.elapsed_seconds > 0
            else 0.0
        )
        self._bar.set_postfix_str(
            f"source={progress.current_source_id} "
            f"upserted={progress.upserted_assets} "
            f"thumbnails={progress.generated_thumbnails} "
            f"failed={progress.failed_thumbnails} "
            f"thumbs/s={rate:.1f} "
            f"stale={progress.marked_stale}",
            refresh=False,
        )

    def finish(self) -> None:
        self._bar.close()


if __name__ == "__main__":
    main()
