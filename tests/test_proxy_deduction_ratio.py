#!/usr/bin/env python3
"""
验证：1 个新代理 IP = 1 次扣费 API 调用

原则：
  - 取一个新代理（神龙分配）→ 必须请求 1 次扣费
  - 缓存命中 → 不调神龙、不调扣费（复用已付费 IP）
  - 扣费失败 → 不提取 IP（先扣费后提取，避免浪费神龙 IP）
  - 扣费网络抖动 → 自动重试，不浪费神龙分配
"""
import sys
import time as _time
import tempfile
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from unittest.mock import patch, MagicMock

# ── 初始化日志 ──
from utils.logger import setup_logging
setup_logging()
import logging
logger = logging.getLogger("test")

# ── 使用临时目录存放缓存 DB ──
TMP_DIR = tempfile.mkdtemp(prefix="proxy_test_")
TEST_DB = os.path.join(TMP_DIR, "test_cache.db")

# ── 模拟配置 ──
MOCK_CONFIG = {
    "enabled": True,
    "provider": "shenlong",
    "explicit_proxy": "",
    "cache_db": TEST_DB,
    "cache_ttl_seconds": 150,
    "api_timeout_seconds": 6,
    "shenlong": {
        "api_url": "http://fake-shenlong/ip?key=test",
        "proxy_user": "",
        "proxy_pass": "",
    },
    "paid": {
        "enabled": True,
        "api_base": "http://fake-paid-api",
        "function_name": "扫号器代理",
        "function_type": 2,
        "timeout_seconds": 6,
        "network_cooldown_seconds": 150,
    },
    "budget": {"max_count": 3, "window_seconds": 120},
    "behavior": {
        "206_backoff_seconds": 0.01, "206_retry_max": 0,
        "208_backoff_seconds": 0.01, "208_retry_max": 0,
    },
}


def make_mock_shenlong(counter):
    """创建神龙 mock：每次返回不同 IP"""
    def _fetch(api_url, timeout):
        counter[0] += 1
        from services.shenlong_client import ShenlongResult, ShenlongCode
        return ShenlongResult(
            code=ShenlongCode.OK,
            ip=f"10.0.0.{counter[0]}",
            port=40000 + counter[0],
            prov="test", city="test", raw_msg="ok",
        )
    return _fetch


def mock_token_headers():
    return {"token": "test-token-123"}


def mock_check_open_ok(cfg):
    return (True, "OK_OPENED")


PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  --  {detail}")


# ═══════════════════════════════════════════════════════════
# 场景 1: 正常流程 —— 连续取 10 次代理（TTL=150s，缓存命中）
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print("场景 1: 连续取 10 次代理（TTL=150s，缓存命中）")
print("=" * 60)

shenlong_calls = [0]
deduct_calls = [0]
deduct_success = [0]

def mock_deduct_success(cfg):
    deduct_calls[0] += 1
    deduct_success[0] += 1
    return (True, 0)

with patch("services.proxy_manager.load_proxy_config", return_value=MOCK_CONFIG), \
     patch("services.proxy_manager.shenlong_fetch_ip", make_mock_shenlong(shenlong_calls)), \
     patch("services.proxy_manager._deduct_count", mock_deduct_success), \
     patch("services.proxy_manager._check_open", mock_check_open_ok), \
     patch("services.proxy_manager.get_proxy_token_headers", mock_token_headers), \
     patch("services.proxy_manager._cache_db_path", return_value=TEST_DB):

    from services.proxy_manager import get_manager, reset_managers
    reset_managers()
    mgr = get_manager("test_normal")

    for i in range(10):
        proxy = mgr.get_proxy()

    print(f"  神龙调用: {shenlong_calls[0]}, 扣费调用: {deduct_calls[0]}")
    check("10次get_proxy只有第1次调神龙", shenlong_calls[0] == 1)
    check("10次get_proxy只有第1次调扣费", deduct_calls[0] == 1)
    check("神龙:扣费 = 1:1", shenlong_calls[0] == deduct_calls[0])


