# Changelog

## 0.1.1 — 2026-10-03

- **india-code:** repeal status is now `true` / `false` / `null`. India Code sets the flag on
  only 845 of 1,753 central Acts; the rest were wrongly reported as in force. Parser version 2;
  existing harvests can be corrected offline with `harvester reparse india-code`.
- **india-code:** allow two concurrent requests (the file endpoint is slow server-side), with
  request starts still at least one second apart.
- **india-code:** crawl despite robots.txt returning HTTP 500, with the reason documented in the
  source and in every run manifest.
- Docs: note that India Code was used to demonstrate and test harvester; document the test fixtures.
- CI: GitHub Actions updated to current major versions.

## 0.1.0 — 2026-10-03

First release.

- Crawl engine with a persistent SQLite frontier: resumable, de-duplicated, prioritised.
- Content-addressed raw store with fetch history; offline `reparse`.
- Per-request cache policies (prefer / revalidate / bypass) with conditional requests.
- robots.txt per RFC 9309, Crawl-delay, per-host throttling with adaptive back-off and Retry-After.
- Honest Scrapling-based fetcher (no impersonation, real User-Agent).
- JSONL output with per-record provenance and data licence; run manifests.
- Built-in sources: OAI-PMH, DSpace 7+, India Code.
- CLI: run, reparse, status, changes, show, robots, list, new.
