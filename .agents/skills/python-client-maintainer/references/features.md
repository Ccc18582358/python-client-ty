# Feature map

## Authentication and application lifecycle

- Captcha fetch and username/password login: `src/api/auth_client.py`, `src/gui/login_window.py`.
- Remembered UI credentials/preferences: login window writes `config/ui_prefs.json`; treat contents as sensitive.
- Token, expiry normalization, persistence, auth header, logout: `src/api/session.py` and the session config path it resolves.
- Environment, base URL, timeout, and optional brand name/logo: `src/config/api_config.py`, selected by `config/active.json` from `dev.json` or `prod.json`. The shipped defaults use the neutral `账号估价助手` name and code-drawn UI mark, so no customer logo is shown.
- Login → main window → logout/login loop: `main.py`.

## Main window and navigation

- Four navigation pages, user card, business configuration, theme toggle, logout: `src/gui/main_window.py`. Navigation uses neutral labels (`扫描任务`, `优选商品`) and the default Fluent user avatar.
- Global shortcuts: F5 refresh, Ctrl+F filters, Ctrl+1..4 page switching.
- Global theme, native-widget QSS, fluent label/table patches and recursive propagation: `src/gui/theme.py` and `src/gui/widgets/*theme*`, `global_label_qss.py`, `fluent_color_patch.py`.
- Game/platform add, rename, enable, reorder, delete and reset: `game_platform_manager.py` backed by `ConfigManager`.

## Scan task management

- Paginated task list, selection, run-now, enable/disable, create/edit/delete/batch delete, and next-run display: `src/gui/pages/scan_task_page.py`.
- Task editor controls platforms, games, always/window schedule, interval, full/latest scan, scan limit, secondary-real-name ratio, and platform bargain ratio.
- Thirty-second due-task checks and threaded execution: `src/services/scan_scheduler.py`.
- Scanner plugin registration and fan-out: `scanner_registry.py`; production registration occurs by importing `jingxi_scanner.py`.
- Per-task cancellation uses stop events in `jingxi_scanner.py`.

## Jingxi/Pangxie crawling

- List and detail endpoints, async producer/consumer pipeline, concurrency, rate limit, jitter, retries, pagination, risk pauses, proxy handling, attribute extraction and normalized output: `src/services/jingxi_spider_pangxie.py`.
- Supports full scans and latest-N scans. Game/platform mappings come from `ConfigManager`, not hard-coded UI labels alone.
- `scanner_service.submit_scan_results` validates/persists normalized results and enqueues returned database IDs for estimation.
- Panzhi crawler and WAF helpers: `src/services/jingxi_spider_panzhi.py`, `src/services/panzhi_folder/waf_cookie_generator.py`, `src/services/panzhi_folder/panzhi_waf_cookie_generator.py`, `src/services/panzhi_folder/decode_utils.py`, and `src/services/panzhi_folder/pz_wasm_sign_runner.js`.
- `jingxi_spider_panzhi.PanzhiAccountCrawler` can seed its request context from a browser snapshot so the list-page probe/adaptor can replay real cookies, localStorage, WAF state, and browser IDs instead of regenerating defaults.
- 7881 crawler emits one red JSON line per SKU at batch submission time with `url`, `price`, `product_info` preview, and `has_valid_price` for console inspection; proxy refresh logs are de-noised to keep the crawl output readable.

## Estimation and pricing

- Pending queue and recovery of unestimated rows: `src/services/estimation_worker.py`.
- Duplicate reuse, Excel result selection, secondary-real-name/platform-bargain formulas, retry/error state and DB update: `src/services/scan_task_service.py`.
- Excel workbook copy/open, formula input/output, platform cell mapping, deal comparison, singleton and cleanup: `src/excel/excel_calculator_final.py`.
- Workbook protection inspection/removal: `src/excel/excel_utils.py`.
- Manual game/workbook/product input, result table, clear and save-to-history: `src/gui/pages/calc_page.py`.

## Product views

- Deal page: only deal rows; platform/game/price/premium/date filters, pagination, status, retry, URL/ID actions, Excel export and batch delete.
- History page: all product rows; progress card, platform/game/price/date filters, pagination, retry, URL/ID actions and batch delete.
- `DataTable`, pagination, persistent filter state, empty/loading states and estimation progress are reusable widgets under `src/gui/widgets`.
- Startup removes product records older than seven days through `DBManagerV2._cleanup_old_records()`.

## Legacy/local HTTP API

`src/api/api_server.py` contains rate-limited `/api/scanner/callback`, `/api/deals`, `/api/history`, and `/api/health`, but current application startup does not call `start_api_server()`. Treat it as dormant compatibility code unless a caller is demonstrated.

## Build and assets

- `price-monitor-system.spec` describes PyInstaller collection; `build.py` stages config/assets and installer files.
- `version.json` is build metadata. Source/frozen root resolution is used by config, database, Excel and logo paths.
- xlsx, ico/png/jpg assets and SQLite files are runtime assets, not generated source to casually rewrite.

