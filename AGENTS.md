# AGENTS.md

## Orientation

Before design or implementation work, read:

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

## Development Environment

Agents are likely running in a sandbox environment on the developer laptop during implementation. This is expected and acceptable. The final production setup will happen on the server after the product is finished.

Install development tools or dependencies in the sandbox when they are needed to build, test, or evaluate the project. Keep production deployment assumptions documented in `docs/DESIGN.md` or `docs/STACK.md` rather than inferring them from the sandbox.

To let the developer view the app from the host browser during sandbox development, build the frontend and run:

```bash
cd /home/felix/_Documents/Projects/findme-v2
npm --prefix frontend run build
.venv/bin/findme serve --config config/findme.dev.toml --host 0.0.0.0 --port 8000 --static-dir frontend/dist
```

Port 8000 should be published from the sandbox to the host. The developer can then open `http://localhost:8000`.

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

## Scope Discipline

Do not expand v1 scope without an explicit user decision. When a design decision changes, update `docs/DESIGN.md` so future work starts from the same understanding.

## Work Tracking

Agents may keep local todo trackers in `trackers/*.md` when useful for multi-step work.

Commit completed, coherent work whenever it is sensible to do so.
