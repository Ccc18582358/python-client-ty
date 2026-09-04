# Evidence-driven diagnostics

## Evidence order

1. Reproduce with exact input, environment, page/task ID and timestamp.
2. Search `_app.log` and traceback around that timestamp; do not infer ordering from isolated lines.
3. Trace current code from the triggering signal/button/timer to the first persisted or visible result.
4. Inspect resolved config paths and values with secrets redacted.
5. Query the actual selected DB read-only and compare row state before/after the event.
6. Capture thread identity for Qt/COM failures and actual request/response metadata for network failures.
7. Change one cause, repeat the same reproduction, and compare evidence.

## Symptom routing

- Cannot login/captcha: `active.json` → `APIConfig` resolved URL/timeout → request exception/status/business payload → session expiry/persistence → login signal result.
- Task never runs: task row enabled/status/schedule → `_should_run` time/window/last run → registered scanners → spawned thread exception → stop event.
- Crawl returns nothing: task game/platform mapping → list request parameters → response business code/page data/token → detail queue → normalization/filtering → submission count.
- Proxy failures: merged proxy config → cache age/value → provider response → paid API status/business body → authenticated proxy format → direct-vs-proxy comparison. Never print credentials or tokens. For paid API startup/deduction, the client sends only the `token` header. HTTP 403 with JSON `msg="missing signature headers"` means the route is reachable but the backend gateway/interceptor rejected the token-only request; capture it as `MISSING_SIGNATURE_HEADERS`, not as network failure or package-not-open. For Panzhi `ProxyError`/curl `(7)`/`WinError 10061` proxy connection failures, invalidate the cached proxy and wait the proxy TTL before retrying; do not classify it as captcha and do not consume force-rotate budget. For `deductionCount` network exceptions, classify the failure as paid API outage: do not cache/use the newly fetched Shenlong IP, enter paid API cooldown, and make Panzhi/7881 wait instead of direct fallback.
- Product saved but not estimated: product `estimated/error/retry_count` → worker singleton/running queue → estimateRequested signal connection → calculator availability → DB update.
- Wrong price: original input/product text/game mapping → workbook path/version → sheet and cells → raw platform prices → chosen platform/formula/ratios → final/deal comparison.
- Frozen app only failure: compare `sys.frozen`, executable/app root, bundled data, writable paths, hidden imports and source-mode assumptions.
- UI freeze: timestamp UI stalls; measure crawler thread, DB query, timer callbacks and Excel duration; capture main-thread stack. Do not blame Qt or Excel from CPU usage alone.
- Theme/layout issue: identify native Qt vs qfluentwidgets widget, inline style vs global QSS, patch installation, theme propagation and widget lifecycle.
- 7881 detail/list captcha handling: only treat `https://netsec-img-req-cn.zijieapi.com` in the returned body as the captcha trigger, then refresh the proxy and retry once. Do not use generic keyword matching for this site.

## Useful safe commands

```powershell
rg -n "ERROR|Traceback|失败|超时|estimate|ScanScheduler" _app.log
sqlite3 data\price_monitor_v2.db ".schema"
sqlite3 data\price_monitor_v2.db "SELECT estimated, retry_count, COUNT(*) FROM products GROUP BY estimated, retry_count;"
.venv\Scripts\python.exe -m compileall -q main.py src
.venv\Scripts\python.exe .agents\skills\python-client-maintainer\scripts\project_inventory.py --check
```

Read-only queries against both candidate DB paths may be needed. Never issue UPDATE/DELETE against a real DB merely to test a theory.

## Verification matrix

- Pure parser/config/schedule/formula selection: focused deterministic unit/reproduction.
- DB CRUD/migration: temporary copied database plus schema and row assertions.
- Qt signal/lifecycle: minimal event-loop test and, when material, manual window interaction.
- Excel: configured real workbook, known input/output, thread check, and orphan-process cleanup.
- HTTP crawler/auth: captured request parity, response business semantics, retry/rate-limit behavior, and a small bounded live sample when authorized.
- Performance: same workload, timestamps and sample size before/after; report median/range rather than one anecdote.
- Build: compile/import, PyInstaller build when relevant, launch frozen artifact, verify config/DB/xlsx/icon paths.

## Reporting standard

Separate facts from hypotheses. Cite `path:line`, log timestamp, query and summarized result, or reproduction command. State what was not tested. A fix is complete only when the original failure no longer reproduces and an adjacent behavior remains intact.
