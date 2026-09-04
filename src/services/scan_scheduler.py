"""
扫号任务调度器（单 QTimer 30s tick）

双维度判定：
  1. 调度维度（什么时候跑）：全天 / 时间窗口
  2. 扫描维度（扫多少）：全量扫描（跑 1 次）/ 最新数据扫描（按 interval 周期）

4 种组合：
  - always + full   ：24h 不停地全量
  - always + latest ：24h 每 interval 跑一次
  - window + full   ：窗口内跑 1 次
  - window + latest ：窗口内每 interval 跑一次
"""
import logging
import time
from datetime import datetime
from typing import Dict, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.scan_task_service import ScanTaskService

logger = logging.getLogger(__name__)


class ScanScheduler(QObject):
    """调度器"""
    taskExecuted = Signal(int)  # task_id（UI 用）
    schedulerStarted = Signal()
    schedulerStopped = Signal()
    tickFinished = Signal()     # 每个 tick 结束发（UI 刷新"下次执行"列用）

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._timer = QTimer(self)
        self._timer.setInterval(30_000)  # 30s tick
        self._timer.timeout.connect(self._tick)
        self._last_run: Dict[int, datetime] = {}  # task_id → last run time
        self._tasks = []
        self._tick_count = 0

    def start(self):
        """启动调度器"""
        self.reload_tasks()
        self._timer.start()
        self.schedulerStarted.emit()
        logger.info("[ScanScheduler] 已启动（30s tick），共 %d 个任务", len(self._tasks))

    def stop(self):
        """停止调度器"""
        self._timer.stop()
        self.schedulerStopped.emit()
        logger.info("[ScanScheduler] 已停止，共执行 %d 次 tick", self._tick_count)

    def reload_tasks(self):
        """从 DB 重新加载所有启用的任务"""
        self._tasks = self.db.get_enabled_scan_tasks() if hasattr(self.db, 'get_enabled_scan_tasks') else []
        logger.info(
            "[ScanScheduler] 重新加载任务: 共 %d 个启用",
            len(self._tasks),
        )
        for t in self._tasks:
            last = t.get('last_run_at')
            logger.debug(
                "[ScanScheduler]   任务 #%d '%s' platform=%s scan_mode=%s interval=%dmin last_run=%s",
                t.get('id'), t.get('task_name', '?'),
                t.get('platforms', '?'), t.get('scan_mode', 'latest'),
                t.get('interval_minutes', 10),
                last if last else '从未执行',
            )

    def _tick(self):
        """每 30s 检查：哪些任务该执行"""
        self._tick_count += 1
        now = datetime.now()
        triggered = 0
        skipped = 0

        for task in self._tasks:
            try:
                should_run, reason = self._should_run(task, now)
                if should_run:
                    logger.info(
                        "[ScanScheduler] ▶ 触发任务 #%d '%s'（原因: %s）",
                        task.get('id'), task.get('task_name', '?'), reason,
                    )
                    self._execute(task)
                    self._last_run[task['id']] = now
                    triggered += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(
                    "[ScanScheduler] 任务 #%d tick 异常: %s",
                    task.get('id'), e, exc_info=True,
                )

        if triggered > 0 or self._tick_count <= 2:
            logger.info(
                "[ScanScheduler] tick #%d 完成: 触发 %d, 跳过 %d",
                self._tick_count, triggered, skipped,
            )
        self.tickFinished.emit()

    def _should_run(self, task: dict, now: datetime):
        """双维度判定：是否应该执行

        Returns:
            (should_run: bool, reason: str)
        """
        task_id = task.get('id')
        task_name = task.get('task_name', '?')

        if not task.get('enabled'):
            return False, "未启用"

        # ===== 维度 1：调度判定（什么时候跑）=====
        schedule_mode = task.get('schedule_mode', 'always') or 'always'
        if not ScanTaskService.in_window(
            now.time(),
            schedule_mode,
            task.get('window_start'),
            task.get('window_end'),
        ):
            return False, f"不在时间窗口内（mode={schedule_mode}）"

        # ===== 维度 2：扫描模式 =====
        scan_mode = task.get('scan_mode', 'latest') or 'latest'

        if scan_mode == 'full':
            # 全量扫描：只跑一次（last_run_at 有值就跳过）
            if task.get('last_run_at'):
                return False, "全量模式已完成（last_run_at 已设置）"
            return True, "全量模式首次执行"

        # latest：按 interval 调度
        last = self._last_run.get(task['id'])
        if last is None:
            # 重启后内存为空，用 DB 里的 last_run_at 兜底，避免所有旧任务立刻跑一遍
            last_str = task.get('last_run_at')
            if last_str:
                try:
                    last = datetime.fromisoformat(last_str)
                except (ValueError, TypeError):
                    pass
        interval = int(task.get('interval_minutes', 10) or 10)
        if last:
            elapsed = (now - last).total_seconds()
            if elapsed < interval * 60:
                remaining = int(interval * 60 - elapsed)
                return False, f"距上次执行 {elapsed:.0f}s，还需等待 {remaining}s（间隔 {interval}min）"
            return True, f"距上次执行 {elapsed:.0f}s，已超过间隔 {interval}min"
        return True, "首次执行（无历史记录）"

    def _execute(self, task: dict):
        """触发执行：调所有已注册的爬虫函数（直接嵌套，不走 HTTP）"""
        task_id = task['id']
        task_name = task.get('task_name', '?')
        payload = {
            "task_id": task_id,
            "task_name": task_name,
            "platforms": task.get('platforms', []),
            "games": task.get('games', []),
            "scan_mode": task.get('scan_mode', 'latest'),
            "scan_limit": task.get('scan_limit', 50) if task.get('scan_mode') == 'latest' else 0,
            "pricing": {
                "secondary_real_name_ratio": task.get('secondary_real_name_ratio', 0) or 0,
                "platform_bargain_ratio": task.get('platform_bargain_ratio', 0) or 0,
            },
        }

        # 调所有已注册的爬虫函数（在子线程跑，避免阻塞 30s tick）
        from services.scanner_registry import get_scanners
        scanners = get_scanners()
        if not scanners:
            logger.warning(
                "[ScanScheduler] 任务 #%d '%s': 没有注册任何爬虫函数！"
                "请检查 main_window.py 中爬虫模块的 import 是否成功",
                task_id, task_name,
            )
        else:
            import threading
            def _run_in_thread():
                t_start = time.monotonic()
                logger.info(
                    "[ScanScheduler] 任务 #%d '%s': 爬虫线程启动，共 %d 个爬虫函数",
                    task_id, task_name, len(scanners),
                )
                try:
                    from services.scanner_registry import run_scanners
                    run_count = run_scanners(task_id, payload)
                    elapsed = time.monotonic() - t_start
                    logger.info(
                        "[ScanScheduler] 任务 #%d '%s': 爬虫线程完成，%d 个爬虫执行成功，耗时 %.1fs",
                        task_id, task_name, run_count, elapsed,
                    )
                except Exception as e:
                    elapsed = time.monotonic() - t_start
                    logger.error(
                        "[ScanScheduler] 任务 #%d '%s': 爬虫线程异常，耗时 %.1fs: %s",
                        task_id, task_name, elapsed, e, exc_info=True,
                    )

            threading.Thread(
                target=_run_in_thread, daemon=True,
                name=f"Scanner-{task_id}-{task_name[:8]}",
            ).start()
            logger.info(
                "[ScanScheduler] 任务 #%d '%s': 已启动 daemon 线程（platforms=%s games=%s mode=%s limit=%d）",
                task_id, task_name,
                payload['platforms'], payload['games'],
                payload['scan_mode'], payload['scan_limit'],
            )

        # 同时更新 DB 状态
        self.db.update_scan_task(task_id, last_run_at=datetime.now().isoformat())
        logger.debug("[ScanScheduler] 任务 #%d: last_run_at 已更新", task_id)
        # 通知 UI
        self.taskExecuted.emit(task_id)


# 全局单例
_scan_scheduler: Optional[ScanScheduler] = None


def init_scan_scheduler(db) -> ScanScheduler:
    global _scan_scheduler
    if _scan_scheduler is None:
        _scan_scheduler = ScanScheduler(db)
        _scan_scheduler.start()
    return _scan_scheduler


def get_scan_scheduler() -> Optional[ScanScheduler]:
    return _scan_scheduler
