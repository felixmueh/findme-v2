# Technology Stack

This document records the current technology choices for `findme-v2`.

## Repository And Deployment Shape

Use a monorepo with separate backend and frontend areas.

Conceptual layout:

```text
backend/
  findme/
  tests/
frontend/
  src/
  tests/
docs/
```

V1 deploys as a single app by default:

- backend serves API endpoints
- backend serves asset endpoints
- backend serves the built static frontend
- indexing is CLI-driven from the same backend codebase

The frontend must remain a static Vite app with a clean API boundary. This should make it easy to later host the frontend separately with nginx/Caddy/etc. while keeping FastAPI as API/asset server only.

Search engines such as Typesense or Meilisearch may run as separate required services if selected after evaluation.

## Backend

Use Python as the main backend and indexer language.

Python is the orchestrator for:

- API serving
- indexing workflow
- search lane orchestration
- media derivative orchestration
- configuration loading
- CLI tools

Performance-sensitive hot paths should use native libraries or external tools where appropriate. Rust/C++ with Python bindings is allowed later for proven bottlenecks.

Backend web framework:

- FastAPI/Starlette
- Uvicorn for serving

FastAPI serves the API, asset endpoints, and default static frontend deployment.

## Indexing And Media Pipeline

Indexing is CLI-driven for v1.

Use an explicitly parallel worker pipeline for indexing and derivative generation. This is a core requirement, not an afterthought.

Guidelines:

- use bounded worker pools
- use process-level parallelism for CPU-heavy work if threads do not scale
- batch database writes
- benchmark on real crawl data before tuning worker counts
- avoid one subprocess per tiny operation when batching or worker pools are better

No Celery/Redis/job queue in v1.

Media processing:

- Pillow for static image thumbnail generation
- consider Pillow-SIMD in deployment if it is useful and compatible
- `ffmpeg` as a required external tool for video poster/preview generation
- future PDF tooling chosen when PDF support starts

Missing required tools should fail clearly. Do not implement generic fallback behavior.

## Database

Use SQLite as the primary v1 index database.

Assumptions:

- single personal server
- index DB is disposable/rebuildable derived state
- local reads should be fast
- deployment should stay simple

Use WAL mode.

Use explicit SQL with a small repository/data-access layer. Do not start with an ORM.

Migration tooling can be chosen during implementation.

## Search Engine

Do not hand-roll fuzzy/prefix/infix lexical search.

Use a search lane adapter boundary and compare engines after crawling real data.

Candidate lexical engines:

- Typesense
- Meilisearch
- SQLite FTS5/trigram as an embedded baseline
- other candidates only if they are clearly useful

If Typesense or Meilisearch wins, it becomes a required service for the chosen deployment. There should be no weak production fallback that changes search behavior silently.

Domain expansion remains in our backend code/config for v1 rather than being hidden entirely inside a search engine synonym system.

See `docs/SEARCH.md` for the search design.

## Frontend

Use:

- Svelte
- Vite
- TypeScript
- TanStack Virtual or another proven virtualization library for the result grid

Start with plain CSS and no UI component framework.

The frontend is a static app. It talks to the backend through API calls and asset URLs. It should use relative API paths by default, with an environment override if needed later.

## API Contract

Start with handwritten TypeScript types matching backend response models.

Do not introduce generated API clients initially. FastAPI's OpenAPI output can support generation later if API drift becomes a problem.

API tests are the guardrail for backend/frontend contract behavior.

## Configuration

Use:

- TOML for app/source config
- YAML for the domain vocabulary

Configuration is server-side and hand-editable. A configuration UI is not part of v1.

## Testing

Use the `tdd` skill for implementation work.

Backend:

- pytest

Frontend:

- Vitest for frontend logic/components where useful

UI:

- Playwright later for focused smoke/performance checks if useful

Search/ranking:

- workbench/debug CLI or dev UI
- lightweight query evaluation set after real crawl data exists
- regression checks for important search expectations

Core behavior should be developed test-first through public interfaces.

## Logging

Use configurable application logging.

Development can use debug logging. Normal server deployment should use info/error logging.

Logs should go to stdout/stderr so systemd/journald can collect them.

## Deployment

V1 app deployment is native on the server.

Expected deployment pieces:

- FastAPI/Uvicorn app managed by systemd
- app logs collected by journald
- CLI indexing commands run manually or through systemd-run/timers later
- optional separate search-engine service if selected

Docker is not required for the app itself in v1. Docker can be used for search-engine experiments or services if convenient.
