# Changelog

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
