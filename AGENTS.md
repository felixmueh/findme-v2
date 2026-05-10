# AGENTS.md

## Orientation

Before any repository work, read:

- `~/.config/findme-v2/ENVIRONMENT.md` for the active environment

If `~/.config/findme-v2/ENVIRONMENT.md` does not exist, ask the user which environment this is before continuing. Do not create `.agent/ENVIRONMENT.md`, `.agents/ENVIRONMENT.md`, or any other active environment marker inside the repository.

The active environment file must live outside the repo and should be a symlink to one of:

- `docs/environments/laptop.md`
- `docs/environments/server.md`
- `docs/environments/laptop-sbx.md`
- `docs/environments/server-sbx.md`

Example:

```bash
mkdir -p ~/.config/findme-v2
ln -sf "$PWD/docs/environments/server.md" ~/.config/findme-v2/ENVIRONMENT.md
```

Before design or implementation work, also read:

- `docs/DESIGN.md` for current product and system decisions

Keep product decisions in `docs/DESIGN.md`, not in this file. This file is for durable instructions to future agents.

## Engineering Style

This is personal-use software for a known environment. Prefer small, direct, maintainable solutions over broad abstractions or generic configurability.

Prioritize:

- small code
- maintainable design
- high interaction speed
- explicit server-side configuration
- strong tests for core behavior
- clear error messages
- direct solutions for the actual use case

Use the `tdd` skill for implementation work. Core behavior should be developed test-first through public interfaces.

When working on search behavior, ranking, indexing signals, domain vocabulary, or search evaluation, run the local DND search benchmark/evaluation command before considering the work complete. The benchmark is allowed to depend on the local DND source mount and does not need to run in generic CI.

## Environments

This project is used from exactly four environments:

- laptop host
- server host
- laptop Docker sandbox
- server Docker sandbox

The host and sandbox may share the same repository working directory. The active environment marker must not live inside the repository.

Committed environment descriptions live in `docs/environments/`. Each profile describes accessible paths, writable paths, safe commands, commands that require explicit confirmation, and how to serve and index the app.

Each worktree has its own app port recorded in the repository-root `.port` file. When launching the app, use the port from `.port` for that worktree, including Docker sandbox port publishing, except on the server host environment. The server host must always serve on port 8000 regardless of `.port`.

Use `config/findme.dev.toml` on laptop and laptop-sbx. This indexes only `sample-assets` and may share `.findme-data` between the laptop host and laptop sandbox.

Use `config/findme.server.toml` on server and server-sbx. Both server environments may assume `/media/NAS/main/Documents/DND` is mounted and may share `/var/lib/findme-v2`.

Install development tools or dependencies in the sandbox when they are needed to build, test, or evaluate the project. Keep production deployment assumptions documented in `docs/DESIGN.md` or `docs/STACK.md` rather than inferring them from the sandbox.

## No Generic Fallbacks

Do not add fallback mechanisms just to support unknown environments.

If the configured environment is missing a required tool, directory, or dependency, fail clearly with an actionable error. Do not silently degrade into a weaker generic path unless the user explicitly asks for that behavior.

## Source Safety

Asset source directories are read-only inputs. Do not write index files, metadata, generated derivatives, or cache data into source directories.

The complete DND asset library is mounted read-only at:

```text
/media/NAS/main/Documents/DND
```

Prefer the local development sample library at:

```text
sample-assets
```

Use the sample library for normal development and tests. If more realistic coverage is needed, copy only the required assets from the read-only DND mount into `sample-assets`, keep the sample set below 40 GB, and do not bulk-mirror the full library.

## Compatibility

Do not preserve backwards compatibility by default. This is personal-use software in a controlled environment; when an API, data shape, CLI contract, or frontend contract should change, update callers and tests directly instead of adding compatibility layers. Add temporary compatibility only when the user explicitly asks for a migration bridge.

## Scope Discipline

Do not expand v1 scope without an explicit user decision. When a design decision changes, update `docs/DESIGN.md` so future work starts from the same understanding.

## Work Tracking

Agents may keep local todo trackers in `trackers/*.md` when useful for multi-step work.

Commit completed, coherent work whenever it is sensible to do so.