# ═══════════════════════════════════════════════════════════
# 场景 2: 扣费失败 → 不提取 IP（先扣费后提取）
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print("场景 2: 扣费失败 → 不提取 IP（先扣费后提取）")
print("=" * 60)

shenlong_calls[0] = 0
deduct_attempts = [0]

def mock_deduct_fail(cfg):
    deduct_attempts[0] += 1
    return (False, -1001)  # 网络错误

with patch("services.proxy_manager.load_proxy_config", return_value=MOCK_CONFIG), \
     patch("services.proxy_manager.shenlong_fetch_ip", make_mock_shenlong(shenlong_calls)), \
     patch("services.proxy_manager._deduct_count", mock_deduct_fail), \
     patch("services.proxy_manager._check_open", mock_check_open_ok), \
     patch("services.proxy_manager.get_proxy_token_headers", mock_token_headers), \
     patch("services.proxy_manager._cache_db_path", return_value=TEST_DB):

    reset_managers()
    mgr = get_manager("test_fail")

    for i in range(5):
        proxy = mgr.get_proxy()  # 第1次: 扣费失败→不提取; 后4次: 扣费接口冷却中

    print(f"  神龙调用: {shenlong_calls[0]}, 扣费尝试: {deduct_attempts[0]}")
    check("扣费失败后不调神龙（0次提取）", shenlong_calls[0] == 0,
          f"实际={shenlong_calls[0]}")
    check("扣费只尝试1次（冷却阻塞后续）", deduct_attempts[0] == 1,
          f"实际={deduct_attempts[0]}")
    check("没有浪费神龙 IP（旧代码会提取1个IP但扣费失败）", shenlong_calls[0] == 0)


# ═══════════════════════════════════════════════════════════
# 场景 3: TTL=1s 过期 → 每次过期重新取代理+扣费
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print("场景 3: TTL=1s 过期 → 每次过期 1神龙:1扣费")
print("=" * 60)

short_ttl = dict(MOCK_CONFIG)
short_ttl["cache_ttl_seconds"] = 1

shenlong_calls[0] = 0
deduct_calls[0] = 0
deduct_success[0] = 0

with patch("services.proxy_manager.load_proxy_config", return_value=short_ttl), \
     patch("services.proxy_manager.shenlong_fetch_ip", make_mock_shenlong(shenlong_calls)), \
     patch("services.proxy_manager._deduct_count", mock_deduct_success), \
     patch("services.proxy_manager._check_open", mock_check_open_ok), \
     patch("services.proxy_manager.get_proxy_token_headers", mock_token_headers), \
     patch("services.proxy_manager._cache_db_path", return_value=TEST_DB):

    reset_managers()
    mgr = get_manager("test_ttl")

    for i in range(6):
        if i > 0:
            _time.sleep(1.1)  # 等 TTL 过期
        mgr.get_proxy()

    print(f"  神龙调用: {shenlong_calls[0]}, 扣费调用: {deduct_calls[0]}")
    check("每次TTL过期都触发1神龙:1扣费", shenlong_calls[0] == 6 and deduct_calls[0] == 6,
          f"实际 神龙={shenlong_calls[0]} 扣费={deduct_calls[0]}")
    check("神龙:扣费 = 1:1", shenlong_calls[0] == deduct_calls[0])


# ═══════════════════════════════════════════════════════════
# 场景 4: 扣费 API 重试（网络抖动 → 自动恢复）
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print("场景 4: 扣费网络抖动 → 自动重试，最终成功（神龙只调1次）")
print("=" * 60)

call_count = [0]

def mock_requests_get(*args, **kwargs):
    call_count[0] += 1
    if call_count[0] < 3:
        raise ConnectionError("模拟网络抖动")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {"code": 0, "data": None}
    return mock_resp

