"""
扫号任务业务编排

职责：
  1. 议价公式选择（按 supports_realname 分流）
  2. 去重查（按 product_id + original_price）
  3. 调度判定（窗口 + 间隔）
  4. 单商品估价流程编排
"""
import logging
from typing import Dict, Tuple, Optional

logger = logging.getLogger(__name__)


class ScanTaskService:
    """扫号任务业务编排（无 Qt 依赖，可纯 Python 测）"""

    def __init__(self, db, calc):
        """
        Args:
            db: DBManagerV2 实例
            calc: ExcelCalculatorFinal 实例
        """
        self.db = db
        self.calc = calc

    # ==================== 议价公式 ====================

    @staticmethod
    def calculate_final_price(
        target_price: float,
        secondary_real_name_ratio: float,
        platform_bargain_ratio: float,
        supports_realname: bool,
    ) -> Tuple[float, str]:
        """议价公式（按 supports_realname 分流）

        二次实名（true）：
            final = target × (1 + 二次实名加价比 + 平台议价比例)
        非二次实名（false）：
            final = target × (1 + 平台议价比例)

        Returns:
            (final_price, formula_tag)
        """
        if supports_realname:
            multiplier = 1.0 + secondary_real_name_ratio + platform_bargain_ratio
            tag = "secondary"
        else:
            multiplier = 1.0 + platform_bargain_ratio
            tag = "platform_only"
        return round(target_price * multiplier, 2), tag

    # ==================== 调度判定 ====================

    @staticmethod
    def in_window(now_time, schedule_mode: str,
                  window_start: Optional[str], window_end: Optional[str]) -> bool:
        """时间窗口判定（含跨天）

        Args:
            now_time: datetime.time
            schedule_mode: 'always' / 'window'
            window_start: 'HH:MM'
            window_end: 'HH:MM'
        """
        if schedule_mode == 'always' or not schedule_mode:
            return True
        if not window_start or not window_end:
            return True
        try:
            h1, m1 = window_start.split(':')
            h2, m2 = window_end.split(':')
            from datetime import time
            s = time(int(h1), int(m1))
            e = time(int(h2), int(m2))
        except Exception:
            return True

        cur = now_time
        if s <= e:
            # 不跨天
            return s <= cur < e
        else:
            # 跨天（如 22:00-06:00）
            return cur >= s or cur < e

    # ==================== 去重 ====================

    def is_duplicate(self, product_id: str, original_price: float) -> bool:
        """查 DB：product_id 存在且价格未变 → 重复估价"""
        if not product_id:
            return False
        last = self.db.get_last_estimated(product_id)
        if not last:
            return False
        try:
            return abs(float(last.get('original_price', 0)) - float(original_price)) < 0.001
        except (TypeError, ValueError):
            return False

    # ==================== 单商品估价流程 ====================

    def estimate_one(self, product: Dict) -> bool:
        """对一个 product 做完整估价

        Args:
            product: DB 行 dict（必含 id / product_id / product_info / game_type
                    / crawled_platform / original_price / supports_realname / task_id）

        Returns:
            True = 成功（含去重跳过）
        """
        db_id = product.get('id')
        product_id = product.get('product_id', '')
        original_price = float(product.get('original_price', 0) or 0)

        # 1. 取出 task 配置
        task_id = product.get('task_id')
        task = self.db.get_scan_task(task_id) if task_id else None
        if not task:
            self.db.update_product_estimated(
                product_db_id=db_id,
                prices={}, final_price=0,
                pricing_formula='', is_deal=False,
                estimated_error=f"task {task_id} 不存在"
            )
            return False

        secondary_ratio = float(task.get('secondary_real_name_ratio', 0) or 0)
        bargain_ratio = float(task.get('platform_bargain_ratio', 0) or 0)
        supports_realname = bool(product.get('supports_realname', 0))

        # 2. 去重：同 product_id + 同价格 + 上次**成功**估价（final_price>0）→ 跳过
        #    注意：上次 final_price=0 视为"估价失败/无结果"，不算跳过，必须重试
        last = self.db.get_last_estimated(product_id)
        if (last
            and float(last.get('final_price', 0) or 0) > 0
            and abs(float(last.get('original_price', 0)) - original_price) < 0.001):
            # 重复估价：复制上次的平台价格 + 议价结果，更新时间戳
            import json as _json
            _last_prices = {}
            try:
                _pj = last.get('prices_json', '') or ''
                if _pj:
                    _last_prices = _json.loads(_pj) or {}
            except Exception:
                pass
            self.db.update_product_estimated(
                product_db_id=db_id,
                prices=_last_prices,
                final_price=float(last.get('final_price', 0) or 0),
                pricing_formula=last.get('pricing_formula', '') or '',
                is_deal=bool(last.get('is_deal', 0)),
                estimated_error=None,
            )
            logger.info(f"[scan_task_service] 跳过重复估价: product_id={product_id}")
            return True

        # 3. 调 Excel 算 4 平台价
        # 读当前 retry_count（用于失败时判断是否到上限）
        current_retry = int(product.get('retry_count', 0) or 0)

        # ★ 每次都从全局拿最新公式引擎——避免 worker 持过期引用（换表后旧引用失效）
        try:
            from excel.formula_engine import get_current_formula_engine
            live_calc = get_current_formula_engine()
            if live_calc is None:
                logger.warning("[scan_task_service] 公式引擎未初始化，跳过本次估价")
                return False
            self.calc = live_calc  # 更新 service 引用（兼容性）
        except Exception as e:
            logger.error(f"[scan_task_service] 获取公式引擎失败: {e}")
            return False

        try:
            res = self.calc.calculate_price(
                product.get('product_info', ''),
                product.get('game_type', ''),
                product_id=str(product.get('product_id', '') or ''),
            )
        except Exception as e:
            # Excel 异常也走 retry 机制
            if current_retry >= 2:
                self.db.update_product_estimated(
                    product_db_id=db_id,
                    prices={}, final_price=0,
                    pricing_formula='', is_deal=False,
                    estimated_error=f'多次重试仍异常: {e}',
                    force_estimated=2,  # 标记为永久失败
                )
            else:
                self.db.update_product_estimated(
                    product_db_id=db_id,
                    prices={}, final_price=0,
                    pricing_formula='', is_deal=False,
                )
            return False

        if not res or not res.get('success'):
            err = res.get('message', 'Excel 计算失败') if isinstance(res, dict) else 'Excel 计算失败'
            # 失败也走 retry 机制
            if current_retry >= 2:
                self.db.update_product_estimated(
                    product_db_id=db_id,
                    prices={}, final_price=0,
                    pricing_formula='', is_deal=False,
                    estimated_error=f'多次重试仍失败: {err}',
                    force_estimated=2,  # 标记为永久失败
                )
            else:
                self.db.update_product_estimated(
                    product_db_id=db_id,
                    prices={}, final_price=0,
                    pricing_formula='', is_deal=False,
                )
            return False

        prices = res.get('prices', {}) or {}

        # 4. 挑回调平台那列价格
        crawled_platform = product.get('crawled_platform', '')
        target_price = float(prices.get(crawled_platform, 0) or 0)

        # 5. 议价公式
        final_price, formula = self.calculate_final_price(
            target_price=target_price,
            secondary_real_name_ratio=secondary_ratio,
            platform_bargain_ratio=bargain_ratio,
            supports_realname=supports_realname,
        )

        # 6. 捡漏判断（"捡漏" = 系统估价高于卖家挂的价 → 有正溢价 → 值得买）
        #    用户定义: 溢价 = final - original，溢价 > 0 才是捡漏商品
        is_deal = final_price > original_price > 0 and final_price > 0

        # 7. 落库
        # 7.1 重试机制：如果 final_price=0 → 走重试分支
        #     当 retry_count >= 2（即第 3 次重试）时强制标 estimated=2 (永久失败)
        #     阻止 worker 兜底再扫到（避免无限循环）
        if final_price == 0:
            # 看当前 retry_count（这次写入后变 N+1）
            current_retry = int(product.get('retry_count', 0) or 0)
            if current_retry >= 2:
                # 第 3 次仍无结果 → 强制标永久失败
                self.db.update_product_estimated(
                    product_db_id=db_id,
                    prices=prices,
                    final_price=0,
                    pricing_formula='',
                    is_deal=False,
                    estimated_error='多次重试仍无结果（数据问题或公式不匹配）',
                    force_estimated=2,  # ★ 关键：让 update_product_estimated 走 estimated=2 分支
                )
                logger.warning(
                    f"[scan_task_service] 估价重试 {current_retry + 1} 次仍无结果, "
                    f"标永久失败: product_id={product_id}"
                )
                return False
            # 还有重试机会：写 estimated=1 + final=0 + 无 error → worker 还会重试
            self.db.update_product_estimated(
                product_db_id=db_id,
                prices=prices,
                final_price=final_price,
                pricing_formula=formula,
                is_deal=is_deal,
            )
            logger.info(
                f"[scan_task_service] 估价无结果, 标记重试 #{current_retry + 1}/3: "
                f"product_id={product_id}"
            )
            return False  # 算"待重试"（UI 显示待估价）

        # 7.2 正常情况（final_price>0 或 prices 有内容）
        self.db.update_product_estimated(
            product_db_id=db_id,
            prices=prices,
            final_price=final_price,
            pricing_formula=formula,
            is_deal=is_deal,
        )

        # 8. 捡漏推送钉钉
        # if is_deal:
        #     try:
        #         from services.dingtalk_notify import notify_deal
        #         notify_deal(
        #             product_id=str(product_id),
        #             game_type=product.get("game_type", ""),
        #             platform=crawled_platform,
        #             original_price=original_price,
        #             final_price=final_price,
        #             url=product.get("url", ""),
        #             supports_realname=supports_realname,
        #             target_price=target_price,
        #             secondary_pct=round(secondary_ratio * 100, 1),
        #             bargain_pct=round(bargain_ratio * 100, 1),
        #         )
        #     except Exception:
        #         logger.exception("[scan_task_service] 钉钉推送异常")

        return True
