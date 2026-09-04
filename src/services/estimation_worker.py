"""
异步估价 Worker（QThread + 线程池并发）

职责：
  1. 监听内存队列（callback 刚推入的 product_db_id）
  2. 兜底：定时扫 DB estimated=0
  3. 对每个 product 调 ScanTaskService.estimate_one（在线程池里并发跑）
  4. 发出 estimationStarted / estimationFinished 信号 → UI 刷新

关键改动（相对旧版）：
  - 旧版因为 Excel COM 有线程亲和，只能「worker 发 estimateRequested →
    主线程调 calc」，主线程每次 calc 阻塞 1~3s → GUI 卡顿，还得 1.5s 限流。
  - 现在用纯 Python 公式引擎（formulas），无 COM 约束，可直接在后台线程池
    并发估价，彻底移除主线程阻塞 + 限流。
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Set

from PySide6.QtCore import QObject, QThread, Signal, Slot

from services.scan_task_service import ScanTaskService

logger = logging.getLogger(__name__)

# 线程池规模：公式引擎 calc 受 GIL 限制不会真并行加速，
# 但能重叠 DB I/O；取 3 个 worker 足够，避免 sqlite 写锁争抢。
MAX_WORKERS = 3
# 同时 in-flight 的商品数上限（防止把待估价商品一次全塞进池里重复提交）
MAX_INFLIGHT = 8


class EstimationWorker(QObject):
    """异步估价 Worker

    信号（跨线程自动 queued 到主线程槽）：
      estimationStarted(int, str, str, str)      — 开始估价 (db_id, product_id, platform, game_type)
      estimationFinished(int, str, str, str, bool) — 完成估价 (..., success)
      productEstimated(int, float, bool)          — 兼容旧 3 元签名
      workerStarted() / workerStopped()
    """
    estimationStarted = Signal(int, str, str, str)
    estimationFinished = Signal(int, str, str, str, bool)
    productEstimated = Signal(int, float, bool)
    workerStarted = Signal()
    workerStopped = Signal()

    def __init__(self, db, calc, parent=None,
                 max_workers: int = MAX_WORKERS,
                 max_inflight: int = MAX_INFLIGHT):
        super().__init__(parent)
        self.db = db
        self.calc = calc
        self.service = ScanTaskService(db, calc)
        self._thread: Optional[QThread] = None
        self._stop = False
        # 内存队列：callback 推入的 product_db_id（跨线程写，需锁）
        self._queue: Set[int] = set()
        self._lock = threading.Lock()
        # 已提交到线程池、尚未完成的 db_id（防止重复提交）
        self._inflight: Set[int] = set()
        self._max_inflight = max_inflight
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def enqueue(self, product_ids):
        """api_server / 页面调用：把刚落的商品 id 推入队列（线程安全）"""
        with self._lock:
            for pid in product_ids:
                self._queue.add(int(pid))

    def start(self):
        """启动后台线程"""
        if self._thread and self._thread.isRunning():
            logger.info("[EstimationWorker] 已在运行")
            return
        self._stop = False
        self._thread = QThread()
        self.moveToThread(self._thread)
        self._thread.started.connect(self._run)
        self._thread.start()
        self.workerStarted.emit()
        logger.info("[EstimationWorker] 已启动")

    def stop(self):
        """停止后台线程 + 关闭线程池"""
        self._stop = True
        if self._thread:
            self._thread.quit()
            self._thread.wait(2000)
        try:
            # ★ 必须 wait=True：wait=False 会让 non-daemon 的估价线程在
            # pool.apply() 阻塞时，被后续 close_formula_engine() 的 terminate()
            # 永久卡死（apply 既不返回也不抛异常），导致进程退不出。
            self._executor.shutdown(wait=True, cancel_futures=True)
        except Exception:
            pass
        self.workerStopped.emit()
        logger.info("[EstimationWorker] 已停止")

    # ---------------- 主循环 ----------------

    @Slot()
    def _run(self):
        """主循环：每 0.3s 拉队列 + 兜底扫 DB，提交到线程池并发估价"""
        logger.info("[EstimationWorker] 主循环启动")
        while not self._stop:
            try:
                with self._lock:
                    slots = self._max_inflight - len(self._inflight)
                if slots > 0:
                    # 1) 内存队列（callback 刚落库的）
                    with self._lock:
                        qids = list(self._queue)
                        self._queue.clear()
                    # 2) 兜底扫 DB（estimated=0 / 待重试）
                    dbids = []
                    if len(qids) < slots:
                        rows = self.db.get_pending_products(limit=slots * 2)
                        dbids = [int(p['id']) for p in rows]

                    submitted = 0
                    for did in qids + dbids:
                        if submitted >= slots:
                            break
                        if self._submit(did):
                            submitted += 1
            except Exception as e:
                logger.error(f"[EstimationWorker] 主循环异常: {e}")
            time.sleep(0.3)
        logger.info("[EstimationWorker] 主循环退出")

    def _submit(self, db_id: int) -> bool:
        """提交一个商品到线程池（去重 in-flight），返回是否真的提交"""
        db_id = int(db_id)
        with self._lock:
            if db_id in self._inflight or self._stop:
                return False
            self._inflight.add(db_id)
        fut = self._executor.submit(self._process_one, db_id)
        fut.add_done_callback(lambda _f, did=db_id: self._reap(did))
        return True

    def _reap(self, db_id: int):
        with self._lock:
            self._inflight.discard(db_id)

    # ---------------- 单商品估价（池线程里跑） ----------------

    def _process_one(self, db_id: int):
        """取产品 → 发 started → 估价 → 重读 → 发 finished"""
        product = None
        try:
            product = self.db.get_product_by_id_new(db_id)
            if not product:
                return
            product_id = str(product.get('product_id', '') or '')
            platform = str(product.get('crawled_platform', '') or '')
            game_type = str(product.get('game_type', '') or '')

            self.estimationStarted.emit(int(db_id), product_id, platform, game_type)

            self.service.estimate_one(product)

            # 重读 DB 拿最新 final_price / is_deal（estimate_one 已落库）
            fresh = self.db.get_product_by_id_new(db_id)
            if fresh is None:
                fresh = product
            final_price = float(fresh.get('final_price', 0) or 0)
            is_deal = bool(fresh.get('is_deal', 0))

            self.productEstimated.emit(int(db_id), final_price, is_deal)
            self.estimationFinished.emit(
                int(db_id), product_id, platform, game_type, final_price > 0,
            )
        except Exception as e:
            logger.error(f"[EstimationWorker] 估价 product#{db_id} 失败: {e}", exc_info=True)
            # ★ 失败也要发 finished，避免进度卡永久卡住这条产品
            try:
                if product is not None:
                    self.estimationFinished.emit(
                        int(db_id),
                        str(product.get('product_id', '') or ''),
                        str(product.get('crawled_platform', '') or ''),
                        str(product.get('game_type', '') or ''),
                        False,
                    )
                else:
                    self.estimationFinished.emit(int(db_id), '', '', '', False)
            except Exception:
                pass


# 全局单例（api_server 和 main_window 都引）
_estimation_worker: Optional[EstimationWorker] = None


def init_estimation_worker(db, calc) -> EstimationWorker:
    """初始化全局 Worker 单例

    - 第一次调用：创建 + 启动
    - 已存在但 calc 变了：自动 stop 旧实例 + 建新实例（用于测试切换 mock calc）
    - 已存在且 calc 相同：复用
    """
    global _estimation_worker
    if _estimation_worker is None:
        _estimation_worker = EstimationWorker(db, calc)
        _estimation_worker.start()
        return _estimation_worker
    # calc 变了 → 重建（用于测试切 mock）
    if _estimation_worker.service.calc is not calc:
        try:
            _estimation_worker.stop()
        except Exception:
            pass
        _estimation_worker = EstimationWorker(db, calc)
        _estimation_worker.start()
    return _estimation_worker


def get_estimation_worker() -> Optional[EstimationWorker]:
    return _estimation_worker