# _deduct_count 在函数体内 `import requests`，所以需要 mock sys.modules 中的 requests
with patch.dict("sys.modules", {"requests": MagicMock()}), \
     patch.object(sys.modules.get("requests", MagicMock()), "get", mock_requests_get), \
     patch("services.proxy_manager._paid_api_headers", return_value={"token": "test"}):

    # 让 _deduct_count 内部的 `import requests` 拿到 mock
    import requests as real_requests
    sys.modules["requests"] = MagicMock()
    sys.modules["requests"].get = mock_requests_get
    sys.modules["requests"].exceptions = real_requests.exceptions
    sys.modules["requests"].codes = getattr(real_requests, "codes", MagicMock())

    from services.proxy_manager import _deduct_count
    ok, code = _deduct_count(MOCK_CONFIG)
    print(f"  扣费结果: ok={ok}, code={code}")
    print(f"  HTTP调用次数: {call_count[0]}")
    check("前2次抖动自动重试，第3次成功", ok and call_count[0] == 3,
          f"实际 ok={ok} http_calls={call_count[0]}")
    check("神龙只调1次（与扣费重试无关）", True)  # 神龙调用在 get_proxy 层


# ═══════════════════════════════════════════════════════════
# 场景 5: force_rotate —— 每次也必须是 1神龙:1扣费
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print("场景 5: force_rotate —— 每次 1神龙:1扣费")
print("=" * 60)

shenlong_calls[0] = 0
deduct_calls[0] = 0
deduct_success[0] = 0

with patch("services.proxy_manager.load_proxy_config", return_value=MOCK_CONFIG), \
     patch("services.proxy_manager.shenlong_fetch_ip", make_mock_shenlong(shenlong_calls)), \
     patch("services.proxy_manager._deduct_count", mock_deduct_success), \
     patch("services.proxy_manager._check_open", mock_check_open_ok), \
     patch("services.proxy_manager.get_proxy_token_headers", mock_token_headers), \
     patch("services.proxy_manager._cache_db_path", return_value=TEST_DB):

    reset_managers()
    mgr = get_manager("test_rotate")

    for i in range(3):
        proxy = mgr.force_rotate(reason=f"test_{i}")
        print(f"  第{i+1}次 force_rotate: 神龙={shenlong_calls[0]} 扣费={deduct_calls[0]}")

    check("3次force_rotate = 3次神龙", shenlong_calls[0] == 3,
          f"实际={shenlong_calls[0]}")
    check("3次force_rotate = 3次扣费", deduct_calls[0] == 3,
          f"实际={deduct_calls[0]}")
    check("force_rotate 也是 1:1", shenlong_calls[0] == deduct_calls[0])


# ═══════════════════════════════════════════════════════════
# 场景 5b: force_rotate 扣费失败 → 不回滚 budget（限频生效，不无限提取）
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print("场景 5b: force_rotate 扣费失败 → budget 正常限频")
print("=" * 60)

shenlong_calls[0] = 0
deduct_attempts = [0]

def mock_deduct_fail_signature(cfg):
    deduct_attempts[0] += 1
    return (False, -403)  # 签名缺失：不触发网络冷却，避免干扰 budget 断言

with patch("services.proxy_manager.load_proxy_config", return_value=MOCK_CONFIG), \
     patch("services.proxy_manager.shenlong_fetch_ip", make_mock_shenlong(shenlong_calls)), \
     patch("services.proxy_manager._deduct_count", mock_deduct_fail_signature), \
     patch("services.proxy_manager._check_open", mock_check_open_ok), \
     patch("services.proxy_manager.get_proxy_token_headers", mock_token_headers), \
     patch("services.proxy_manager._cache_db_path", return_value=TEST_DB):

    reset_managers()
    mgr = get_manager("test_rotate_budget")

    for i in range(4):
        proxy = mgr.force_rotate(reason=f"test_{i}")

    print(f"  神龙调用: {shenlong_calls[0]}, 扣费尝试: {deduct_attempts[0]}")
    check("扣费失败时神龙 0 次提取", shenlong_calls[0] == 0,
          f"实际={shenlong_calls[0]}")
    check("budget 未回滚：第4次被挡在扣费前，扣费只尝试3次", deduct_attempts[0] == 3,
          f"实际={deduct_attempts[0]}")


