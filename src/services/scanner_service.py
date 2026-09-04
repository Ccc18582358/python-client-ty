"""
扫号结果落库服务（爬虫 → DB 的唯一入口）

═══════════════════════════════════════════════════════════════════
                  爬 虫 作 者 必 读
═══════════════════════════════════════════════════════════════════

【你只需要调这一个函数】

  submit_scan_results(
      task_id  = 1,                # 你的 payload 里就有
      platform = "螃蟹",            # 当前正在爬的平台名
      results  = [...],            # list[dict]，见下表
  )

【results 字段】

  ┌──────────────────┬────────┬──────────────────────────────────┐
  │ 字段             │ 必填   │ 说明                             │
  ├──────────────────┼────────┼──────────────────────────────────┤
  │ product_id       │   ✅   │ 平台给的外部 ID（去重 key）       │
  │ product_info     │   ❌   │ 商品描述（Excel 公式用）          │
  │ original_price   │   ❌   │ 原价（卖家挂价）                  │
  │ url              │   ❌   │ 商品详情页（UI 复制按钮用）        │
  │ game_type        │   ❌   │ 游戏名（火影/王者/金铲铲/...）    │
  │ supports_realname│   ❌   │ 是否二次实名（影响议价公式）      │
  └──────────────────┴────────┴──────────────────────────────────┘

【去重规则】
  同一 product_id + 同价 → 跳过，不入新库
  同一 product_id + 异价 → 更新价格 + 重新入估价队列
  product_id 为空     → 永远算新商品

【内部流程】（你不用关心）
  1. save_product_pending → DB estimated=0
  2. enqueue db_id → EstimationWorker 估价队列
  3. Worker 后台线程池并发调 estimate_one（纯 Python 公式引擎，不阻塞 UI）

═══════════════════════════════════════════════════════════════════
"""
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

# ★ 模块级 DB 单例：避免每次 submit_scan_results 都 new DBManagerV2()
_db_instance = None


def _get_db():
    global _db_instance
    if _db_instance is None:
        from database.db_manager_v2 import DBManagerV2
        _db_instance = DBManagerV2()
    return _db_instance


def submit_scan_results(*, task_id: int, platform: str, results: List[Dict[str, Any]]) -> int:
    """爬虫回调内部入口：落库 estimated=0 + 推 Worker 估价队列

    Args:
        task_id:  扫号任务 ID（payload["task_id"]）
        platform: 平台名（螃蟹/盼之/7881/氪金兽...）
        results:  爬虫结果列表，每条格式见模块顶部表格

    Returns:
        入队（已落库）的商品数

    Raises:
        ValueError: task_id/platform 不合法
    """
    db = _get_db()

    # ===== 1. 校验 =====
    if not task_id:
        raise ValueError("task_id 必填")
    if not platform:
        raise ValueError("platform 必填")
    if not results:
        logger.info(f"[scanner_service] task_id={task_id} platform={platform} 无结果，跳过")
        return 0

    task = db.get_scan_task(int(task_id))
    if not task:
        raise ValueError(f"task {task_id} 不存在")

    # ===== 2. 落库 estimated=0 =====
    pending_ids = []
    skipped_ids = []  # 同 product_id + 同价，被跳过的
    for r in results:
        try:
            db_id = db.save_product_pending(
                product_info=r.get('product_info', '') or '',
                original_price=float(r.get('original_price', 0) or 0),
                url=r.get('url', '') or '',
                game_type=r.get('game_type', '') or '',
                product_id=r.get('product_id', '') or '',
                task_id=int(task_id),
                crawled_platform=platform,
                supports_realname=bool(r.get('supports_realname', False)),
            )
            if db_id is None:
                # 同 product_id + 同价 → 跳过，不入库
                skipped_ids.append(r.get('product_id', ''))
            else:
                pending_ids.append(db_id)
        except Exception as e:
            logger.error(f"[scanner_service] 落库单条失败: {e}")

    logger.info(
        f"[scanner_service] task_id={task_id} platform={platform} "
        f"入队 {len(pending_ids)}/{len(results)} 条, 跳过同价 {len(skipped_ids)} 条"
    )

    # ===== 3. 推入 Worker 队列（异步估价）=====
    if pending_ids:
        try:
            from services.estimation_worker import get_estimation_worker
            worker = get_estimation_worker()
            if worker:
                worker.enqueue(pending_ids)
                logger.info(f"[scanner_service] 推入估价队列 {len(pending_ids)} 条")
            else:
                # Worker 没起来：商品保持 estimated=0 等下次 Worker 兜底扫
                logger.warning("[scanner_service] 估价 Worker 未启动，商品保持 estimated=0")
        except Exception as e:
            # 落库已成功，推队列失败不影响主流程
            logger.error(f"[scanner_service] 推队列失败（落库已成功）: {e}")

    return len(pending_ids)
