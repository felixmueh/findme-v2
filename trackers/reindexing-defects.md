# Re-indexing defects

## 1. Interrupted indexing leaves the server unable to restart

Severity: critical

Status: resolved

Observed on 2026-08-06:

- A folder index job committed a new index fingerprint while search preprocessing
  was still pending.
- The Docker daemon restarted and interrupted the job.
- The container then exited at startup with `search corpus is stale` and Docker's
  `unless-stopped` policy retried it 20 times.

Required behavior:

- `findme serve` repairs a stale derived search corpus before starting the web
  server.
- Lower-level corpus loading continues to reject stale data.

Resolution:

- `findme serve` checks the canonical fingerprint and rebuilds a stale derived
  corpus before constructing the application.
- A CLI regression test simulates interruption after the canonical index commit
  and verifies the repaired snapshot before Uvicorn starts.

## 2. Index job progress is transient and collides with toolbar status

Severity: high

Status: resolved

Observed behavior:

- Index progress replaces the normal result/status text in the crowded toolbar.
- Refreshing the page loses the client-side job state even though the server job
  continues.
- The compact status changes or disappears between indexing, preprocessing, and
  runtime reload phases.

Required behavior:

- Show a small persistent spinning job button while work is active.
- Clicking it opens a larger overlay with the job scope, phase, counters, timing,
  failures, and error details.
- Recover the current job from the server after a page refresh and keep polling
  through every phase until completion.

Resolution:

- The toolbar keeps its search/result status and shows a separate compact job
  spinner only while a job is active.
- The job dialog reports scope, phase, detail, counters, failures, start time,
  and elapsed time; backend progress includes file/action and search rebuild
  milestones.
- Page load restores the current active job, and transient poll failures retry
  without discarding it.