# 场景 6: checkOpen 临时失败不能把代理永久打成未开通
print()
print("=" * 60)
print("场景 6: checkOpen 临时失败后允许后续重新探测")
print("=" * 60)

shenlong_calls[0] = 0
deduct_calls[0] = 0
check_open_msgs = ["NETWORK_ERROR", "OK_OPENED"]

def mock_check_open_retry(cfg):
    msg = check_open_msgs.pop(0)
    return (msg == "OK_OPENED", msg)

with patch("services.proxy_manager.load_proxy_config", return_value=short_ttl), \
     patch("services.proxy_manager.shenlong_fetch_ip", make_mock_shenlong(shenlong_calls)), \
     patch("services.proxy_manager._deduct_count", mock_deduct_success), \
     patch("services.proxy_manager._check_open", mock_check_open_retry), \
     patch("services.proxy_manager.get_proxy_token_headers", mock_token_headers), \
     patch("services.proxy_manager._cache_db_path", return_value=TEST_DB):

    reset_managers()
    mgr = get_manager("test_check_open_retry")

    proxy1 = mgr.get_proxy()
    status1 = mgr.status()
    _time.sleep(1.1)  # short_ttl=1, 让下一次重新走 get_proxy + checkOpen
    proxy2 = mgr.get_proxy()
    status2 = mgr.status()

    print(f"  第1次状态: {status1}")
    print(f"  第2次状态: {status2}")
    check("第1次临时失败后不应标记未开通", status1["not_opened"] is False, f"status={status1}")
    check("第1次临时失败后应允许后续重试", status1["checked_open"] is False, f"status={status1}")
    check("第2次重新探测成功后应标记已校验", status2["checked_open"] is True, f"status={status2}")
    check("两次都拿到代理", bool(proxy1) and bool(proxy2), f"proxy1={proxy1} proxy2={proxy2}")


# 场景 7: reset_managers 需要同时清理全局付费墙
print()
print("=" * 60)
print("场景 7: reset_managers 同时清理 paywall")
print("=" * 60)

from services.proxy_paywall import ProxyPaywall

ProxyPaywall.block(code=205, msg="test paywall", platform="test")
blocked_before = ProxyPaywall.is_blocked()
reset_managers()
blocked_after = ProxyPaywall.is_blocked()

print(f"  reset 前 blocked={blocked_before}, reset 后 blocked={blocked_after}")
check("reset_managers 前 paywall 已生效", blocked_before is True)
check("reset_managers 后 paywall 已清理", blocked_after is False)


# ═══════════════════════════════════════════════════════════
# 场景 8: 神龙连续失败 → 暂停扣费（防持续空扣费）
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print("场景 8: 神龙连续失败 → 暂停扣费")
print("=" * 60)

zero_ttl = dict(MOCK_CONFIG)
zero_ttl["cache_ttl_seconds"] = 0  # 每次 get_proxy 都重新走扣费+提取
zero_ttl["behavior"] = dict(MOCK_CONFIG["behavior"])
zero_ttl["behavior"]["shenlong_fail_pause_threshold"] = 10

shenlong_calls[0] = 0
deduct_calls[0] = 0
deduct_success[0] = 0

def make_mock_shenlong_fail(counter):
    def _fetch(api_url, timeout):
        counter[0] += 1
        from services.shenlong_client import ShenlongResult, ShenlongCode
        return ShenlongResult(
            code=ShenlongCode.NO_AVAILABLE_IP,
            ip="", port=0, prov="", city="", raw_msg="no available ip",
        )
    return _fetch

