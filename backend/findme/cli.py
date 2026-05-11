from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil
import sys
import time

from tqdm import tqdm
import uvicorn

from findme.api import create_app
from findme.cleanup import cleanup_index
from findme.config import ConfigError, load_config
from findme.db import connect, initialize_database
from findme.indexer import IndexEvent, count_indexable_files, index_sources
from findme.search import SearchIndex, SearchMode, load_domain_vocabulary
from findme.search_corpus import (
    SEARCH_CORPUS_VERSION,
    SearchCorpusError,
    load_search_corpus_snapshot,
    rebuild_search_corpus,
    search_corpus_lexical_path,
    search_corpus_snapshot_path,
)
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
            logger = _IndexLogger(stream=sys.stdout, verbosity=args.verbose)
            logger.log("counting indexable files started")
            total_files = count_indexable_files(config)
            logger = _IndexLogger(
                stream=sys.stdout,
                total_files=total_files,
                verbosity=args.verbose,
            )
            logger.log(f"counting indexable files finished: files={total_files}")
            logger.log(
                f"index started: files={total_files} thumbnail_workers={thumbnail_workers}"
            )
            progress = _IndexProgressReporter(
                bar=_IndexProgressBar(total_files=total_files),
                logger=logger,
            )
            with connect(db_path) as db:
                initialize_database(db)
                summary = index_sources(
                    db,
                    config,
                    thumbnail_workers=thumbnail_workers,
                    retry_failed_thumbnails=args.retry_failed_thumbnails,
                    progress=progress,
                )
            progress.finish()
            logger.log(
                "asset indexing finished: "
                f"scanned={summary.scanned_files} "
                f"upserted={summary.upserted_assets} "
                f"thumbnails={summary.generated_thumbnails} "
                f"failed={len(summary.thumbnail_failures)} "
                f"stale={summary.marked_stale}"
            )
            logger.log("search preprocessing started")
            corpus_path = rebuild_search_corpus(config, db_path, log=logger.log)
            print(
                "indexed "
                f"{summary.scanned_files} files, "
                f"upserted {summary.upserted_assets}, "
                f"generated {summary.generated_thumbnails} thumbnails, "
                f"failed {len(summary.thumbnail_failures)} thumbnails, "
                f"marked stale {summary.marked_stale}"
            )
            print(f"rebuilt search corpus: {corpus_path}")
            for failure in summary.thumbnail_failures:
                print(
                    "thumbnail failed: "
                    f"{failure.source_id}:{failure.relative_path}: {failure.message}",
                    file=sys.stderr,
                )
            return 0

        if args.command == "rebuild-search-corpus":
            logger = _IndexLogger(stream=sys.stdout)
            with connect(db_path) as db:
                initialize_database(db)
            logger.log("search preprocessing started")
            corpus_path = rebuild_search_corpus(config, db_path, log=logger.log)
            print(f"rebuilt search corpus: {corpus_path}")
            return 0

        if args.command == "serve":
            app = create_app(config, db_path, static_dir=args.static_dir)
            uvicorn.run(app, host=args.host, port=args.port)
            return 0

        if args.command == "cleanup":
            with connect(db_path) as db:
                initialize_database(db)
                summary = cleanup_index(db, data_root=config.data_root, dry_run=args.dry_run)
            prefix = "would clean" if args.dry_run else "cleaned"
            print(
                f"{prefix} "
                f"{summary.stale_assets} stale assets, "
                f"{summary.asset_search_rows} stale search rows, "
                f"{summary.unreferenced_cache_files} unreferenced cache files"
            )
            return 0

        if args.command == "search-eval":
            evaluation = load_search_evaluation(args.eval)
            selected_scopes = frozenset(args.scope or ["strict", "fuzzy", "domain"])
            evaluation = _filter_evaluation_scopes(evaluation, selected_scopes)
            domain_vocabulary = (
                load_domain_vocabulary(config.domain_vocabulary_path)
                if config.domain_vocabulary_path is not None
                else None
            )
            with connect(db_path) as db:
                initialize_database(db)
                snapshot_path = search_corpus_snapshot_path(
                    config.data_root,
                    config.search_corpus_path,
                )
                lexical_path = search_corpus_lexical_path(
                    config.data_root,
                    config.search_corpus_path,
                )
                snapshot = load_search_corpus_snapshot(snapshot_path)
                search_index = SearchIndex.load_from_snapshot(
                    db,
                    snapshot,
                    expected_version=SEARCH_CORPUS_VERSION,
                    lexical_index_path=lexical_path,
                )
                report = evaluate_search(
                    evaluation,
                    runner=lambda scope, query: [
                        SearchResultRef(result.source_id, result.relative_path)
                        for result in search_index.search(
                            query,
                            domain_vocabulary=domain_vocabulary,
                            mode=_search_mode(scope),
                            limit=args.limit,
                        ).assets
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
    except SearchCorpusError as exc:
        print(f"search corpus error: {exc}", file=sys.stderr)
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
    index.add_argument(
        "--retry-failed-thumbnails",
        action="store_true",
        help="retry thumbnail generation for unchanged assets that previously failed",
    )
    index.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase index logging verbosity; -v logs each file queued and completed",
    )

    rebuild_search = subparsers.add_parser(
        "rebuild-search-corpus",
        help="rebuild the derived search corpus from the indexed database",
    )
    rebuild_search.add_argument("--config", type=Path, required=True)

    serve = subparsers.add_parser("serve", help="serve the API and built frontend")
    serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--static-dir", type=Path, default=Path("frontend/dist"))

    cleanup = subparsers.add_parser(
        "cleanup",
        help="remove stale index rows and unreferenced generated cache files",
    )
    cleanup.add_argument("--config", type=Path, required=True)
    cleanup.add_argument(
        "--dry-run",
        action="store_true",
        help="report cleanup counts without deleting anything",
    )

    search_eval = subparsers.add_parser(
        "search-eval",
        help="run local search quality evaluation",
    )
    search_eval.add_argument("--config", type=Path, required=True)
    search_eval.add_argument("--eval", type=Path, required=True)
    search_eval.add_argument(
        "--scope",
        action="append",
        choices=["strict", "fuzzy", "domain"],
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
    if scope not in {"strict", "fuzzy", "domain"}:
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


class _IndexLogger:
    def __init__(
        self,
        *,
        stream,
        total_files: int | None = None,
        progress_interval_seconds: float = 30.0,
        verbosity: int = 0,
    ) -> None:
        self._stream = stream
        self._total_files = total_files
        self._progress_interval_seconds = progress_interval_seconds
        self._verbosity = verbosity
        self._last_progress_log = 0.0
        self._current_source_id: str | None = None

    def log(self, message: str) -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"{timestamp} {message}", file=self._stream, flush=True)

    def event(self, event: IndexEvent) -> None:
        if event.kind == "thumbnail-wait":
            waiting = ", ".join(event.waiting_assets)
            self.log(
                "index waiting for thumbnail workers: "
                f"source={event.source_id} {event.message} waiting=[{waiting}]"
            )
            return
        if self._verbosity < 1:
            return
        if event.kind == "file-queued":
            self.log(
                "index file queued: "
                f"source={event.source_id} "
                f"path={event.relative_path} "
                f"action=thumbnail "
                f"{event.message}"
            )

    def progress(self, progress) -> None:
        if self._total_files is None:
            return
        if progress.current_source_id != self._current_source_id:
            self._current_source_id = progress.current_source_id
            self.log(
                "index source active: "
                f"source={progress.current_source_id} "
                f"scanned={progress.scanned_files}/{self._total_files}"
            )
        if self._verbosity >= 1 and progress.current_relative_path:
            self.log(
                "index file done: "
                f"source={progress.current_source_id} "
                f"path={progress.current_relative_path} "
                f"action={progress.current_action} "
                f"thumbnail={progress.current_thumbnail}"
            )

        now = time.monotonic()
        should_log = (
            self._last_progress_log == 0.0
            or now - self._last_progress_log >= self._progress_interval_seconds
            or progress.scanned_files >= self._total_files
        )
        if not should_log:
            return

        self._last_progress_log = now
        self.log(
            "index progress: "
            f"scanned={progress.scanned_files}/{self._total_files} "
            f"source={progress.current_source_id} "
            f"upserted={progress.upserted_assets} "
            f"thumbnails={progress.generated_thumbnails} "
            f"failed={progress.failed_thumbnails} "
            f"stale={progress.marked_stale} "
            f"elapsed={progress.elapsed_seconds:.1f}s"
        )


class _IndexProgressReporter:
    def __init__(self, *, bar: "_IndexProgressBar", logger: _IndexLogger) -> None:
        self._bar = bar
        self._logger = logger

    def __call__(self, progress) -> None:
        if isinstance(progress, IndexEvent):
            self._logger.event(progress)
            return
        self._bar(progress)
        self._logger.progress(progress)

    def finish(self) -> None:
        self._bar.finish()


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
