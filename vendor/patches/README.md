# MediaCrawler workbench compatibility patch 2

Base: `5d547f45862ca2d4b404e5a51775879d579b29d4` (no upstream upgrade).

`workbench-2.patch` records the changes against the untouched source copies in
`baseline/`. `manifest.json` records SHA-256 values of original and patched files.
The installation check and every new worker verify patched files before import.
Upstream copyright/license headers are preserved.

Explicit interfaces:

- `DouYinCrawler(runtime=...).start()`: owns the embedding lifecycle, login and
  extension dispatch; `get_comments(page_hooks=...)` enters the actual client
  pagination; `collect_activity(...)` registers likes/recommendation pagination.
- `DouYinClient.prepare_request(...)` and optional `signer`: public signing hook,
  replacing private-function cloning. The pinned signing algorithm is unchanged.
- `pagination.collect_pages(fetch, hooks)`: stop checks, checkpoint resume,
  raw-response normalization and atomic page callbacks. Request failures preserve
  the saved cursor. Three consecutive empty, repeated or stalled pages stop at
  the last safe position. There is no verified cursor-invalid response for these
  pinned endpoints, so none currently enables automatic replay from page one.
  Hook exceptions propagate; request retry budgets remain owned by the host.
- `DouYinLogin(manual_login=...)`: manual login extension, without slider solving.
- `CDPBrowserManager(connection_url=...)`: loopback-only attachment; cleanup does
  not close an externally owned browser/context. The worker stops its driver.
- `BrowserLauncher.launch_dedicated(...)`: service-owned Chrome, separate profile,
  loopback debugging, normal browser flags. Initial blank page avoids competing
  startup navigations; the session extension navigates once to Douyin.
- Default stores/media downloader import lazily; embedded workbench never enables
  unrelated platforms, database stores, or media downloads.

Production extensions and SQLite adaptation live in `backend/engine*.py`.
Legacy collectors remain only as behavioral comparison fixtures/public imports;
the worker has no fallback call to them. Existing dependency lock (51 resolved
packages) already covers the actual Douyin engine import graph, so no unrelated
upstream dependencies or conflicting upstream FastAPI pins were installed.

To regenerate the patch after a reviewed compatibility change:
`python vendor/patches/build_patch.py`. Do not change the baseline copies.

Version 2 removes the previous fallback replay and checks cancellation again
before saving a fetched page. Reply-growth evidence and its database migration
are implemented in the host, without changing upstream endpoint parameters.