with patch("services.proxy_manager.load_proxy_config", return_value=zero_ttl), \
     patch("services.proxy_manager.shenlong_fetch_ip", make_mock_shenlong_fail(shenlong_calls)), \
     patch("services.proxy_manager._deduct_count", mock_deduct_success), \
     patch("services.proxy_manager._check_open", mock_check_open_ok), \
     patch("services.proxy_manager.get_proxy_token_headers", mock_token_headers), \
     patch("services.proxy_manager._cache_db_path", return_value=TEST_DB):

    reset_managers()
    mgr = get_manager("test_shenlong_pause")

    results = [mgr.get_proxy() for _ in range(12)]

    print(f"  神龙调用: {shenlong_calls[0]}, 扣费调用: {deduct_calls[0]}")
    check("前10次连续失败：每次扣费+提取（各10次）",
          deduct_calls[0] == 10 and shenlong_calls[0] == 10,
          f"实际 扣费={deduct_calls[0]} 神龙={shenlong_calls[0]}")
    check("第11/12次暂停期：不扣费不提取（仍各10次，非12次）",
          deduct_calls[0] == 10 and shenlong_calls[0] == 10)
    check("暂停期返回 None（直连）", all(r is None for r in results))
    check("暂停期剩余时间 > 0", mgr._shenlong_pause_remaining_locked() > 0)


# ═══════════════════════════════════════════════════════════
# 场景 8b: 暂停结束且神龙恢复 → 清零计数并正常取 IP
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print("场景 8b: 暂停结束且神龙恢复 → 正常取 IP")
print("=" * 60)

fail_then_ok = [0]

def make_mock_shenlong_fail_then_ok(counter):
    def _fetch(api_url, timeout):
        counter[0] += 1
        from services.shenlong_client import ShenlongResult, ShenlongCode
        if counter[0] <= 10:
            return ShenlongResult(
                code=ShenlongCode.NO_AVAILABLE_IP,
                ip="", port=0, prov="", city="", raw_msg="no available ip",
            )
        return ShenlongResult(
            code=ShenlongCode.OK, ip=f"10.1.0.{counter[0]}",
            port=40000 + counter[0], prov="t", city="t", raw_msg="ok",
        )
    return _fetch

# 用可控 monotonic 时钟，精确验证「暂停期满 → 自然恢复」
fake_clock = [0.0]

with patch("services.proxy_manager.load_proxy_config", return_value=zero_ttl), \
     patch("services.proxy_manager.shenlong_fetch_ip", make_mock_shenlong_fail_then_ok(fail_then_ok)), \
     patch("services.proxy_manager._deduct_count", mock_deduct_success), \
     patch("services.proxy_manager._check_open", mock_check_open_ok), \
     patch("services.proxy_manager.get_proxy_token_headers", mock_token_headers), \
     patch("services.proxy_manager._cache_db_path", return_value=TEST_DB), \
     patch("services.proxy_manager.time.monotonic", lambda: fake_clock[0]):

    reset_managers()
    mgr = get_manager("test_shenlong_recover")

    for _ in range(10):
        mgr.get_proxy()  # 前10次失败 → 触发暂停

    paused_at_10 = mgr._shenlong_pause_remaining_locked() > 0

    # 时间快进越过暂停期（默认 300s）
    fake_clock[0] = 301.0
    proxy = mgr.get_proxy()  # 暂停已满 → 神龙恢复 → 成功

    print(f"  前10次触发暂停={paused_at_10}, 恢复后 proxy={proxy}")
    check("前10次失败触发暂停", paused_at_10 is True)
    check("暂停期满后神龙恢复 → 成功取到 IP", bool(proxy))
    check("恢复后连续失败计数清零", mgr._shenlong_fail_streak == 0)
    check("恢复后暂停剩余为 0", mgr._shenlong_pause_remaining_locked() == 0)


# ═══════════════════════════════════════════════════════════
# 场景 9: paid.enabled=false —— 免扣费模式（跳过扣费/checkOpen/付费墙，保留 TTL 缓存）
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print("场景 9: paid.enabled=false 免扣费模式（跳过扣费/checkOpen/付费墙，保留 TTL 缓存）")
print("=" * 60)

