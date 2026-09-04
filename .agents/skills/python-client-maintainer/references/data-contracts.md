# Data contracts

## SQLite

The active database path is resolved by `DBManagerV2`; confirm it at runtime because both `data/price_monitor_v2.db` and `src/database/price_monitor_v2.db` exist.

`scan_tasks` stores task identity; JSON platform/game lists; enabled flag; `always|window` schedule with HH:MM bounds and interval; `full|latest` mode and limit; two pricing ratios; status and run timestamps.

`products` stores task ID, external product ID, product text, seller price, URL, game/platform, real-name capability, estimation state/time/error/retry count, Excel price JSON, final price, pricing formula, deal flag and creation time. Indexes support product deduplication, pending estimation, and task lookup.

Always inspect `.schema` and the selected DB before migration. Use copied/temp DBs for write experiments.

## Scanner result

Each result passed to `submit_scan_results` requires a stable `product_id` and numeric `original_price`. Expected optional values include `product_info`, `url`, `game_type`, and `supports_realname`. `crawled_platform` comes from the submission's `platform`. Preserve external ID normalization and do not deduplicate on display text.

7881 list/detail prices are treated as yuan values as provided by the upstream payload/page. Do not divide integer 7881 prices by 100 during normalization.

Startup DB repair may multiply legacy `products.original_price` by 100 only for `crawled_platform='7881'` rows where `0 < original_price < 100`, then recompute `is_deal` for those rows. Do not apply this repair to all platforms because other marketplaces can have genuine one- or two-digit yuan prices.

## Task payload

Scheduler/scanner payloads include platforms, games, schedule mode/window/interval, scan mode/limit, and pricing ratios. Use `DBManagerV2._decode_task_field` behavior when inspecting stored JSON strings.

Panzhi scanner resolves `browser_context` from the task payload first. When the payload does not carry it, the scanner falls back to `config/panzhi_browser_context.json`, or to the path in `PANZHI_BROWSER_CONTEXT_PATH` if that environment variable is set.

Panzhi list requests use the source-project shape of `pageSize=10` with `countFlag=true`; keep that body shape aligned when changing pagination defaults.

## Dynamic game/platform configuration

`config/game_platform.json` is the simplified user-facing format. `ConfigManager` expands and normalizes it and maintains game IDs, platform IDs, platform-game mappings, sheet names and cell mappings. Current seed data has game `火影忍者`, platform `螃蟹`, and formula cells for `盼之`, `螃蟹`, `7881`, and `氪金兽`. Never assume the seed list is the runtime list.

## API/auth configuration

`config/active.json` selects `dev` or `prod`; optional active-file URL/timeout overrides may supersede the selected environment. Environment files provide name, base URL, timeout, branding and logo. Session code normalizes second/millisecond expiry values and persists credentials outside ordinary source flow. Redact tokens and passwords in diagnostics.

## Proxy configuration

`config/proxy.json` controls enabled state, provider, explicit proxy, cache DB/TTL, API timeout, Shenlong endpoint/credentials, paid API base/function name, force-rotate budget, and provider backoff settings. The crawler merges defaults with this file and caches acquired proxies in SQLite. Treat endpoint query values and credentials as secrets even if placeholders appear in the repository.

The paid proxy API currently calls `/open/apipaid/function/checkOpen` and `/open/apipaid/function/deductionCount` with the login `token` header, `name=扫号器代理`, and `type=2` (`function_type` in `config/proxy.json`, paid-function type "全平台"). If the service returns HTTP 403 with `msg="missing signature headers"`, classify it as `MISSING_SIGNATURE_HEADERS`: the network and route are reachable, but the backend gateway/interceptor is rejecting the token-only request. Do not report this as an unopened package, and do not trigger the 205 paywall.

`checkOpen` only latches proxy direct-mode fallback when it returns the confirmed business result `NOT_OPENED`. Transient outcomes such as `NO_TOKEN`, `NETWORK_ERROR`, `HTTP_xxx`, `INVALID_BODY`, `BUSINESS_xxx`, or `MISSING_SIGNATURE_HEADERS` must not mark the platform as permanently unopened; later tasks are expected to retry `checkOpen`.

Proxy force-rotate budget is only for active captcha/risk IP switches on 7881 and Panzhi. Pangxie/Kejinshou use TTL refresh only. Panzhi list/detail captcha handling first consumes one force-rotate slot to switch IP, then refreshes WAF runtime state (bootstrap/acw/ssxmod) without consuming additional proxy budget before retrying. Panzhi WAF bootstrap captcha failures must not call `force_rotate` because that path returns immediately and would consume budget/deduction without using the new proxy.

Panzhi treats proxy connection refusal/unreachable errors (`ProxyError`, curl `(7)`, `WinError 10061`, "Failed to connect", or "Unable to connect to proxy") as a bad proxy endpoint, not as site captcha. The cached Panzhi proxy is invalidated without consuming `force_rotate` budget, then the crawler waits the configured proxy TTL before fetching a new proxy and continuing the current list/detail retry path.

When `deductionCount` itself fails with a network exception, `ProxyManager` treats it as a paid API outage. The newly fetched Shenlong IP is not cached or used, the paid API enters `paid.network_cooldown_seconds` cooldown (defaulting to the proxy TTL), and further `get_proxy`/`force_rotate` calls do not call Shenlong or consume captcha budget during that cooldown. Panzhi and 7881 wait for the cooldown before retrying proxy acquisition instead of falling back to direct requests.

`reset_managers()` clears both per-platform `ProxyManager` singletons and the global `ProxyPaywall` state, so a fresh login starts from a clean proxy/paywall state.

## Kejinshou H5 contract

Kejinshou list/detail calls use signed H5 GET query parameters for `mwp.kjs_search.product.search/1.0` and `mwp.kjs_product.product.detail/1.0`. The request profile tracks the current web capture (`h5-nuxt/3.41.10`, Chrome 150, `mw-device=undefined/1920/1080`, and `content-type: application/x-www-form-urlencoded`). If a response has `ret="FAIL_SYS_TOKEN_NEED_RENEW"` with `token` and `encToken`, the crawler updates its in-memory `h5_token`/`h5_token_enc`, rebuilds the signed query, and retries; do not parse that token-renew response as an empty product list.

## Excel contract

The configured workbook filename is stored in `QSettings("PriceMonitor", "PriceMonitorClient")` under `excel/filename` and resolved relative to the app/executable root. `ConfigManager` supplies game sheet and platform output cells. Excel COM formula evaluation is the source of platform prices; a Python mock cannot validate formula correctness. Preserve workbook binaries and make backups before any protection rewrite.

Panzhi crawler WAF state is loaded from `config/waf_state.json` and falls back to `config/waf_state.json.bak2` when the canonical file is absent. The saved state must still contain the same WAF keys and cookies.

## Dormant Flask routes

- `POST /api/scanner/callback`: scanner ingestion compatibility path.
- `GET /api/deals`: deal query.
- `GET /api/history`: product history query.
- `GET /api/health`: health response.

Do not change these contracts unless an active consumer is identified or the user explicitly requests the dormant API.

