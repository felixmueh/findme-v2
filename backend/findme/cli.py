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
from findme.cleanup import cleanup_thumbnails
from findme.config import ConfigError, load_config
from findme.db import connect, initialize_database, search_corpus_is_stale
from findme.indexer import IndexEvent, index_sources
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
            source_ids = frozenset(args.source) if args.source is not None else None
            if source_ids is not None:
                configured_sources = {source.id for source in config.sources}
                unknown_sources = sorted(source_ids - configured_sources)
                if unknown_sources:
                    print(
                        "unknown source id(s): "
                        + ", ".join(unknown_sources),
                        file=sys.stderr,
                    )
                    return 2
            thumbnail_worker_timeout_seconds = args.thumbnail_worker_timeout_seconds
            if thumbnail_worker_timeout_seconds is not None:
                if thumbnail_worker_timeout_seconds < 0:
                    print(
                        "--thumbnail-worker-timeout-seconds must be non-negative "
                        "(0 disables timeouts)",
                        file=sys.stderr,
                    )
                    return 2
                if thumbnail_worker_timeout_seconds == 0:
                    thumbnail_worker_timeout_seconds = None
            if thumbnail_workers < 1:
                print("--thumbnail-workers must be a positive integer", file=sys.stderr)
                return 2
            if args.rebuild_thumbnails:
                shutil.rmtree(config.data_root / "cache" / "thumbs", ignore_errors=True)
            logger = _IndexLogger(
                stream=sys.stdout,
                verbosity=args.verbose,
            )
            logger.log(f"index started: thumbnail_workers={thumbnail_workers}")
            progress = _IndexProgressReporter(
                bar=None,
                logger=logger,
            )
            with connect(db_path) as db:
                initialize_database(db)
                summary = index_sources(
                    db,
                    config,
                    source_ids=source_ids,
                    thumbnail_workers=thumbnail_workers,
                    retry_failed_thumbnails=args.retry_failed_thumbnails,
                    rebuild_thumbnails=args.rebuild_thumbnails,
                    thumbnail_worker_timeout_seconds=thumbnail_worker_timeout_seconds,
                    progress=progress,
                )
                corpus_stale = search_corpus_is_stale(db)
            progress.finish()
            logger.log(
                "asset indexing finished: "
                f"scanned={summary.scanned_files} "
                f"written={summary.written_assets} "
                f"thumbnails={summary.generated_thumbnails} "
                f"failed={len(summary.thumbnail_failures)}"
            )
            corpus_path = search_corpus_snapshot_path(
                config.data_root,
                config.search_corpus_path,
            )
            if corpus_stale:
                logger.log("search preprocessing started")
                corpus_path = rebuild_search_corpus(config, db_path, log=logger.log)
            else:
                logger.log("search preprocessing skipped: corpus current")
            thumbnail_cleanup_summary = None
            if args.cleanup_thumbnails:
                logger.log("thumbnail cleanup started")
                with connect(db_path) as db:
                    initialize_database(db)
                    thumbnail_cleanup_summary = cleanup_thumbnails(
                        db,
                        data_root=config.data_root,
                        progress=_CleanupProgressReporter(),
                        log=logger.log,
                    )
            print(
                "indexed "
                f"{summary.scanned_files} files, "
                f"written {summary.written_assets}, "
                f"generated {summary.generated_thumbnails} thumbnails, "
                f"failed {len(summary.thumbnail_failures)} thumbnails"
            )
            if corpus_stale:
                print(f"rebuilt search corpus: {corpus_path}")
            else:
                print(f"search corpus current: {corpus_path}")
            if thumbnail_cleanup_summary is not None:
                print(
                    "cleaned "
                    f"{thumbnail_cleanup_summary.unreferenced_thumbnail_files} "
                    "unreferenced thumbnail files"
                )
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
            with connect(db_path) as db:
                initialize_database(db)
                corpus_stale = search_corpus_is_stale(db)
            if corpus_stale:
                logger = _IndexLogger(stream=sys.stdout)
                logger.log("stale search corpus detected at startup; rebuilding")
                rebuild_search_corpus(config, db_path, log=logger.log)
            app = create_app(config, db_path, static_dir=args.static_dir)
            uvicorn.run(app, host=args.host, port=args.port)
            return 0

        if args.command == "cleanup-thumbnails":
            logger = _IndexLogger(stream=sys.stderr)
            logger.log("thumbnail cleanup command started")
            progress = _CleanupProgressReporter()
            with connect(db_path) as db:
                logger.log("thumbnail cleanup: acquiring database connection")
                initialize_database(db)
                logger.log("thumbnail cleanup: database initialized")
                summary = cleanup_thumbnails(
                    db,
                    data_root=config.data_root,
                    dry_run=args.dry_run,
                    progress=progress,
                    log=logger.log,
                )
            progress.finish()
            prefix = "would clean" if args.dry_run else "cleaned"
            print(
                f"{prefix} "
                f"{summary.unreferenced_thumbnail_files} "
                "unreferenced thumbnail files"
            )
            logger.log("thumbnail cleanup command completed")
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
        "--cleanup-thumbnails",
        action="store_true",
        help="remove unreferenced generated thumbnails after a successful index",
    )
    index.add_argument(
        "--thumbnail-worker-timeout-seconds",
        type=float,
        default=None,
        help=(
            "mark thumbnail generation as failed and continue if a worker exceeds this many seconds "
            "(0 disables timeouts; no value means no timeout)"
        ),
    )
    index.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase index logging verbosity; -v logs each file queued and completed",
    )
    index.add_argument(
        "--source",
        action="append",
        dest="source",
        help="limit index run to one or more source ids",
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
        "cleanup-thumbnails",
        help="remove unreferenced generated thumbnail files",
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
        self._logged_scan_complete_progress = False

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
        if self._verbosity >= 2:
            if event.kind == "thumbnail-timeout":
                self.log(
                    "index thumbnail timeout: "
                    f"source={event.source_id} "
                    f"{event.message}"
                )
            if event.kind == "source-assets-loaded":
                self.log(
                    "index source assets loaded: "
                    f"source={event.source_id} "
                    f"{event.message}"
                )
            if event.kind == "index-count-start":
                self.log(
                    "index count start: "
                    f"source={event.source_id}"
                )
            elif event.kind == "index-count-checkpoint":
                self.log(
                    "index count checkpoint: "
                    f"source={event.source_id} "
                    f"{event.message}"
                )
            elif event.kind == "index-count-complete":
                self.log(
                    "index count complete: "
                    f"source={event.source_id} "
                    f"{event.message}"
                )
            if event.kind == "scan-checkpoint":
                self.log(
                    "index scan checkpoint: "
                    f"source={event.source_id} "
                    f"path={event.relative_path} "
                    f"{event.message}"
                )
            elif event.kind == "thumbnail-worker-started":
                self.log(
                    "index thumbnail worker start: "
                    f"source={event.source_id} "
                    f"path={event.relative_path} "
                    f"{event.message}"
                )
            if event.kind == "source-scan-complete":
                self.log(
                    "index source scan complete: "
                    f"source={event.source_id} "
                    f"{event.message}"
                )
            if event.kind == "file-skip":
                self.log(
                    "index file skip: "
                    f"source={event.source_id} "
                    f"path={event.relative_path} "
                    f"reason={event.message}"
                )
            elif event.kind in {"file-scheduled", "file-submitted"}:
                self.log(
                    "index file scheduled: "
                    f"source={event.source_id} "
                    f"path={event.relative_path} "
                    f"{event.message}"
                )
            if event.kind == "file-submitted":
                wait_ms = (
                    f"{event.wait_seconds:.2f}s"
                    if event.wait_seconds is not None
                    else "n/a"
                )
                self.log(
                    "index file submitted: "
                    f"source={event.source_id} "
                    f"path={event.relative_path} "
                    f"worker={event.worker_id or 'n/a'} "
                    f"wait={wait_ms} "
                    f"{event.message}"
                )
            if event.kind == "file-failed":
                queue_wait_ms = (
                    f"{event.wait_seconds:.2f}s"
                    if event.wait_seconds is not None
                    else "n/a"
                )
                self.log(
                    "index file failed: "
                    f"source={event.source_id} "
                    f"path={event.relative_path} "
                    f"queue_wait={queue_wait_ms} "
                    f"{event.message}"
                )
            elif event.kind == "file-completed":
                queue_wait_ms = (
                    f"{event.wait_seconds:.2f}s"
                    if event.wait_seconds is not None
                    else "n/a"
                )
                worker_ms = (
                    f"{event.worker_elapsed_seconds:.2f}s"
                    if event.worker_elapsed_seconds is not None
                    else "n/a"
                )
                self.log(
                    "index file completed: "
                    f"source={event.source_id} "
                    f"path={event.relative_path} "
                    f"worker={event.worker_id or 'n/a'} "
                    f"worker_elapsed={worker_ms} "
                    f"queue_wait={queue_wait_ms} "
                    f"{event.message}"
                )
            elif event.kind == "insert-batch":
                self.log(
                    "index insert batch: "
                    f"source={event.source_id} "
                    f"{event.message}"
                )
            elif event.kind == "insert-batch-start":
                self.log(
                    "index insert batch start: "
                    f"source={event.source_id} "
                    f"{event.message}"
                )
        if event.kind == "file-queued":
            self.log(
                "index file queued: "
                f"source={event.source_id} "
                f"path={event.relative_path} "
                f"action=thumbnail "
                f"{event.message}"
            )

    def progress(self, progress) -> None:
        if progress.current_source_id != self._current_source_id:
            self._current_source_id = progress.current_source_id
            if self._total_files is not None:
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

        if self._total_files is None:
            return

        now = time.monotonic()
        should_log = (
            self._last_progress_log == 0.0
            or now - self._last_progress_log >= self._progress_interval_seconds
            or (
                progress.scanned_files >= self._total_files
                and not self._logged_scan_complete_progress
            )
        )
        if not should_log:
            return

        self._last_progress_log = now
        if progress.scanned_files >= self._total_files:
            self._logged_scan_complete_progress = True
        self.log(
            "index progress: "
            f"scanned={progress.scanned_files}/{self._total_files} "
            f"source={progress.current_source_id} "
            f"written={progress.written_assets} "
            f"thumbnails={progress.generated_thumbnails} "
            f"failed={progress.failed_thumbnails} "
            f"elapsed={progress.elapsed_seconds:.1f}s"
        )


class _IndexProgressReporter:
    def __init__(
        self,
        *,
        bar: "_IndexProgressBar | None",
        logger: _IndexLogger,
    ) -> None:
        self._bar = bar
        self._logger = logger

    def __call__(self, progress) -> None:
        if isinstance(progress, IndexEvent):
            self._logger.event(progress)
            return
        if self._bar is not None:
            self._bar(progress)
        self._logger.progress(progress)

    def finish(self) -> None:
        if self._bar is not None:
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
            f"written={progress.written_assets} "
            f"thumbnails={progress.generated_thumbnails} "
            f"failed={progress.failed_thumbnails} "
            f"thumbs/s={rate:.1f}",
            refresh=False,
        )

    def finish(self) -> None:
        self._bar.close()


class _CleanupProgressReporter:
    def __init__(self) -> None:
        self._bars: dict[str, "tqdm | None"] = {}
        self._positions: dict[str, int] = {}

    def __call__(self, stage: str, current: int, total: int | None) -> None:
        if total is not None and total <= 0:
            return

        bar = self._bars.get(stage)
        if bar is None:
            bar = tqdm(
                total=total,
                desc=_cleanup_stage_description(stage),
                unit="item",
                dynamic_ncols=True,
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}, {postfix}]",
                file=sys.stderr,
            )
            self._bars[stage] = bar
            self._positions[stage] = 0
        elif total is not None and bar.total != total:
            bar.total = total

        previous = self._positions[stage]
        delta = current - previous
        if delta > 0:
            bar.update(delta)
            self._positions[stage] = current
        if total is not None and current == total:
            bar.n = total
            bar.refresh()

    def finish(self) -> None:
        for bar in self._bars.values():
            if bar is not None:
                bar.close()


def _cleanup_stage_description(stage: str) -> str:
    if stage == "thumbnail-scan":
        return "cleanup thumbnail scan"
    if stage == "thumbnails":
        return "cleanup thumbnails"
    return f"cleanup {stage}"


if __name__ == "__main__":
    main()