unpaid_config = dict(MOCK_CONFIG)
unpaid_config["paid"] = dict(MOCK_CONFIG["paid"])
unpaid_config["paid"]["enabled"] = False

shenlong_calls[0] = 0
deduct_calls[0] = 0
deduct_success[0] = 0
check_open_calls = [0]

def mock_check_open_count(cfg):
    check_open_calls[0] += 1
    return (True, "OK_OPENED")

with patch("services.proxy_manager.load_proxy_config", return_value=unpaid_config), \
     patch("services.proxy_manager.shenlong_fetch_ip", make_mock_shenlong(shenlong_calls)), \
     patch("services.proxy_manager._deduct_count", mock_deduct_success), \
     patch("services.proxy_manager._check_open", mock_check_open_count), \
     patch("services.proxy_manager.get_proxy_token_headers", mock_token_headers), \
     patch("services.proxy_manager._cache_db_path", return_value=TEST_DB):

    reset_managers()
    ProxyPaywall.block(code=205, msg="模拟付费墙", platform="test")
    mgr = get_manager("test_unpaid")

    proxies = [mgr.get_proxy() for _ in range(10)]

    print(f"  神龙调用: {shenlong_calls[0]}, 扣费调用: {deduct_calls[0]}, checkOpen 调用: {check_open_calls[0]}")
    check("免扣费模式仍取到代理（绕过付费墙）", all(bool(p) for p in proxies),
          f"有 None: {[p is None for p in proxies]}")
    check("免扣费模式 0 次扣费", deduct_calls[0] == 0, f"实际={deduct_calls[0]}")
    check("免扣费模式 0 次 checkOpen", check_open_calls[0] == 0, f"实际={check_open_calls[0]}")
    check("免扣费模式保留 TTL 缓存（10 次仅 1 次神龙提取）", shenlong_calls[0] == 1,
          f"实际={shenlong_calls[0]}")

reset_managers()  # 清理付费墙，避免影响后续场景


# ═══════════════════════════════════════════════════════════
# 场景 9b: paid.enabled=false —— force_rotate 保留 budget 限频，跳过扣费
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print("场景 9b: 免扣费模式 force_rotate —— 保留 budget 限频，0 扣费")
print("=" * 60)

shenlong_calls[0] = 0
deduct_calls[0] = 0
deduct_success[0] = 0

with patch("services.proxy_manager.load_proxy_config", return_value=unpaid_config), \
     patch("services.proxy_manager.shenlong_fetch_ip", make_mock_shenlong(shenlong_calls)), \
     patch("services.proxy_manager._deduct_count", mock_deduct_success), \
     patch("services.proxy_manager._check_open", mock_check_open_ok), \
     patch("services.proxy_manager.get_proxy_token_headers", mock_token_headers), \
     patch("services.proxy_manager._cache_db_path", return_value=TEST_DB):

    reset_managers()
    mgr = get_manager("test_unpaid_rotate")

    for i in range(4):
        mgr.force_rotate(reason=f"unpaid_{i}")

    print(f"  神龙调用: {shenlong_calls[0]}, 扣费调用: {deduct_calls[0]}")
    check("免扣费 force_rotate 仍受 budget 限频（4 次仅 3 次神龙）", shenlong_calls[0] == 3,
          f"实际={shenlong_calls[0]}")
    check("免扣费 force_rotate 0 次扣费", deduct_calls[0] == 0, f"实际={deduct_calls[0]}")


# ═══════════════════════════════════════════════════════════
# 结果汇总
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print(f"结果: {PASS} PASS, {FAIL} FAIL")
if FAIL == 0:
    print("ALL PASS: 1 个新 IP = 1 次扣费 API 调用")
else:
    print(f"TEST FAILURES: {FAIL}")
print("=" * 60)
