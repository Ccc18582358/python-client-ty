# Architecture

## Runtime shape

This is a Windows desktop application built with PySide6 and PySide6-Fluent-Widgets. `main.py` owns one `QApplication` and loops between `LoginWindow` and `MainWindow`. Closing after logout clears the session and returns to login; ordinary close exits.

The main window owns one `DBManagerV2`, starts the Excel calculator singleton, estimation worker, scan scheduler, and imports `services.jingxi_scanner` to register the production scanner. It exposes four pages: scan tasks, deal products, history, and manual price calculation.

## Primary data flow

```text
ScanScheduler (GUI-thread QTimer, 30 s)
  -> scanner_registry.run_scanners
  -> daemon scanner thread
  -> jingxi_scanner
  -> JingxiAccountCrawler async list/detail pipeline
  -> scanner_service.submit_scan_results
  -> DBManagerV2.save_product_pending (estimated=0)
  -> EstimationWorker queue on QThread
  -> estimateRequested(db_id), queued to MainWindow
  -> ScanTaskService.estimate_one
  -> ExcelCalculatorFinal.calculate_price
  -> DBManagerV2.update_product_estimated
  -> MainWindow estimation signals / global estimation_bus
  -> EstimationProgressCard, DealPage, HistoryPage refresh
```

## Thread and lifecycle boundaries

- Qt widgets, window lifecycle, `ScanScheduler`'s `QTimer`, and `MainWindow._on_estimate_requested` run on the main thread.
- Each due scan launches a daemon Python thread; the crawler uses asyncio/httpx inside that scan path.
- `EstimationWorker` lives on a `QThread`, but requests the actual calculation through a Qt signal. Excel COM is called by `MainWindow` on the thread that owns the calculator.
- The Excel calculator is a module singleton shared by the main window and manual calculation page. Shutdown must close the workbook/application and stop background services.
- SQLite connections are opened per operation by `DBManagerV2`; do not share cursors across threads.

## Layer ownership

- `src/api`: login captcha/auth, persisted session, and a deprecated/not-started Flask callback API.
- `src/config`: environment/branding config plus dynamic game/platform/cell mappings.
- `src/database`: schema, migrations, task CRUD, product pending/estimated state, filters, cleanup.
- `src/services`: scheduling, scanner registry, pxb7 crawler, persistence handoff, and estimation orchestration.
- `src/excel`: xlsx protection helpers and Windows Excel COM calculation/singleton management.
- `src/gui`: login/main windows, four pages, reusable widgets, theme propagation.
- `build.py`, `.spec`: PyInstaller and installer staging; frozen path behavior differs from source mode.

## Known architectural caveats

- `src/api/api_server.py` defines Flask routes but `main.py` does not start this server; do not assume it handles current scanner ingestion.
- Existing docs may describe older demo-scanner or worker timing behavior. Confirm against current source and generated inventory.
- Excel calculation on the UI thread can affect responsiveness. Any redesign must preserve COM apartment correctness and be proven with timing evidence.

