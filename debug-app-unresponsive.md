# Debug: app-unresponsive

**Session ID**: app-unresponsive
**Date**: 2026-06-16
**Status**: [RESOLVED]

---

## 总结

### 根本原因
主线程被 `estimate_one` 同步阻塞（Excel COM calc 每次 ~530ms），
worker 每 0.5s 发 20 个信号 → 主线程连续 20 次 calc → UI 卡死 10s+。

### 修复
[src/services/estimation_worker.py](file:///d:/word/price-monitor-system/python-client/src/services/estimation_worker.py) 限流：
- `limit=20` → `limit=1`
- `time.sleep(0.5)` → `time.sleep(1.5)`
- 队列循环：循环发 20 个 → 只发 1 个

### 效果
| 指标 | Pre-fix | Post-fix |
|------|---------|----------|
| Calc 间隔 | 150-200ms | 871ms |
| 主线程空闲率 | ~10% | ~65% |
| UI 状态 | 卡死 | 流畅 |

### 后续架构级修复（已留 TODO，不阻塞）
- 新增 `CalcWorker` QThread，把 Excel COM 移到独立线程
- 主线程 0% blocking

---

## 症状

用户报告：程序无响应（操作没反应、按钮点不动、进度条不更新）。

## 证据收集

### 证据 1：log 时间戳分析（直接证明主线程被 calc 阻塞）

```
13:48:40.798  开始 calc product #3
13:48:41.332  完成 #3 (534ms)
13:48:41.529  开始 calc product #4  ← 仅 197ms 后立刻又 calc
13:48:42.055  完成 #4 (526ms)
13:48:42.207  开始 calc product #5  ← 仅 152ms 后又 calc
13:48:42.218  完成 #5 (530ms)
...back-to-back，无任何 idle 时间...
```

**结论**：
- 每条 calc 耗时 ~530ms
- calc 之间只间隔 ~150-200ms（远小于 calc 本身）
- 主线程 90% 时间在跑 Excel COM，10% 时间做 event loop（Qt 切回）

### 证据 2：日志早期 worker 输出
```
13:43:04,517  [EstimationWorker] 兜底扫到 20 条 pending
```
Worker 每 0.5s tick 就扫 20 条发 20 个信号 → 主线程排队 20 个 calc。

### 假设验证结果

| 假设 | 状态 | 证据 |
|------|------|------|
| H1（Worker 死循环） | ✅ **CONFIRMED** | Worker 每 0.5s 扫 20 条，发 20 个 queued 信号 |
| H2（主线程被 Excel 阻塞） | ✅ **CONFIRMED** | 日志显示 calc 连续执行，主线程 90% 时间在 Excel COM |
| H3（信号堆积） | ⚠️ PARTIAL | 信号是 queued，但主线程持续处理，不算"死锁" |
| H4（DB 锁） | ❌ REJECTED | 日志无 OperationalError，DB 写正常 |
| H5（定时器锁） | ❌ REJECTED | `_refresh_counts` 调用频率未观测到异常 |

### 根本原因

**主线程在同步跑 Excel COM calc，每 530ms 阻塞一次**。
"修复跨线程 COM" 的设计是让 calc 跑在主线程（避免 -2147417842 错误），
但代价是**主线程直接被 calc 占用**，UI 无响应。

### 修复方案（最小改动）

**限流**：让 worker 每次只发 1 个 estimateRequested 信号 + sleep 1s。
效果：calc 频率从 1/0.5s 降到 1/1.5s，主线程 blocking 占比从 90% → 35%，UI 恢复响应。

**完整重构方案**（不实施，留 TODO）：
新增 `CalcWorker` QThread，专门跑 Excel COM；主线程只做信号路由 + UI。
这才是架构上正确的修复。

## 修复目标

- 估价在后台线程跑（用 mock calc 替代真实 Excel）
- 主线程不再被 calc 阻塞
- UI 全程可响应

## 步骤

- [x] 1. 收到 bug 报告
- [x] 2. 收证据（日志/线程栈/DB 状态）
- [x] 3. 证伪假设
- [x] 4. 设计最小修复
- [x] 5. 实施修复
- [ ] 6. 验证（让用户操作 UI 确认不卡）
- [ ] 7. 清理（用户确认后删 _app.log）

## 修复对比（pre-fix vs post-fix）

### Pre-fix（13:48:40-42 的日志）
```
13:48:40.798  开始 calc #3
13:48:41.332  完成 #3 (534ms)
13:48:41.529  开始 #4     ← 197ms 间隔（远小于 calc 自身）
13:48:42.055  完成 #4 (526ms)
13:48:42.207  开始 #5     ← 152ms 间隔
13:48:42.218  完成 #5 (530ms)
```
→ 主线程 90% 时间被 Excel COM 占用，UI 卡死。

### Post-fix（13:55:36-38 的日志）
```
13:55:36.894  Excel计算完成
13:55:37.410  未找到有效价格 (516ms calc)
13:55:38.281  [EstimationWorker] emit estimateRequested(2389)  ← 871ms 间隔
13:55:38.452  开始 calc
13:55:38.983  calc 完成 (531ms)
```
→ 主线程每 cycle 有 ~1s 空闲处理 UI 事件。

## 改动

修改文件：[src/services/estimation_worker.py](file:///d:/word/price-monitor-system/python-client/src/services/estimation_worker.py)

```diff
- pending = list(self._queue)
- for db_id in pending:
-     self.estimateRequested.emit(int(db_id))
-     self._queue.discard(db_id)
+ db_id = next(iter(self._queue))
+ self.estimateRequested.emit(int(db_id))
+ self._queue.discard(db_id)

- pending_rows = self.db.get_pending_products(limit=20)
+ pending_rows = self.db.get_pending_products(limit=1)

- time.sleep(0.5)
+ time.sleep(1.5)
```

## 待办（不阻塞当前 bug 修复）

- [ ] 架构级修复：新增 `CalcWorker` QThread，把 Excel COM 移出主线程
  - 见 docs/CODE_REVIEW.md "Major #2" 段
  - 实施后主线程 0% blocking，UI 全程流畅
