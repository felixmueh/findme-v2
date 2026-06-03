# Frontend Performance Findings

This is an alternate path to directory state prediction.

Directory state prediction tries to avoid waiting for folder count/state updates before the result grid can feel responsive. The experiments here point to a simpler first fix: stop rendering and patching the entire expanded folder tree on every search or directory change. If folder display work is bounded, the app can show search results quickly even when exact folder counts are refreshed later.

## Summary

The 300 ms frontend delay is not explained by rendering 50 small thumbnails. It reproduces with mocked API responses and 1x1 GIF thumbnails, so the dominant browser-side cost is elsewhere.

The strongest finding is that result updates are coupled to folder tree rendering. When many folder rows are visible, a search result update also makes the folder panel expensive, even when the actual asset grid is small.

Measured in a disposable Playwright harness with mocked endpoints:

- 1000 visible full folder rows: about 1.4 s median search update.
- Same 1000 directories with root collapsed: under about 300 ms.
- Same 1000 directories with a simple 40-row virtual folder window: about 113 ms median, about 119 ms p90.
- 5000 directories with a 40-row virtual folder window: about 297 ms median unless follow-up work is deferred.
- 5000 directories with virtual folder window, no immediate 30-to-100 fill, deferred directory state refresh, and raw large arrays: about 116 ms median, about 230 ms p90.

## What This Means

The primary problem is display work:

- too many folder rows are mounted when the tree is expanded
- each row has non-trivial controls, classes, labels, icons, and derived state
- search changes and folder state changes happen in one large Svelte component
- row helper calls such as key construction, visibility checks, labels, and state flags multiply by visible row count

Internal work exists, but it is secondary. Once folder DOM is bounded, then these costs become visible:

- compact directory-state JSON parsing and merge work
- the follow-up directory count refresh after search
- the 30-result first page followed by the 100-result fill
- Svelte deep proxy overhead for large payload arrays

Raw array state helps only after display work is bounded. It does not fix full expanded folder rendering by itself.

## Recommended Simple Direction

1. Virtualize the folder panel.

   Keep roughly 40 mounted folder rows plus small overscan. Preserve scroll height with spacer blocks. This is the highest-confidence fix from the experiments and keeps the UI model intact.

2. Defer directory count/state refresh from the first-result path.

   Show result cards first. Refresh folder counts shortly after paint or during idle time. Exact count freshness matters less than avoiding blocking the result grid.

3. Avoid the immediate 30-to-100 fill on the critical path.

   The split introduced by `c89f6b57` did not solve the browser-side issue. It can still add an extra result update. Load enough for first display, then fill later when idle or when scrolling needs it.

4. Use raw Svelte state for large immutable payload arrays.

   `directories` and `cards` are server snapshots. Treat them as immutable arrays and replace them wholesale:

   ```ts
   let directories = $state.raw<DirectoryNode[]>([])
   let cards = $state.raw<ResultCard[]>([])
   ```

   Do not mutate nested directory objects in place. When compact state changes a row, create a new row object and assign a new array only when something actually changed.

## Not Promising

- CSS `contain` / `content-visibility` on the folder panel made the measured case worse.
- Merely extracting a lighter folder component helped at large row counts but did not solve the problem.
- Freezing directory state refresh did not solve the full expanded tree cost.
- Raw arrays alone left 1000 visible folder rows around the same multi-second scale.

## Practical Design Implication

Directory state prediction may still be useful later, but it should not be the first lever. The first lever should be making folder rendering bounded and scheduling folder count refreshes after result paint. That path is simpler, easier to reason about, and directly attacks the measured bottleneck.
