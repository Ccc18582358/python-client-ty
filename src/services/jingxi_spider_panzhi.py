#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盼之代售 (panzhi.com) 商品账号爬虫。

职责（只做这几件事）：
    1. 翻页拉取商品列表（POST public/goodsPublic/page，需 WAF 参数 + v17 签名）。
    2. 提取列表中的 goodsNo、price 等字段。
    3. 价格对比：已存在且价格不变 → 跳过详情请求。
    4. GET 请求商品详情页 HTML，解析 content-table 和商品描述。
    5. 通过回调（on_batch / on_progress / should_stop）逐批返回给 GUI。

不在职责范围：
    - 不写数据库（由 scanner_service.submit_scan_results 负责）。
    - 不依赖 Django。
    - 不做算法筛选。
    - 不导出 Excel。

架构：
    - HTTP 层：curl_cffi 模拟 Chrome TLS 指纹（优先），降级到 requests。
    - 并发模型：主线程翻列表 → ThreadPoolExecutor 并发拉详情。
    - WAF 自动刷新：检测到 challenge HTML → 调 Node.js runner 执行 challenge → 更新状态 → 重试。

用法（示例）：
    import threading
    from services.jingxi_spider_panzhi import PanzhiAccountCrawler

    crawler = PanzhiAccountCrawler(
        game_id="11",
        game_name="火影忍者",
        platform="盼之",
        token="2f2349c584c040829480813af0f4872a",
    )
    crawler.load_browser_context({...})  # 从浏览器抓包注入 WAF 状态

    stop_event = threading.Event()

    def on_batch(batch):
        for it in batch["results"]:
            print(it["product_id"], it["original_price"])

    threading.Thread(
        target=lambda: crawler.run_full(
            on_batch=on_batch,
            on_progress=None,
            should_stop=stop_event.is_set,
        ),
        daemon=True,
    ).start()

注意：
    - 需要 token（登录态）和 WAF 状态（从浏览器抓包获取，或使用内置默认值）。
    - 列表接口：POST https://api.pzds.com/api/web-client/v2/public/goodsPublic/page
    - 详情页：GET https://www.pzds.com/goodsDetails/{goodsNo}/{catalogueId}
    - 签名算法：WASM（优先，版本由 oss wasm-config-prod.json 的 pc.currentVersion 决定，现为 v18）→ MD5（降级），由 sign_utils.generate_sign() 统一处理。
    - decode__1174：由 decode_utils.generate_decode_value() 通过 Node.js WAF runner 生成。
    - 代理：默认直连，传 proxy 参数可覆盖。
"""

# 关掉代理 HTTPS 证书警告
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import json
import logging
import os
import queue
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("jingxi_spider_panzhi")

# ── 代理已全部迁移到 services.proxy_manager ──
#    本文件不再 import kejinshou 下的神龙 helper；is_proxy_enabled 仍可独立用
try:
    from services.jingxi_spider_kejinshou import is_proxy_enabled  # type: ignore[import-not-found]
except ImportError:
    is_proxy_enabled = None  # type: ignore[assignment]

# 盼之专用的验证码检测文本
PANZHI_CAPTCHA_TEXT = "为了更好的访问体验，请进行验证"


def _is_panzhi_captcha(text: str) -> bool:
    """检测盼之验证码页面。"""
    return PANZHI_CAPTCHA_TEXT in (text or "")

# ── ssxmod cookie 生成器 ─────────────────────────────────────
try:
    from services.panzhi_folder.waf_cookie_generator import generate_cookies  # type: ignore[import-untyped]
except ImportError:
    try:
        from panzhi_folder.waf_cookie_generator import generate_cookies  # type: ignore[import-untyped]
    except ImportError:
        generate_cookies = None  # type: ignore[assignment]

# ── WAF 指纹 / 事件模板（与生产 panzhi_publish_helper.py 一致）──
WAF_FINGERPRINT_TEMPLATE = (
    "3318398726a19e35503032^websdk-2.3.18d^{now}^357^1|15^zh-CN^-480^16705151|12791"
    "^3072|1728|3072|560|0|0|3072|1728|3072|1688|0|0^3^Win32^12^ANGLE "
    "(NVIDIA, NVIDIA GeForce GTX 1060 6GB (0x00001C03) Direct3D11 vs_5_0 ps_5_0, D3D11)"
    "|Google Inc. (NVIDIA)^24|24^0^28^5|1318940364^340347491^3758718338^1^19^1^0"
    "^M^101^4^0^416^Google Inc.^8^136|39|28|2|0^3726056879^125^{now}"
    "^599647252^10^29^^22|2364406823|1^5^1^0^9^-1^0^0"
)
WAF_EVENT_TEMPLATE = (
    "3318398726a19e35503032^websdk-2.3.18d^{now}^357^M^535"
    "^762+489+519449|86+-85+32|14+-11+414|-26+40+32|-119+252+37|-116+39+2837"
    "|150+-258+31|89+-119+32|12+-18+38|0+1+259|-1+11+31|-3+58+32|0+150+30"
    "|-8+207+32|462+-410+8056|-84+305+186285|-2+0+100|-85+67+30|-747+51+650"
    "|-48+-16+32|-36+-3+32|-23+0+30|-12+4+31|-3+3+40|-2+5+30|0+7+33"
    "|1147+-188+4325|220+-39+131763|64+-75+31|21+-14+32|1+0+31|3+-8+32"
    "|-5+0+31|-14+17+32|-12+43+31|-528+54+59230|-33+-29+32|-38+-21+32"
    "|-61+-36+30|-45+-17+32|-16+-2+31|-5+3+31|-22+26+32|-56+68+32"
    "|363+-126+85593|2+-4+1891|41+-33+30|40+-55+32|33+-57+32|10+-43+30"
    "|19+-59+32|3+-13+32|3+-12+38|-5+8+40|-18+56+30|-27+90+32|-23+161+32"
    "|597+91+47044|-316+-38+32|-299+-40+30|-124+-20+32|-18+-4+102|-15+4+32"
    "|-54+1+30|-139+-24+32|-120+-35+32|-60+-24+30|9+-10+32|121+-8+32"
    "|173+0+30|27+0+32|2+-1+78|8+2+62|-6+3+32|-12+0+32|-8+0+30|-2+-7+40"
    "|0+-9+32|11+-14+30|14+-10+32|13+-5+32|13+-4+30|7+-5+32|2+0+134"
    "|3+2+30|4+5+40|3+3+32|0+2+516|1+11+32|0+27+30|0+16+32"
    "|961+148+2740625|-278+-88+47|-132+-55+39|-20+-12+31|-9+1+30|-5+2+286"
    "|-2+1+1076|-56+66+31|-1616+-291+37169|0+2+648"
    "^1050676^10+1156+317+1050676|11+1156+317+87|10+1157+375+439|11+1157+375+77"
    "^^0^0^0^125^{now}^0^0^2^2^0"
)

# ── curl_cffi（优先）──────────────────────────────────────────
import requests as _fallback_requests

try:
    from curl_cffi import requests as curl_requests  # type: ignore[import-untyped]

    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None  # type: ignore[assignment]
    HAS_CURL_CFFI = False

# ── 项目内 WAF / 签名工具 ────────────────────────────────────
try:
    from services.panzhi_folder.decode_utils import (  # type: ignore[import-untyped]
        generate_decode_value,
        append_decode_param,
        run_challenge,
        DEFAULT_DEVICE_ID,
        DEFAULT_GLOBAL_ID,
        DEFAULT_PZID,
        DEFAULT_WAF_BD,
        DEFAULT_WAF_A_TS,
        DEFAULT_WAF_FP_ID,
        DEFAULT_WAF_DYSIG,
    )
except ImportError:
    # 降级：直接从 panzhi_folder 导入
    _script_dir = Path(__file__).resolve().parent / "panzhi_folder"
    if str(_script_dir) not in sys.path:
        sys.path.insert(0, str(_script_dir))
    from decode_utils import (  # type: ignore[import-untyped]
        generate_decode_value,
        append_decode_param,
        run_challenge,
        DEFAULT_DEVICE_ID,
        DEFAULT_GLOBAL_ID,
        DEFAULT_PZID,
        DEFAULT_WAF_BD,
        DEFAULT_WAF_A_TS,
        DEFAULT_WAF_FP_ID,
        DEFAULT_WAF_DYSIG,
    )

try:
    from services.panzhi_folder.sign_utils import (  # type: ignore[import-untyped]
        generate_sign,
        generate_device_id,
        generate_global_id,
        generate_pzid,
    )
except ImportError:
    _script_dir = Path(__file__).resolve().parent / "panzhi_folder"
    if str(_script_dir) not in sys.path:
        sys.path.insert(0, str(_script_dir))
    from sign_utils import (  # type: ignore[import-untyped]
        generate_sign,
        generate_device_id,
        generate_global_id,
        generate_pzid,
    )

# ── 常量 ─────────────────────────────────────────────────────

DEFAULT_LIST_URL = "https://api.pzds.com/api/web-client/v2/public/goodsPublic/page"
API_SERVER_TIME = "https://api.pzds.com/api/server/time"
DETAIL_URL_TEMPLATE = "https://www.pzds.com/goodsDetails/{goods_no}/{catalogue_id}"

# WAF 状态持久化文件（与生产 refresh_waf.py → waf_state.json 一致）
WAF_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "config", "waf_state.json")
WAF_STATE_FALLBACK_FILE = WAF_STATE_FILE + ".bak2"

DEFAULT_BROWSER_VERSION = os.environ.get("PZ_BROWSER_VERSION", "136")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{DEFAULT_BROWSER_VERSION}.0.0.0 Safari/537.36"
)
PZ_VERSION = os.environ.get("PZ_VERSION", "26.902.1553")
DEFAULT_X_SIGN_VERSION = os.environ.get("PZ_X_SIGN_VERSION", "")
CHANNEL_INFO = (
    '{"channelCode":null,"tag":null,"channelType":null,'
    '"searchWord":"null","adExtras":"","urlParam":""}'
)

# 默认 Cookie（不含 ssxmod / acw_tc / sso，由 browser_context 提供或运行时刷新）
DEFAULT_COOKIES: Dict[str, str] = {
    "_c_WBKFRo": "uEkRuaZ5sNWbdzdmcesbfseqQ9Z57IX4i8yReb1a",
    "Hm_lvt_8e2c03f98f8af83cf09317d232baf903": "1782358618",
    "HMACCOUNT": "B7415ECF2CCB5365",
}

# 默认 ssxmod（长编码 WAF 会话 cookie，来自浏览器的固定备份值）
DEFAULT_SSXMOD_ITNA = (
    "1-eq8xgxAxyDrxKx4qiPBYq0titDRDvxUqYo3e5D8D_qitGLQDWLqPiIxD_UDfoqAtGpDWeDhDQqDSRDGoDtE4GODFuo4Dwx"
    "xii1DGPDLlpm75zfDj_i6AKAg3i_DI43dDOEaPG9D7q2O1Qr1gaQDi89BeqDYPDUxDz8geDSgG3D0F5uiKD0pqDix0k7D0"
    "CDnqDzZxDpxG7GUxWeQqDUiejqDDtkiSD3WMzOm2DgWw4DDTUumrUD050m/Y4eD0idr3xIWk2qG52gDlDdNw4VIkKuQlRqs"
    "ZRmkztehHRgjjoIeVDLszS9NGgMNuQouN_imxmdb24xpAZmdlifLje3meGDDiDD3hYrKgWT4DjY2dI2or2qxGWlA3eeAeOo"
    "cREmYPWxe0e4eeaDaP59_KIt5Io0ua0To6hGW2EA_rY_=K0DmBPDPD/35t17beTN=fqLw9DE31mow41fT17b6l_WxFalw8Cm"
    "m8KIYO6pWPkKc_WckGc3qI77Uuem4NI7KgGkqTxehWerxgAF30F4mUemGPL7DAEC=Q4R0_O_jrfXK92S2eOFbZBgpQ_WLyS"
    "F_D0qLGOGvhpqlj7mEGC6LzZdzZyOWHsZyH5QduFmaGHlxUQoW7vHGoAxGvK0aDiaiMqd25TKSXjqWxPI0oAiiUrtfDDYo"
    "mdCh82_OQoA1P1IwdYALDDpCbfB_GjuQmw1mek0hjevHfCPIG=74hifBDhDdm3B2DBKkDr7DtDWG5ERDD3TF_BKiqzGDID4"
    "0q54O0PYi5jxmr5jdK0_i1hGAjQg92ld4KrmxQmVlFlpzrkuDLrigixaQYqxeO2pDD"
)
DEFAULT_SSXMOD_ITNA2 = (
    "1-eq8xgxAxyDrxKx4qiPBYq0titDRDvxUqYo3e5D8D_qitGLQDWLqPiIxD_UDfoqAtGpDWeDhDQqDSRDGoDtE4GODFuo4Dwx"
    "xii1DGPDLlpm75zfDj_i6AKAg3i_DI43dDOEaPG9D7q2O1Qr1gaQDi89BeqDYPDUxDz8geDSgG3D0F5uiKD0pqDix0k7D0"
    "CDnqDzZxDpxG7GUxWeQqDUiejqDDtkiSD3WMzOm2DgWw4DDqEueCUD050m/Y4eD0idr3xIWk2qG52gDlDdNw4VIkKuQlRqs"
    "ZRmkUoDvCtdk_o0N0SvvzZq7U/uQTRINw9=GCr3FYAe6vCh5b1ppihmDeDD4DioeoRYv8bDGpY8rQtEozYDmb5ahGDaYoE"
    "FtaeEG3GDIiGGA4AAB0Fn0Rg0QxzU4zYE=em3tPaQ4rj2DKieXDAD0HgAEqd_7REi17CnK5=E_QOWAT1fqT_pFm30__zQw"
    "_n7IYOxmWPkKc_WnwY93qI77exNIGWMZ7Ck73GYm_Y3Y5cbx9_YerUqCDqU7PqD=pLhgBYTf96iX4F292eaFbc7gpQKyF"
    "9bmVr7PQqlQjCG5evxb2SC6LzphC0uBFj1mhZR8mENw=M4BGeO5QrIecoz7DgDGHCi7pj9DGFS9S/BWivdHx4l0=o3elYq"
    "iwQ925o8o7fqiXT/B3bz7KPD3gqoBqfiI5hv5=DfDPteA0YIhPhPjxzDY4hd0DDGvzx_D4jUPx8jU=G5e45eDiR=jAPxTU"
    "DfD8mjC0YDPo0SQ4UYoA_C8WFIkkKlP1qNtIO07lvOiXmvho992pOUYA_O9YGx3ReD"
)

DEFAULT_HEADERS: Dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
    "Content-Type": "application/json",
    "Origin": "https://www.pzds.com",
    "Pragma": "no-cache",
    "Referer": "https://www.pzds.com/",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "PZVersionCode": "1",
    "Skey": "CLIENT",
    "PZPlatform": "pc",
    "PZOs": "windows",
    "channelInfo": CHANNEL_INFO,
    "x-oss-forbid-overwrite": "true",
}

# 触发"连续 N 次风控就停"的阈值
RISK_STOP_THRESHOLD = 5
# 403 后默认暂停（秒）
RISK_PAUSE_SECONDS = 120

# ── 数据库（只读，查询已有商品价格用于跳过详情）───────────────


def _app_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _db_path() -> str:
    return os.path.join(_app_root(), "data", "price_monitor_v2.db")


def _query_existing_prices(product_ids: List[str]) -> Dict[str, float]:
    """批量查询已有商品的最新价格。返回 {product_id: original_price}。"""
    if not product_ids:
        return {}
    db = _db_path()
    if not os.path.exists(db):
        return {}
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in product_ids)
        conn.execute(
            f"SELECT product_id, original_price FROM products "
            f"WHERE product_id IN ({placeholders}) ORDER BY id DESC",
            product_ids,
        )
        seen: Set[str] = set()
        result: Dict[str, float] = {}
        for row in conn.fetchall():
            pid = row["product_id"]
            if pid not in seen:
                result[pid] = float(row["original_price"] or 0)
                seen.add(pid)
        conn.close()
        return result
    except Exception:
        return {}


# ── 工具函数 ─────────────────────────────────────────────────


def _parse_cookie(value: Any) -> Dict[str, str]:
    """解析 cookie 字符串 / dict / JSON 为 {key: value}。"""
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(k): ("" if v is None else str(v)) for k, v in value.items()}
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            try:
                return _parse_cookie(json.loads(text))
            except Exception:
                pass
        cookies: Dict[str, str] = {}
        for part in text.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                if k:
                    cookies[k] = v
        return cookies
    return {}


def _cookie_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def _coerce_price(value: Any) -> Optional[float]:
    """盼之列表价格是元（数字或字符串），直接转 float。"""
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _is_waf_challenge(text: str) -> bool:
    """检测响应文本是否为 WAF challenge 页面。"""
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "aliyun_waf",
            "_waf_bd8ce2ce37",
            "renderData",
            "aliyun_waf_fa9faf9f45",
        )
    )


def _is_captcha(text: str) -> bool:
    return "AliyunCaptcha.js" in (text or "") or "initAliyunCaptcha" in (text or "")


def _extract_waf_bd(text: str) -> str:
    """从 challenge HTML 中提取 _waf_bd8ce2ce37 的值。"""
    m = re.search(r'_waf_bd8ce2ce37":"(.*?)"', text or "")
    return m.group(1) if m else ""


def _state_head(value: Any) -> str:
    """获取 WAF 状态值 || 分隔符之前的部分。"""
    return str(value or "").split("||", 1)[0]


def _decoded_state_head(value: Any) -> str:
    head = _state_head(value)
    try:
        return urllib.parse.unquote(head)
    except Exception:
        return head


def _state_ts(value: Any, fallback: int) -> int:
    head = _state_head(value)
    return int(head) if head.isdigit() else fallback


def _resolve_waf_state_file() -> Optional[str]:
    for path in (WAF_STATE_FILE, WAF_STATE_FALLBACK_FILE):
        if os.path.exists(path):
            return path
    return None


# ── HTML 解析（纯 regex，无 BeautifulSoup 依赖）───────────────


def _parse_detail_html(html: str) -> Tuple[Dict[str, str], str]:
    """解析盼之商品详情页 HTML。

    Returns:
        (table_data, description_text)

        table_data: content-table 的 key-value 对，如 {"商品类型": "成品号", ...}
        description_text: 商品描述文本（【操作系统】ios\\n【游戏区服】苹果QQ\\n...）
    """
    table_data: Dict[str, str] = {}
    description_text = ""

    if not html:
        return table_data, description_text

    # ── 1. 解析所有 content-table ──
    # 页面可能有多组 content-table，全部提取
    table_blocks = re.findall(
        r'<div class="content-table"[^>]*>(.*?)</div>\s*</div>\s*</div>',
        html,
        re.DOTALL,
    )
    for block in table_blocks:
        items = re.findall(
            r'<div class="content-table-label"[^>]*>(.*?)</div>\s*'
            r'<div class="content-table-value"[^>]*>\s*'
            r'<span[^>]*>(.*?)</span>',
            block,
            re.DOTALL,
        )
        for label, value in items:
            label_text = re.sub(r"<[^>]+>", "", label).strip()
            value_text = re.sub(r"<[^>]+>", "", value).strip()
            if label_text and value_text:
                table_data[label_text] = value_text

    # ── 2. 解析商品描述（text-overflow）──
    desc_match = re.search(
        r'<div class="text-overflow"[^>]*>\s*<span[^>]*>(.*?)</span>',
        html,
        re.DOTALL,
    )
    if desc_match:
        raw = desc_match.group(1)
        # 去掉 HTML 标签，保留文本
        description_text = re.sub(r"<[^>]+>", "", raw).strip()

    return table_data, description_text


def _parse_description_sections(desc: str) -> Dict[str, str]:
    """解析商品描述中的【key】value 格式。

    例: "LVPPK 号 \\n【操作系统】ios\\n【游戏区服】苹果QQ"
    → {"操作系统": "ios", "游戏区服": "苹果QQ"}
    """
    result: Dict[str, str] = {}
    # 匹配【key】后面直到下一个【或结尾的内容
    matches = re.findall(r"【(.+?)】\s*(.*?)(?=【|$)", desc, re.DOTALL)
    for key, value in matches:
        key = key.strip()
        value = value.strip()
        if key:
            result[key] = value
    return result


def _build_product_info(
    table_data: Dict[str, str],
    desc: str,
    game_name: str = "",  # noqa: ARG001 (reserved for future prefix logic)
) -> str:
    """组装 product_info 字符串（中文逗号分隔，与 kejinshou 风格一致）。"""
    desc_sections = _parse_description_sections(desc)
    parts: List[str] = []

    # 区服（优先从描述中取）
    area = desc_sections.get("游戏区服", "")
    server = table_data.get("区服", desc_sections.get("区服", ""))
    if area and server:
        parts.append(f"{area} {server}区")
    elif server:
        parts.append(f"{server}区")
    elif area:
        parts.append(area)

    # content-table 的 key-value 对（排序：常见字段优先）
    TABLE_ORDER = [
        "商品类型", "发布时间", "商品编号", "有无防沉迷", "实名情况",
        "S忍者数", "A忍者数", "战力", "VIP", "能否人脸包赔",
    ]
    for key in TABLE_ORDER:
        val = table_data.get(key)
        if val:
            parts.append(f"{key}：{val}")
    for key, val in table_data.items():
        if key not in TABLE_ORDER and val:
            parts.append(f"{key}：{val}")

    # 描述里的结构化字段（排除已覆盖的）
    DESC_ORDER = [
        "操作系统", "游戏区服", "VIP等级", "战力值", "段位",
        "点券", "金币", "高招券", "战力排名", "忍者星数",
    ]
    for key in DESC_ORDER:
        val = desc_sections.get(key)
        if val and key not in table_data:
            parts.append(f"{key}：{val}")

    # S忍 / A忍 / B忍 列表
    for ninja_key in ("S忍", "A忍", "B忍"):
        val = desc_sections.get(ninja_key)
        if val:
            parts.append(f"{ninja_key}：" + val.replace("，", "、"))

    # 其他描述 key
    for key, val in desc_sections.items():
        if key not in DESC_ORDER and key not in ("S忍", "A忍", "B忍") and val:
            parts.append(f"{key}：" + val.replace("，", "、"))

    product_info = "，".join(parts)
    return product_info


def _detect_realname(table_data: Dict[str, str], desc: str) -> bool:
    """检测是否支持二次实名。"""
    realname_keys = ("实名情况", "有无防沉迷", "能否人脸包赔")
    for key in realname_keys:
        val = table_data.get(key, "")
        if val:
            if any(
                kw in val
                for kw in ("不可修改", "不可二次实名", "不可实名",
                           "不能实名", "禁实名", "未实名", "不支持人脸包赔")
            ):
                return False
            if any(
                kw in val
                for kw in ("可二次实名", "可修改", "可实名", "支持人脸包赔")
            ):
                return True
    # fallback：检查描述
    if any(kw in desc for kw in ("不可二次实名", "不可修改实名", "不能实名")):
        return False
    if any(kw in desc for kw in ("可二次实名", "可修改实名")):
        return True
    return True  # 默认保守当作支持


# ── 爬虫主体 ─────────────────────────────────────────────────


class PanzhiAccountCrawler:
    """盼之独立爬虫，不依赖 Django。由 GUI 在后台线程调用 run_full / run_latest。"""

    def __init__(
        self,
        *,
        # 鉴权
        token: str = "",
        # 游戏
        game_id: str = "11",
        game_name: str = "",
        goods_catalogue_id: int = 6,
        platform: str = "盼之",
        # 接口
        list_url: str = DEFAULT_LIST_URL,
        base_headers: Optional[Dict[str, str]] = None,
        # 网络
        proxy: Optional[str] = None,
        is_proxy: Optional[bool] = None,
        # 调度
        detail_concurrency: int = 3,
        requests_per_second: float = 0.5,
        queue_size: int = 300,
        timeout: float = 30.0,
        max_retries: int = 3,
        batch_size: int = 30,
        max_pages: int = 2000,
        page_size: int = 10,
        verify_ssl: bool = False,
        # 防封
        jitter_max: float = 0.5,
        risk_pause_seconds: float = RISK_PAUSE_SECONDS,
        # WAF 初始状态（可从浏览器抓包覆盖）
        waf_bd: str = DEFAULT_WAF_BD,
        waf_a_ts: str = DEFAULT_WAF_A_TS,
        waf_fp_id: str = DEFAULT_WAF_FP_ID,
        waf_dysig: str = DEFAULT_WAF_DYSIG,
        device_id: str = "",
        global_id: str = "",
        pzid: str = "",
        browser_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.token = token
        self.game_id = game_id
        self.game_name = game_name
        self.goods_catalogue_id = goods_catalogue_id
        self.platform = platform
        self.list_url = list_url
        self.page_size = page_size

        # ── WAF 状态 ──
        self._waf_bd = waf_bd
        self._waf_a_ts = waf_a_ts
        self._waf_fp_id = waf_fp_id
        self._waf_dysig = waf_dysig
        self._identity_locked = bool(
            device_id
            or global_id
            or pzid
            or (
                browser_context
                and isinstance(browser_context, dict)
                and any(
                    browser_context.get(key)
                    for key in ("deviceId", "device_id", "globalId", "global_id", "PZid", "pzid")
                )
            )
        )
        self._device_id = device_id or DEFAULT_DEVICE_ID
        self._global_id = global_id or DEFAULT_GLOBAL_ID
        self._pzid = pzid or DEFAULT_PZID
        self._waf_cookies: Dict[str, str] = dict(DEFAULT_COOKIES)
        self._browser_version = DEFAULT_BROWSER_VERSION
        self._user_agent = DEFAULT_USER_AGENT
        self._pz_version = PZ_VERSION
        self._x_sign_version_override = DEFAULT_X_SIGN_VERSION
        self._browser_local_storage: Dict[str, Any] = {}
        self._cdp_url = os.environ.get("PANZHI_CDP_URL", "").strip()
        # ssxmod 默认值
        self._waf_cookies.setdefault("ssxmod_itna", DEFAULT_SSXMOD_ITNA)
        self._waf_cookies.setdefault("ssxmod_itna2", DEFAULT_SSXMOD_ITNA2)

        # ── 调试用 ──
        self._last_list_fetch_debug: Dict[str, Any] = {}
        self._waf_request_info: Dict[str, Any] = {}

        # ── 代理（与 7881 逻辑一致：150s TTL 内复用，过期或验证码则切换）──
        self.proxy = proxy
        self.is_proxy = bool(is_proxy) if is_proxy is not None else (
            bool(is_proxy_enabled()) if callable(is_proxy_enabled) else False
        )
        self._proxy_cache_ip: Optional[str] = None
        self._proxy_cache_time: float = 0.0
        self._current_proxy_ip: str = ""  # 当前使用的代理 ip:port（用于日志观察 IP 使用情况）
        self._www_acw_tc_cache_time: float = 0.0
        self._captcha_refresh_lock = threading.Lock()
        self._last_captcha_refresh_time: float = 0.0
        self._detail_request_lock = threading.Lock()
        self._last_detail_request_time: float = 0.0
        self._proxy_recovery_lock = threading.Lock()
        self._proxy_recovery_until: float = 0.0
        self.timeout = timeout
        self.max_retries = max_retries
        self.verify_ssl = verify_ssl
        self.risk_pause_seconds = risk_pause_seconds

        # ── 调度 ──
        self.detail_concurrency = max(1, detail_concurrency)
        self.requests_per_second = max(0.1, requests_per_second)
        self.queue_size = max(1, queue_size)
        self.batch_size = max(1, batch_size)
        self.max_pages = max(1, max_pages)
        self.jitter_max = max(0.0, jitter_max)
        self.detail_min_interval = max(0.8, 1.0 / self.requests_per_second)

        # ── headers ──
        self._base_headers = dict(DEFAULT_HEADERS)
        if base_headers:
            self._base_headers.update(base_headers)

        # ── browser_context ──
        if browser_context and isinstance(browser_context, dict):
            self.load_browser_context(browser_context)

        # ── 从持久化文件加载 WAF 状态（browser_context 优先）──
        self._load_waf_state()

        # ── 价格缓存（本轮扫描）──
        self._price_cache: Dict[str, float] = {}

    # ── 公共入口 ─────────────────────────────────────────────

    def load_browser_context(self, ctx: Dict[str, Any]) -> None:
        """从浏览器抓包 JSON 加载完整的 WAF 状态。

        ctx 格式（从浏览器 Console 执行 JS 收集）:
            {
                "localStorage": {...},
                "sessionStorage": {...},
                "cookies": "...",           // document.cookie 字符串
                "userAgent": "...",
                "browserVersion": "...",
                "token": "...",
            }
        """
        if not isinstance(ctx, dict):
            return

        # token
        if ctx.get("token"):
            self.token = str(ctx["token"])

        # cookies
        cookie_str = ctx.get("cookie") or ctx.get("cookies") or ""
        if cookie_str:
            parsed = _parse_cookie(cookie_str)
            self._waf_cookies.update(parsed)
            if parsed.get("track_uuid") and not self._waf_cookies.get("new_track_uuid"):
                self._waf_cookies["new_track_uuid"] = parsed["track_uuid"]
            if parsed.get("new_track_uuid") and not self._waf_cookies.get("track_uuid"):
                self._waf_cookies["track_uuid"] = parsed["new_track_uuid"]

        # localStorage → WAF 字段
        ls = ctx.get("localStorage") or {}
        if isinstance(ls, str):
            try:
                ls = json.loads(ls)
            except Exception:
                ls = {}
        if isinstance(ls, dict):
            self._browser_local_storage = {str(k): v for k, v in ls.items()}
            mapping = {
                "_waf_bd8ce2ce37": "_waf_bd",
                "_waf_a86dfdc5f2": "_waf_a_ts",
                "__00b204e9800998__": "_waf_fp_id",
                "api.pzds.com_dySig": "_waf_dysig",
                "www.pzds.com_dySig": "_waf_dysig",
            }
            for storage_key, attr in mapping.items():
                val = ls.get(storage_key)
                if val:
                    setattr(self, attr, str(val))
            if ls.get("track_deviceId") and not (ctx.get("deviceId") or ctx.get("device_id")):
                self._device_id = str(ls["track_deviceId"])

        # sessionStorage → 备用 WAF 字段
        ss = ctx.get("sessionStorage") or {}
        if isinstance(ss, str):
            try:
                ss = json.loads(ss)
            except Exception:
                ss = {}
        if isinstance(ss, dict):
            # 有时 token 在 sessionStorage 里
            if ss.get("token") and not self.token:
                self.token = str(ss["token"])

        # device_id / global_id / pzid
        if ctx.get("deviceId") or ctx.get("device_id"):
            self._device_id = str(ctx.get("deviceId") or ctx.get("device_id"))
        if ctx.get("globalId") or ctx.get("global_id"):
            self._global_id = str(ctx.get("globalId") or ctx.get("global_id"))
        if ctx.get("pzid") or ctx.get("PZid"):
            self._pzid = str(ctx.get("pzid") or ctx.get("PZid"))

        request_identity = ctx.get("requestIdentity") or ctx.get("request_identity") or {}
        if isinstance(request_identity, dict):
            if request_identity.get("deviceId"):
                self._device_id = str(request_identity["deviceId"])
            if request_identity.get("globalId"):
                self._global_id = str(request_identity["globalId"])
            if request_identity.get("PZid") or request_identity.get("pzid"):
                self._pzid = str(request_identity.get("PZid") or request_identity.get("pzid"))
            if request_identity.get("PZVersion"):
                self._pz_version = str(request_identity["PZVersion"])
            if request_identity.get("X-Sign-Version"):
                self._x_sign_version_override = str(request_identity["X-Sign-Version"])
            if request_identity.get("User-Agent"):
                self._user_agent = str(request_identity["User-Agent"])

        browser_version = (
            ctx.get("browserVersion")
            or ctx.get("browser_version")
            or request_identity.get("browserVersion")
        )
        if not browser_version:
            ua_text = str(
                request_identity.get("User-Agent")
                or ctx.get("userAgent")
                or ctx.get("user_agent")
                or self._user_agent
            )
            match = re.search(r"Chrome/(\d+)", ua_text)
            browser_version = match.group(1) if match else ""
        if browser_version:
            self._browser_version = str(browser_version)
            self._user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{self._browser_version}.0.0.0 Safari/537.36"
            )
        elif ctx.get("userAgent") or ctx.get("user_agent"):
            self._user_agent = str(ctx.get("userAgent") or ctx.get("user_agent"))

        # browser fingerprint 暂存（WAF challenge 时用到）
        fp = ctx.get("browserFingerprint") or ctx.get("browser_fingerprint") or {}
        if isinstance(fp, dict) and fp:
            self._waf_request_info["browserFingerprint"] = fp

        env = ctx.get("wafEnv") or ctx.get("waf_env") or ""
        if env:
            self._waf_request_info["wafEnv"] = str(env)
        if ctx.get("cdpUrl") or ctx.get("cdp_url"):
            self._cdp_url = str(ctx.get("cdpUrl") or ctx.get("cdp_url"))

        logger.info(
            "[panzhi] browser_context 已加载: device_id=%s token=%s cookies=%d keys",
            self._device_id[:8] if self._device_id else "-",
            "***" if self.token else "-",
            len(self._waf_cookies),
        )

    # ── WAF 状态持久化（与生产 refresh_waf.py → waf_state.json 一致）──

    def _load_waf_state(self) -> None:
        """从 config/waf_state.json 加载上次保存的 WAF 状态。"""
        state_file = _resolve_waf_state_file()
        if not state_file:
            return
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            # 只在当前仍是默认过期值时才覆盖（browser_context 优先级最高）
            for attr, key in (
                ("_waf_bd", "waf_bd"),
                ("_waf_a_ts", "waf_a_ts"),
                ("_waf_fp_id", "waf_fp_id"),
                ("_waf_dysig", "waf_dysig"),
            ):
                val = data.get(key)
                if val:
                    setattr(self, attr, str(val))
            if not self._identity_locked:
                for attr, key in (
                    ("_device_id", "deviceId"),
                    ("_global_id", "globalId"),
                    ("_pzid", "PZid"),
                ):
                    val = data.get(key)
                    if val:
                        setattr(self, attr, str(val))
            cookie_str = data.get("cookie") or ""
            if cookie_str:
                self._waf_cookies.update(_parse_cookie(cookie_str))
            logger.info(
                "[panzhi] 从 %s 加载 WAF 状态: waf_a_ts=%s",
                os.path.basename(state_file),
                _state_head(self._waf_a_ts),
            )
        except Exception as e:
            logger.warning("[panzhi] 加载 waf_state.json 失败: %s", e)

    def _save_waf_state(self) -> None:
        """将当前 WAF 状态写入 config/waf_state.json。"""
        data: Dict[str, Any] = {
            "waf_bd": self._waf_bd,
            "waf_a_ts": self._waf_a_ts,
            "waf_fp_id": self._waf_fp_id,
            "waf_dysig": self._waf_dysig,
            "cookie": _cookie_header(self._waf_cookies),
            "deviceId": self._device_id,
            "globalId": self._global_id,
            "PZid": self._pzid,
        }
        try:
            os.makedirs(os.path.dirname(WAF_STATE_FILE), exist_ok=True)
            with open(WAF_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("[panzhi] WAF 状态已保存到 %s", os.path.basename(WAF_STATE_FILE))
        except Exception as e:
            logger.warning("[panzhi] 保存 waf_state.json 失败: %s", e)

    def _waf_state_expire_ms(self) -> int:
        """Return the persisted WAF expiry timestamp in milliseconds."""
        text = str(self._waf_a_ts or "")
        if "||" not in text:
            return 0
        _, tail = text.split("||", 1)
        return int(tail) if tail.isdigit() else 0

    def _waf_state_needs_bootstrap(self, *, skew_seconds: int = 300) -> bool:
        """Detect missing or near-expiry WAF state before the first list request."""
        expire_ms = self._waf_state_expire_ms()
        if expire_ms <= 0:
            return True
        return expire_ms <= int((time.time() + skew_seconds) * 1000)

    def run_full(
        self,
        *,
        on_batch: Callable[[Dict[str, Any]], Any],
        on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """全量抓取：order=ASC, sort=null, countFlag=true，翻完所有页。"""
        return self._run_pipeline(
            mode="full",
            max_pages_override=None,
            on_batch=on_batch,
            on_progress=on_progress,
            should_stop=should_stop,
        )

    def run_latest(
        self,
        *,
        pages: int,
        on_batch: Callable[[Dict[str, Any]], Any],
        on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """最新发布：order=DESC, sort=onStandTime, countFlag=false，抓够 pages 页。"""
        return self._run_pipeline(
            mode="latest",
            max_pages_override=max(1, int(pages)),
            on_batch=on_batch,
            on_progress=on_progress,
            should_stop=should_stop,
        )

    # ── 流水线 ───────────────────────────────────────────────

    def _run_pipeline(
        self,
        *,
        mode: str,
        max_pages_override: Optional[int],
        on_batch: Callable[[Dict[str, Any]], Any],
        on_progress: Optional[Callable[[Dict[str, Any]], Any]],
        should_stop: Optional[Callable[[], bool]],
    ) -> Dict[str, Any]:
        import uuid

        task_id = uuid.uuid4().hex
        seen_goods_nos: Set[str] = set()
        batch_buffer: List[Dict[str, Any]] = []
        batch_lock = threading.Lock()

        risk_streak = [0]  # 用 list 实现跨线程可变

        if self._waf_state_needs_bootstrap():
            logger.info(
                "[panzhi] proactive waf bootstrap before first list request: waf_a_ts=%s",
                _state_head(self._waf_a_ts),
            )
            try:
                self._bootstrap_waf_state()
            except Exception:
                logger.exception("[panzhi] proactive WAF bootstrap failed")

        stats: Dict[str, Any] = {
            "task_id": task_id,
            "mode": mode,
            "platform": self.platform,
            "game_id": self.game_id,
            "game_name": self.game_name,
            "list_pages_ok": 0,
            "list_pages_fail": 0,
            "detail_ok": 0,
            "detail_fail": 0,
            "detail_skip": 0,
            "detail_repeat": 0,
            "produced": 0,
            "consumed": 0,
            "list_end_reason": "",
            "risk_paused": False,
        }

        def emit_progress(extra: Optional[Dict[str, Any]] = None) -> None:
            if not on_progress:
                return
            payload = dict(stats)
            if extra:
                payload.update(extra)
            try:
                on_progress(payload)
            except Exception:
                logger.exception("on_progress callback failed")

        def flush_buffer(force: bool = False) -> None:
            nonlocal batch_buffer
            with batch_lock:
                if not batch_buffer:
                    return
                if force or len(batch_buffer) >= self.batch_size:
                    payload = batch_buffer[:]
                    batch_buffer = []
                else:
                    return
            batch_wrapped = {
                "task_id": task_id,
                "platform": self.platform,
                "results": payload,
            }
            try:
                on_batch(batch_wrapped)
            except Exception:
                logger.exception("on_batch callback failed")

        # ── 详情队列 ──
        detail_queue: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue(
            maxsize=self.queue_size
        )

        def detail_worker() -> None:
            """从队列取任务，拉详情页，解析，放入 batch_buffer。"""
            while True:
                try:
                    item = detail_queue.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    if item is None:  # 哨兵
                        return
                    if should_stop and should_stop():
                        stats["detail_skip"] += 1
                        continue

                    result = self._fetch_detail(item)
                    if result is None:
                        stats["detail_fail"] += 1
                        continue
                    if result.get("_proxy_blocked"):
                        stats["detail_fail"] += 1
                        stats["list_end_reason"] = "proxy_budget_exhausted"
                        stats["risk_paused"] = True
                        while not detail_queue.empty():
                            try:
                                detail_queue.get_nowait()
                            except queue.Empty:
                                break
                        return
                    if result.get("_risk"):
                        risk_streak[0] += 1
                        stats["detail_fail"] += 1
                        if risk_streak[0] >= RISK_STOP_THRESHOLD:
                            stats["list_end_reason"] = "risk_control"
                            stats["risk_paused"] = True
                            # 清空队列
                            while not detail_queue.empty():
                                try:
                                    detail_queue.get_nowait()
                                except queue.Empty:
                                    break
                            return
                        continue

                    risk_streak[0] = 0
                    stats["detail_ok"] += 1
                    with batch_lock:
                        batch_buffer.append(result)
                    flush_buffer(force=False)
                    stats["consumed"] += 1
                    emit_progress()
                except Exception:
                    logger.exception("detail_worker error")
                finally:
                    detail_queue.task_done()

        # ── 启动 detail workers ──
        executor = ThreadPoolExecutor(max_workers=self.detail_concurrency)
        futures: List[Future[None]] = []
        for _ in range(self.detail_concurrency):
            futures.append(executor.submit(detail_worker))

        try:
            self._produce_list(
                mode=mode,
                max_pages_override=max_pages_override,
                detail_queue=detail_queue,
                seen_goods_nos=seen_goods_nos,
                stats=stats,
                should_stop=should_stop,
                on_progress=emit_progress,
                batch_buffer=batch_buffer,
                batch_lock=batch_lock,
                flush_buffer=flush_buffer,
            )
        except Exception:
            logger.exception("list producer crashed")
            stats["list_end_reason"] = "producer_exception"
        finally:
            # 发哨兵
            for _ in range(self.detail_concurrency):
                detail_queue.put(None)
            executor.shutdown(wait=True)
            flush_buffer(force=True)

        stats["seen_count"] = len(seen_goods_nos)
        return {
            "task_id": task_id,
            "platform": self.platform,
            "stats": stats,
        }

    # ── 列表生产者 ───────────────────────────────────────────

    def _produce_list(
        self,
        *,
        mode: str,
        max_pages_override: Optional[int],
        detail_queue: queue.Queue,
        seen_goods_nos: Set[str],
        stats: Dict[str, Any],
        should_stop: Optional[Callable[[], bool]],
        on_progress: Callable[[Optional[Dict[str, Any]]], None],
        batch_buffer: List[Dict[str, Any]],
        batch_lock: threading.Lock,
        flush_buffer: Callable[..., None],
    ) -> None:
        page = 1
        prev_page_ids: List[str] = []
        total_pages_hint: Optional[int] = None
        list_fail_streak = 0

        while True:
            if page > self.max_pages:
                stats["list_end_reason"] = "max_pages"
                break
            if max_pages_override is not None and page > max_pages_override:
                stats["list_end_reason"] = "pages_reached"
                break
            if mode == "full" and total_pages_hint is not None and page > total_pages_hint:
                stats["list_end_reason"] = "all_pages_done"
                break
            if should_stop and should_stop():
                stats["list_end_reason"] = "stopped"
                break
            if stats.get("risk_paused"):
                break

            # 翻页速率控制
            if page > 1:
                jitter = random.uniform(0, self.jitter_max)
                time.sleep(1.0 / self.requests_per_second + jitter)

            response_json, err = self._fetch_list_page(page, mode)
            if err:
                stats["list_pages_fail"] += 1
                if err == "captcha_no_proxy":
                    stats["list_end_reason"] = "proxy_budget_exhausted"
                    stats["risk_paused"] = True
                    break
                list_fail_streak += 1
                if list_fail_streak >= 5:
                    stats["list_end_reason"] = "list_repeated_failure"
                    break
                time.sleep(3.0)
                continue

            list_fail_streak = 0
            stats["list_pages_ok"] += 1

            # ── 解析响应 ──
            data_block = (response_json or {}).get("data") or {}
            if not isinstance(data_block, dict):
                data_block = {}
            records = data_block.get("records") or []
            total = data_block.get("total") or 0
            pages_hint = data_block.get("pages")
            if pages_hint is not None:
                total_pages_hint = int(pages_hint)

            logger.info(
                "[panzhi][ip=%s] page=%d/%s mode=%s items=%d total=%s",
                self._current_proxy_ip, page, total_pages_hint or "?", mode, len(records), total,
            )

            # ── 提取本页 goodsNo ──
            current_page_ids: List[str] = []
            batch_nos: List[str] = []
            batch_items: List[Dict[str, Any]] = []
            for raw in records:
                gno = str(raw.get("goodsNo") or "").strip()
                if gno:
                    current_page_ids.append(gno)
                    batch_nos.append(gno)
                    batch_items.append(raw)

            # 与上一页完全一样 → 终止
            if prev_page_ids and current_page_ids == prev_page_ids:
                stats["list_end_reason"] = "duplicate_page"
                break

            # ── 价格对比：批量查 DB ──
            existing_prices = _query_existing_prices(batch_nos)
            # 合并本轮价格缓存
            existing_prices.update(self._price_cache)

            new_count = 0
            for i, raw in enumerate(batch_items):
                gno = batch_nos[i]
                if gno in seen_goods_nos:
                    stats["detail_repeat"] += 1
                    continue
                seen_goods_nos.add(gno)

                list_price = _coerce_price(raw.get("price"))
                # 价格对比：已存在且同价 → 跳过详情，直接用列表数据产出结果
                if gno in existing_prices and list_price is not None:
                    cached_price = existing_prices[gno]
                    if abs(cached_price - list_price) < 0.01:
                        stats["detail_skip"] += 1
                        result = self._build_result_from_list(raw, list_price)
                        self._price_cache[gno] = list_price
                        with batch_lock:
                            batch_buffer.append(result)
                        flush_buffer(force=False)
                        stats["consumed"] += 1
                        continue

                new_count += 1
                enriched = self._build_list_item(raw, list_price)
                if stats.get("risk_paused"):
                    break
                detail_queue.put(enriched)
                stats["produced"] += 1

            if stats.get("risk_paused"):
                break

            # ── 空页处理 ──
            if not records:
                if mode == "latest" and page < (max_pages_override or self.max_pages):
                    page += 1
                    continue
                stats["list_end_reason"] = "empty_list"
                break

            if new_count == 0 and mode != "latest":
                stats["list_end_reason"] = "no_new_ids"
                break

            if len(records) < self.page_size:
                if mode == "latest":
                    if page >= (max_pages_override or self.max_pages):
                        stats["list_end_reason"] = "short_page"
                        break
                else:
                    if total_pages_hint is None or page >= total_pages_hint:
                        stats["list_end_reason"] = "short_page"
                        break

            prev_page_ids = current_page_ids
            page += 1
            on_progress({"page": page, "produced": stats["produced"]})

        if not stats.get("list_end_reason"):
            stats["list_end_reason"] = "completed"

    # ── 列表项构造 ───────────────────────────────────────────

    def _build_list_item(self, raw: Dict[str, Any], list_price: Optional[float]) -> Dict[str, Any]:
        gno = str(raw.get("goodsNo") or "").strip()
        catalogue_id = raw.get("goodsCatalogueId") or raw.get("catalogueId") or self.goods_catalogue_id
        return {
            "goods_no": gno,
            "catalogue_id": int(catalogue_id) if catalogue_id is not None else self.goods_catalogue_id,
            "list_price": list_price,
            "list_title": raw.get("title") or "",
            "list_home_image": raw.get("homeImage") or raw.get("image") or "",
            "game_name": raw.get("gameName") or self.game_name,
            "_raw": raw,
        }

    def _build_result_from_list(
        self,
        raw: Dict[str, Any],
        list_price: Optional[float],
    ) -> Dict[str, Any]:
        """只用列表数据构建结果（价格未变，跳过详情时使用）。"""
        gno = str(raw.get("goodsNo") or "").strip()
        catalogue_id = raw.get("goodsCatalogueId") or raw.get("catalogueId") or self.goods_catalogue_id
        url = DETAIL_URL_TEMPLATE.format(goods_no=gno, catalogue_id=catalogue_id)
        return {
            "product_id": gno,
            "product_info": raw.get("title") or "",
            "original_price": list_price or 0.0,
            "url": url,
            "game_type": raw.get("gameName") or self.game_name,
            "game_id": str(raw.get("gameId") or self.game_id),
            "supports_realname": True,  # 无详情页，保守默认
            "list_title": raw.get("title") or "",
            "list_home_image": raw.get("homeImage") or "",
            "_raw_detail": {},
            "_skip_detail": True,
        }

    # ── 详情抓取 ─────────────────────────────────────────────

    def _fetch_detail(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """GET 商品详情页 HTML，解析后拼装结果。"""
        goods_no = item["goods_no"]
        catalogue_id = item.get("catalogue_id", self.goods_catalogue_id)
        game_id = item.get("_raw", {}).get("gameId") or self.game_id
        detail_url = DETAIL_URL_TEMPLATE.format(goods_no=goods_no, catalogue_id=catalogue_id)

        # 详情页用 www.pzds.com 的 cookie（排除 api.pzds.com 专用的 acw_tc）
        site_cookies = {
            k: v for k, v in self._waf_cookies.items()
            if k not in ("acw_tc", "sso")
        }
        # 补充 track_uuid（用于反爬追踪）
        if self._global_id and "new_track_uuid" not in site_cookies:
            site_cookies["new_track_uuid"] = self._global_id
        if "track_uuid" not in site_cookies and self._global_id:
            site_cookies["track_uuid"] = self._global_id[:32] if len(self._global_id) >= 32 else self._global_id

        detail_headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"https://www.pzds.com/goodsList/{game_id}",
            "sec-ch-ua": f'"Chromium";v="{DEFAULT_BROWSER_VERSION}", "Not=A?Brand";v="24", "Google Chrome";v="{DEFAULT_BROWSER_VERSION}"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        cookie_str = _cookie_header(site_cookies)
        if cookie_str:
            detail_headers["Cookie"] = cookie_str

        # 先拿 www.pzds.com 的 acw_tc
        self._refresh_www_acw_tc()

        html = None
        for attempt in range(self.max_retries):
            self._throttle_detail_request()
            # 每轮重试刷新 cookie header（acw_tc 可能已更新）
            cookie_str = _cookie_header({
                k: v for k, v in self._waf_cookies.items()
                if k not in ("sso",)
            })
            if cookie_str:
                detail_headers["Cookie"] = cookie_str
            proxies = self._resolve_proxy()
            try:
                # ★ www.pzds.com 用普通 requests（curl_cffi 的 TLS 指纹反而触发 WAF）
                resp = _fallback_requests.get(
                    detail_url,
                    headers=detail_headers,
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                    proxies=proxies,
                )
                # 验证码检测 → 切换代理 + 刷新 WAF 后重试
                if _is_panzhi_captcha(resp.text):
                    logger.warning("[panzhi] detail 验证码 goods_no=%s，切换代理", goods_no)
                    new_proxies = self._rotate_proxy_after_captcha(reason=f"detail_captcha_{goods_no}")
                    if new_proxies is None:
                        return {"_proxy_blocked": True, "goods_no": goods_no}
                    proxies = new_proxies  # ★ 用新 proxies 重试
                    if attempt < self.max_retries - 1:
                        continue
                    return None
                if resp.status_code == 403:
                    logger.warning("[panzhi] detail 403 goods_no=%s", goods_no)
                    return {"_risk": True}
                if resp.status_code != 200:
                    logger.warning("[panzhi] detail %d goods_no=%s", resp.status_code, goods_no)
                    if attempt < self.max_retries - 1:
                        time.sleep((attempt + 1) * 2.0)
                        continue
                    return None
                html = resp.text
                break
            except Exception as e:
                logger.warning("[panzhi] detail network error goods_no=%s: %s", goods_no, e)
                if self._handle_proxy_connect_failure(reason=f"detail_network_{goods_no}", exc=e):
                    continue
                if attempt < self.max_retries - 1:
                    time.sleep((attempt + 1) * 2.0)
                    continue
                return None

        if not html:
            return None

        # ── 解析 HTML ──
        table_data, desc = _parse_detail_html(html)
        product_info = _build_product_info(table_data, desc, self.game_name)
        supports_realname = _detect_realname(table_data, desc)

        price = (
            _coerce_price(item.get("list_price"))
            or _coerce_price(table_data.get("价格"))
        )

        url = DETAIL_URL_TEMPLATE.format(goods_no=goods_no, catalogue_id=catalogue_id)

        logger.info(
            "[panzhi][ip=%s] detail ok: goods_no=%s price=%s title=%s",
            self._current_proxy_ip, goods_no, price,
            product_info[:60].replace("\n", " ") if product_info else "-",
        )

        # ── 写入价格缓存 ──
        if price is not None:
            self._price_cache[goods_no] = price

        return {
            "product_id": goods_no,
            "product_info": product_info,
            "original_price": price or 0.0,
            "url": url,
            "game_type": item.get("game_name") or self.game_name,
            "game_id": str(self.game_id),
            "supports_realname": supports_realname,
            "base_infos": {k: v for k, v in table_data.items() if k != "商品描述"},
            "description": desc,
            "list_title": item.get("list_title") or "",
            "list_home_image": item.get("list_home_image") or "",
            "_raw_detail": table_data,
        }

    # ── 列表页请求 ───────────────────────────────────────────

    def _build_list_body(self, page: int, mode: str) -> Dict[str, Any]:
        """构建列表请求 body。"""
        body: Dict[str, Any] = {
            "order": "DESC" if mode == "latest" else "ASC",
            "sort": "onStandTime" if mode == "latest" else None,
            "page": page,
            "pageSize": self.page_size,
            "action": {
                "gameId": self.game_id,
                "merchantMark": None,
                "keywords": [],
                "searchWords": [],
                "searchPropertyIds": [],
                "recommendSearchConfigIds": [],
                "unionGameIds": [],
                "goodsSearchActions": [],
                "goodsCatalogueId": self.goods_catalogue_id,
                "goodsSubCatalogueIds": [],
                "countFlag": False,
                "conditionSearch": False,
            },
        }
        return body

    def _build_headers(self, signature: Dict[str, Any]) -> Dict[str, str]:
        """构建列表请求的完整 headers。"""
        headers = dict(self._base_headers)
        headers["User-Agent"] = self._user_agent
        headers["sec-ch-ua"] = (
            f'"Chromium";v="{self._browser_version}", '
            f'"Not=A?Brand";v="24", '
            f'"Google Chrome";v="{self._browser_version}"'
        )
        headers["PZTimestamp"] = str(signature["PZTimestamp"])
        headers["Random"] = str(signature["Random"])
        headers["Sign"] = str(signature["Sign"])
        headers["PZVersion"] = self._pz_version
        headers["deviceId"] = self._device_id
        headers["globalId"] = self._global_id
        # ★ goodsPublic/page 是匿名接口：浏览器不发送 PZid（用户 id）。
        # 发送 PZid 会让服务端按登录态校验 → NOT_LOGGED_IN（登录失效）。
        if self.token:
            headers["token"] = self.token
        sign_version = (
            signature.get("X-Sign-Version")
            or signature.get("version")
            or self._x_sign_version_override
        )
        if sign_version:
            headers["X-Sign-Version"] = str(sign_version)
        return headers

    # ── 代理（已迁移到 ProxyManager，本类保留薄壳方便调用）────────

    @staticmethod
    def _extract_proxy_ip(proxies: Optional[Dict[str, str]]) -> str:
        """从 proxy dict 提取 ip:port（用于日志观察 IP 使用情况）。"""
        if not proxies:
            return ""
        url = str(proxies.get("http") or proxies.get("https") or "")
        if not url:
            return ""
        try:
            parsed = urllib.parse.urlparse(url)
            host = parsed.hostname or ""
            port = parsed.port
            return f"{host}:{port}" if port else host
        except Exception:
            return ""

    def _resolve_proxy(self, force_refresh: bool = False, reason: str = "") -> Optional[Dict[str, str]]:
        """获取当前代理。TTL 内复用，captcha 触发 force_refresh 时走 ProxyManager.force_rotate。

        WAF cookie（_waf_cookies）独立管理；captcha 切 IP 后会刷新 WAF 状态再重试。

        Returns:
            {"http": "http://...", "https": "http://..."} or None（直连）
        """
        if not self.is_proxy:
            self._current_proxy_ip = ""
            return None
        if self.proxy:
            fixed = {"http": self.proxy, "https": self.proxy}
            self._current_proxy_ip = self._extract_proxy_ip(fixed)
            return fixed

        self._wait_proxy_recovery_if_needed(reason=reason or "get_proxy")
        from services.proxy_manager import get_manager as _get_pm
        mgr = _get_pm("panzhi")

        # 最多等待 300s（5 分钟），超时降级直连
        max_wait = 300
        waited = 0
        while True:
            proxies = (
                mgr.force_rotate(reason=reason or "panzhi_captcha")
                if force_refresh else mgr.get_proxy()
            )
            wait_seconds = mgr.paid_cooldown_remaining()
            if proxies is not None:
                self._current_proxy_ip = self._extract_proxy_ip(proxies)
                logger.info("[panzhi][ip=%s] 获取代理成功, reason=%s", self._current_proxy_ip, reason)
                return proxies
            if wait_seconds <= 0:
                logger.info("[panzhi] 代理不可用（扣费失败/未开通/付费墙），使用直连, reason=%s", reason)
                self._current_proxy_ip = ""
                return None
            if waited >= max_wait:
                logger.warning(
                    "[panzhi] 扣费接口持续冷却超过 %ds，放弃等待，使用直连, reason=%s",
                    max_wait, reason,
                )
                self._current_proxy_ip = ""
                return None
            logger.warning(
                "[panzhi] 扣费接口冷却中，等待 %.1fs 后重试（已等 %.1fs/%ds）, reason=%s",
                wait_seconds, waited, max_wait, reason,
            )
            time.sleep(min(wait_seconds, 30))  # 每次最多睡 30s
            waited += wait_seconds

    def _throttle_detail_request(self) -> None:
        """Throttle detail requests across worker threads to reduce Panzhi captcha bursts."""
        with self._detail_request_lock:
            now = time.monotonic()
            wait_seconds = self.detail_min_interval - (now - self._last_detail_request_time)
            if wait_seconds > 0:
                time.sleep(wait_seconds + random.uniform(0, min(self.jitter_max, 0.5)))
            self._last_detail_request_time = time.monotonic()

    def _rotate_proxy_after_captcha(self, reason: str) -> Optional[Dict[str, str]]:
        """Serialize captcha recovery so concurrent detail workers do not burn proxy budget."""
        with self._captcha_refresh_lock:
            now = time.monotonic()
            if now - self._last_captcha_refresh_time < 5.0:
                return self._resolve_proxy()
            new_proxies = self._resolve_proxy(force_refresh=True, reason=reason)
            if new_proxies is None:
                return None
            self._refresh_waf_after_captcha(reason=reason)
            self._last_captcha_refresh_time = time.monotonic()
            return new_proxies

    @staticmethod
    def _is_proxy_connect_error(exc: Exception) -> bool:
        text = f"{type(exc).__name__}: {exc}".lower()
        markers = (
            "proxyerror",
            "unable to connect to proxy",
            "failed to connect",
            "could not connect to server",
            "connection refused",
            "winerror 10061",
            "curl: (7)",
        )
        return any(marker in text for marker in markers)

    def _proxy_ttl_seconds(self) -> float:
        try:
            from services.proxy_manager import get_manager as _get_pm
            return max(1.0, float(_get_pm("panzhi").cache_ttl_seconds()))
        except Exception:
            return 150.0

    def _wait_proxy_recovery_if_needed(self, reason: str) -> None:
        wait_seconds = 0.0
        with self._proxy_recovery_lock:
            wait_seconds = max(0.0, self._proxy_recovery_until - time.monotonic())
        if wait_seconds > 0:
            logger.warning(
                "[panzhi] 代理恢复等待中, reason=%s, %.1fs 后重新取代理",
                reason or "-",
                wait_seconds,
            )
            time.sleep(wait_seconds)

    def _handle_proxy_connect_failure(self, reason: str, exc: Exception) -> bool:
        if not self.is_proxy or self.proxy or not self._is_proxy_connect_error(exc):
            return False

        wait_seconds = self._proxy_ttl_seconds()
        try:
            from services.proxy_manager import get_manager as _get_pm
            _get_pm("panzhi").invalidate_cache(reason=reason)
        except Exception as invalidate_exc:
            logger.warning("[panzhi] 代理缓存作废失败 reason=%s: %s", reason, invalidate_exc)

        with self._proxy_recovery_lock:
            self._proxy_recovery_until = max(
                self._proxy_recovery_until,
                time.monotonic() + wait_seconds,
            )
        logger.warning(
            "[panzhi] 代理连接失败，等待 %.1fs 后再继续抓取, reason=%s",
            wait_seconds,
            reason,
        )
        self._wait_proxy_recovery_if_needed(reason=reason)
        return True

    def _refresh_waf_after_captcha(self, reason: str = "") -> bool:
        """验证码后切 IP 并重新挑战，生成跟新出口 IP 绑定的 WAF 状态。

        旧实现只刷新 acw_tc、不刷新 _waf_bd/_waf_a_ts/_waf_fp_id/_waf_dysig，
        而这些状态跟挑战时的出口 IP 绑定；切 IP 后仍携带旧 IP 挑战出的状态，
        盼之 WAF 不认 → 反复触发验证码。

        列表页 goodsPublic/page 与详情页 goodsDetails 都是公开接口，
        bootstrap（空 localStorage 重新挑战）生成全新匿名状态是正确的。
        """
        logger.info("[panzhi] captcha 后刷新 acw_tc + WAF bootstrap reason=%s", reason or "-")
        self._waf_cookies.pop("acw_tc", None)
        self._www_acw_tc_cache_time = 0.0

        try:
            self._refresh_acw_tc()
        except Exception as e:
            logger.warning("[panzhi] captcha 后 acw_tc 刷新失败: %s", e)

        # ★ 重新挑战：生成跟当前（新）代理 IP 绑定的 WAF 状态
        try:
            if self._bootstrap_waf_state():
                return True
        except Exception as e:
            logger.warning("[panzhi] captcha 后 WAF bootstrap 失败: %s", e)

        return bool(self._waf_cookies.get("acw_tc"))

    # ── www.pzds.com acw_tc 刷新 ─────────────────────────────

    def _refresh_www_acw_tc(self) -> bool:
        """访问 www.pzds.com 首页获取 acw_tc（用于详情页请求）。150s 内复用。"""
        now = time.monotonic()
        if self._waf_cookies.get("acw_tc") and (now - self._www_acw_tc_cache_time) < 150:
            return True  # 缓存有效
        try:
            resp = _fallback_requests.get(
                "https://www.pzds.com/",
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=15,
                verify=self.verify_ssl,
            )
            set_cookie = resp.headers.get("set-cookie") or resp.headers.get("Set-Cookie") or ""
            if set_cookie:
                for part in set_cookie.split(","):
                    part = part.strip()
                    if "acw_tc=" in part:
                        val = part.split("acw_tc=", 1)[1].split(";", 1)[0].strip()
                        if val:
                            self._waf_cookies["acw_tc"] = val
                            self._www_acw_tc_cache_time = now
                            return True
        except Exception as e:
            logger.warning("[panzhi] www acw_tc 刷新失败: %s", e)
        return False

    # ── acw_tc 刷新 (GET /api/server/time) ────────────────────

    def _refresh_acw_tc(self) -> bool:
        """请求 /api/server/time 获取/刷新 acw_tc cookie。

        与生产 panzhi_publish_helper.py._refresh_acw_tc() 一致。
        """
        import hashlib

        ts = int(time.time() * 1000)
        rnd = int("".join(str(random.randint(1, 9)) for _ in range(6)))
        sign_source = f"PZTimestamp={ts}&Random={rnd}&accessKey=3qXyB7uf"
        md5_sign = hashlib.md5(sign_source.encode()).hexdigest()

        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": DEFAULT_USER_AGENT,
            "PZTimestamp": str(ts),
            "Random": str(rnd),
            "Sign": md5_sign,
            "PZVersion": PZ_VERSION,
            "PZVersionCode": "1",
            "Skey": "CLIENT",
            "PZPlatform": "pc",
            "PZOs": "windows",
            "channelInfo": CHANNEL_INFO,
            "deviceId": self._device_id,
            "globalId": self._global_id,
            "Referer": "https://www.pzds.com/",
        }
        if self.token:
            headers["token"] = self.token
        cookie_str = _cookie_header(self._waf_cookies)
        if cookie_str:
            headers["Cookie"] = cookie_str

        try:
            proxies = self._resolve_proxy()
            if HAS_CURL_CFFI:
                resp = curl_requests.get(
                    API_SERVER_TIME,
                    headers=headers,
                    timeout=15,
                    verify=self.verify_ssl,
                    impersonate=f"chrome{DEFAULT_BROWSER_VERSION}",
                    proxies=proxies,
                )
            else:
                resp = _fallback_requests.get(
                    API_SERVER_TIME, headers=headers, timeout=15, verify=self.verify_ssl,
                    proxies=proxies,
                )
        except Exception:
            return False

        # 从 Set-Cookie 提取新 cookie
        new_cookies: Dict[str, str] = {}
        set_cookie = resp.headers.get("set-cookie") or resp.headers.get("Set-Cookie") or ""
        if set_cookie:
            for part in set_cookie.split(","):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    k = k.split(";")[0].strip()
                    if k:
                        new_cookies[k] = v.split(";")[0].strip()
        # 也检查 response.cookies
        try:
            for k, v in resp.cookies.items():
                new_cookies[k] = v
        except Exception:
            pass

        self._waf_cookies.update(new_cookies)
        return bool(new_cookies.get("acw_tc"))

    # ── ssxmod 生成 ───────────────────────────────────────────

    def _generate_ssxmod(self) -> None:
        """生成 ssxmod_itna / ssxmod_itna2 cookie。

        与生产 panzhi_publish_helper.py._generate_ssxmod() 一致。
        """
        if not generate_cookies:
            return
        now = int(time.time() * 1000)
        waf_fp_id = _decoded_state_head(self._waf_fp_id)
        waf_ts = _state_ts(self._waf_a_ts, now)

        fingerprint_data = WAF_FINGERPRINT_TEMPLATE.format(now=waf_ts)
        event_data = WAF_EVENT_TEMPLATE.format(now=waf_ts)
        if waf_fp_id:
            fingerprint_data = fingerprint_data.split("^", 1)[0] + "^" + fingerprint_data.split("^", 1)[1] if "^" in fingerprint_data else fingerprint_data
            # replace first field with fp_id
            parts = fingerprint_data.split("^", 1)
            if len(parts) == 2:
                fingerprint_data = f"{waf_fp_id}^{parts[1]}"
            parts2 = event_data.split("^", 1)
            if len(parts2) == 2:
                event_data = f"{waf_fp_id}^{parts2[1]}"

        try:
            result = generate_cookies(fingerprint_data, event_data)
        except TypeError:
            result = generate_cookies(fingerprint_data)
        except Exception:
            return

        if isinstance(result, dict):
            if result.get("ssxmod_itna"):
                self._waf_cookies["ssxmod_itna"] = str(result["ssxmod_itna"])
            if result.get("ssxmod_itna2"):
                self._waf_cookies["ssxmod_itna2"] = str(result["ssxmod_itna2"])

    # ── WAF 状态自愈 ─────────────────────────────────────────

    # 与生产 refresh_waf.py 一致：空 localStorage 请求 saveGoods?decode__1174=init
    _WAF_BOOTSTRAP_URL = "https://api.pzds.com/api/web-client/v2/userCenter/saveGoods"

    def _bootstrap_waf_state(self) -> bool:
        """模仿生产 refresh_waf.py：saveGoods?decode__1174=init + 空 localStorage。

        关键：传 {} 作为 localStorage，WAF 才会生成全新状态（而非返回过期值）。
        """
        logger.info("[panzhi] WAF bootstrap: POST saveGoods?decode__1174=init ...")
        proxies = self._resolve_proxy()
        try:
            if HAS_CURL_CFFI:
                resp = curl_requests.post(
                    f"{self._WAF_BOOTSTRAP_URL}?decode__1174=init",
                    data="{}",
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": DEFAULT_USER_AGENT,
                        "Referer": "https://www.pzds.com/",
                    },
                    timeout=30,
                    verify=self.verify_ssl,
                    impersonate=f"chrome{DEFAULT_BROWSER_VERSION}",
                    proxies=proxies,
                )
            else:
                resp = _fallback_requests.post(
                    f"{self._WAF_BOOTSTRAP_URL}?decode__1174=init",
                    data="{}",
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": DEFAULT_USER_AGENT,
                        "Referer": "https://www.pzds.com/",
                    },
                    timeout=30,
                    verify=self.verify_ssl,
                    proxies=proxies,
                )
        except Exception as e:
            logger.warning("[panzhi] WAF bootstrap 请求失败: %s", e)
            return False

        html = resp.text
        # 验证码检测 → 切换代理
        if _is_panzhi_captcha(html):
            logger.warning("[panzhi] WAF bootstrap 触发验证码，放弃本次 WAF 刷新")
            return False

        if "aliyun_waf" not in html:
            logger.warning("[panzhi] WAF bootstrap: 未触发 challenge")
            return False

        logger.info("[panzhi] WAF bootstrap: 收到 challenge (len=%d)，空 localStorage 执行...", len(html))
        try:
            result = run_challenge(
                html,
                page_url=self._WAF_BOOTSTRAP_URL,
                local_storage_data={},  # ★ 空！让 WAF 生成全新状态
                cookie_data="",
                browser_fingerprint={},
                force_env="",
                timeout=60,
            )
        except Exception as e:
            logger.warning("[panzhi] WAF bootstrap challenge 失败: %s", e)
            return False

        return self._merge_waf_result(result)

    def _fetch_list_page(
        self, page: int, mode: str = "full"
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """请求一页列表数据。自动处理 WAF challenge 重试。

        流程（与生产 publish_game 一致）：
        1. 刷新 acw_tc cookie
        2. 每轮重试：生成 ssxmod → 生成 decode__1174 → v18 签名 → 发请求
        3. 遇到 WAF challenge → bootstrap 刷新状态 → 下一轮重试
        """
        body = self._build_list_body(page, mode)
        body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))

        max_attempts = max(1, int(os.environ.get("PZ_WAF_MAX_ATTEMPTS", "3")))
        last_err: Optional[str] = None

        for attempt in range(max_attempts):
            # ── 0. 刷新 acw_tc + ssxmod（每轮重试都刷新，确保和最新 WAF 状态匹配）──
            if not self._waf_cookies.get("acw_tc") or attempt > 0:
                self._refresh_acw_tc()
            if attempt == 0 or os.environ.get("PZ_REGEN_SSXMOD_EACH_ATTEMPT") == "1":
                self._generate_ssxmod()

            # 1. 生成 decode__1174
            try:
                decode_value = generate_decode_value(
                    self.list_url,
                    body_text,
                    method="POST",
                    cookie_data=_cookie_header(self._waf_cookies),
                    local_storage_data=self._build_local_storage(),
                    request_info_data=self._waf_request_info or None,
                    page_url="https://www.pzds.com/",
                    timeout=30,
                )
            except Exception as e:
                logger.warning(
                    "[panzhi] decode 生成失败 (attempt %d/%d): %s",
                    attempt + 1, max_attempts, str(e)[:120],
                )
                if attempt < max_attempts - 1:
                    # WAF 状态过期 → 故意触发 challenge 来刷新
                    if self._bootstrap_waf_state():
                        logger.info("[panzhi] WAF bootstrap 成功，重试 decode")
                        continue
                last_err = f"decode_failed:{e}"
                continue

            # 2. 拼接 URL
            url = append_decode_param(self.list_url, decode_value)

            # 3. 签名
            try:
                signature = generate_sign(body, method="POST")
            except Exception as e:
                return None, f"sign_failed:{e}"

            # 4. headers
            headers = self._build_headers(signature)
            cookie_str = _cookie_header(self._waf_cookies)
            if cookie_str:
                headers["Cookie"] = cookie_str

            # 5. 发请求
            debug_info: Dict[str, Any] = {
                "page": page,
                "mode": mode,
                "attempt": attempt + 1,
                "decode_len": len(decode_value),
                "decode_prefix": decode_value[:16] if decode_value else "",
                "url": url[:120],
            }

            proxies = self._resolve_proxy()
            try:
                if HAS_CURL_CFFI:
                    resp = curl_requests.post(
                        url,
                        data=body_text.encode("utf-8"),
                        headers=headers,
                        timeout=self.timeout,
                        verify=self.verify_ssl,
                        impersonate=f"chrome{DEFAULT_BROWSER_VERSION}",
                        proxies=proxies,
                    )
                else:
                    resp = _fallback_requests.post(
                        url,
                        data=body_text.encode("utf-8"),
                        headers=headers,
                        timeout=self.timeout,
                        verify=self.verify_ssl,
                        proxies=proxies,
                    )
            except Exception as e:
                last_err = f"network:{type(e).__name__}"
                debug_info["error"] = last_err
                logger.warning("[panzhi] list page=%d network error: %s", page, e)
                self._last_list_fetch_debug = debug_info
                if self._handle_proxy_connect_failure(reason=f"list_p{page}_network", exc=e):
                    continue
                if attempt < max_attempts - 1:
                    time.sleep(2.0)
                    continue
                return None, last_err

            text = resp.text
            debug_info["status"] = resp.status_code
            debug_info["text_len"] = len(text)
            self._last_list_fetch_debug = debug_info

            # 6. 检测验证码 → 切换代理 + 刷新 WAF 后重试
            if _is_panzhi_captcha(text):
                logger.warning("[panzhi] list page=%d 触发验证码，切换代理", page)
                new_proxies = self._rotate_proxy_after_captcha(reason=f"list_p{page}_a{attempt+1}")
                if new_proxies is None:
                    # budget 满 / 扣费失败 / 付费墙生效 → 当次 list 请求放弃, 让外层换页
                    logger.warning("[panzhi] captcha + budget/扣费失败, 放弃本 list page=%d", page)
                    return None, "captcha_no_proxy"
                proxies = new_proxies  # ★ 用新 proxies 重试
                if attempt < max_attempts - 1:
                    time.sleep(2.0)
                    continue
                last_err = "captcha"
                continue

            # 7. 检测 WAF challenge → 用 bootstrap 刷新状态（saveGoods + 空 localStorage）
            if _is_waf_challenge(text):
                logger.warning("[panzhi] list page=%d WAF challenge (attempt %d/%d)", page, attempt + 1, max_attempts)
                if attempt < max_attempts - 1:
                    try:
                        self._bootstrap_waf_state()
                    except Exception as e:
                        logger.warning("[panzhi] WAF 刷新失败: %s", e)
                    time.sleep(1.0)
                    continue
                last_err = "waf_challenge"
                continue

            # 8. 检测滑块验证码 (AliyunCaptcha.js)
            if _is_captcha(text):
                logger.warning("[panzhi] list page=%d 触发滑块验证码!", page)
                return None, "captcha"

            # 9. 解析 JSON
            try:
                data = resp.json()
            except Exception:
                # 非 JSON，可能是新的 WAF bd
                next_bd = _extract_waf_bd(text)
                if next_bd and attempt < max_attempts - 1:
                    logger.info("[panzhi] list page=%d 检测到新 waf_bd，刷新后重试", page)
                    self._waf_bd = next_bd
                    continue
                logger.warning("[panzhi] list page=%d 非 JSON 响应 (len=%d)", page, len(text))
                last_err = "non_json"
                continue

            # 9. 成功
            return data, None

        return None, last_err or "max_attempts"

    # ── WAF 状态管理 ─────────────────────────────────────────

    # WAF 动态脚本缓存 key。浏览器快照里存的是过期脚本（如 103KB 的
    # _waf_a23a0b772），一旦传给 decode runner，会被当作 scriptSource 优先执行，
    # 导致执行旧脚本卡死超时（30s）。必须排除，让 runner 读本地 scriptPath。
    _WAF_DYNAMIC_SCRIPT_KEYS = frozenset(("_waf_a23a0b772", "_waf_3d7faf79"))

    def _build_local_storage(self) -> Dict[str, str]:
        """构建 decode__1174 生成所需的 localStorage 数据。"""
        merged: Dict[str, str] = {
            str(k): str(v)
            for k, v in self._browser_local_storage.items()
            if v not in (None, "") and k not in self._WAF_DYNAMIC_SCRIPT_KEYS
        }
        merged.update({
            "_waf_bd8ce2ce37": self._waf_bd,
            "_waf_a86dfdc5f2": self._waf_a_ts,
            "__00b204e9800998__": _decoded_state_head(self._waf_fp_id),
            "api.pzds.com_dySig": self._waf_dysig,
        })
        return merged

    def _merge_waf_result(self, result: Dict[str, Any]) -> bool:
        """从 challenge runner 返回值中提取并更新 WAF 状态。

        与生产 refresh_waf.py 逻辑一致：
        1. 从 localStorage 提取 waf_bd / waf_a_ts / waf_fp_id / waf_dysig
        2. 从 globals 中提取原始值（更权威），自动拼接 ||expire_ts
        3. 合并新 cookie
        返回 True 表示状态已更新。
        """
        updated = False

        # ── 1. localStorage（URL 编码格式，直接使用）──
        ls = result.get("localStorage") or {}
        if isinstance(ls, dict):
            for storage_key, attr in (
                ("_waf_bd8ce2ce37", "_waf_bd"),
                ("_waf_a86dfdc5f2", "_waf_a_ts"),
                ("__00b204e9800998__", "_waf_fp_id"),
                ("api.pzds.com_dySig", "_waf_dysig"),
                ("www.pzds.com_dySig", "_waf_dysig"),
            ):
                val = ls.get(storage_key)
                if val:
                    setattr(self, attr, str(val))
                    updated = True

        # ── 2. globals（原始解码值，重新编码并拼接时间戳）──
        globals_data = result.get("globals") or {}
        if isinstance(globals_data, dict):
            raw_bd = str(globals_data.get("_waf_bd8ce2ce37") or "")
            raw_ts = str(globals_data.get("_waf_a86dfdc5f2") or "")
            if raw_bd and raw_ts.isdigit():
                expire_ts = str(int(raw_ts) + 12 * 60 * 60 * 1000)
                self._waf_bd = f"{urllib.parse.quote(raw_bd, safe='')}||{expire_ts}"
                self._waf_a_ts = f"{raw_ts}||{expire_ts}"
                self._waf_dysig = f"true||{expire_ts}"
                updated = True
            # 其他 globals key
            for g_key, attr in (
                ("__00b204e9800998__", "_waf_fp_id"),
                ("api.pzds.com_dySig", "_waf_dysig"),
                ("www.pzds.com_dySig", "_waf_dysig"),
            ):
                val = globals_data.get(g_key)
                if val:
                    setattr(self, attr, str(val))
                    updated = True

        # ── 3. cookies ──
        new_cookie = result.get("cookie") or ""
        if new_cookie:
            self._waf_cookies.update(_parse_cookie(new_cookie))
            updated = True

        bd_head = _state_head(self._waf_bd)
        ts_head = _state_head(self._waf_a_ts)
        logger.info(
            "[panzhi] WAF 状态已更新: waf_bd=%s waf_a_ts=%s cookies=%d updated=%s",
            bd_head[:20] if bd_head else "-",
            ts_head,
            len(self._waf_cookies),
            updated,
        )
        if updated:
            self._save_waf_state()
        return updated

    def _handle_waf_challenge(self, html: str) -> None:
        """执行 WAF challenge（常规请求被拦截时）。"""
        result = run_challenge(
            html,
            page_url=self.list_url,
            local_storage_data=self._build_local_storage(),
            cookie_data=_cookie_header(self._waf_cookies),
            browser_fingerprint=self._waf_request_info.get("browserFingerprint") or {},
            force_env=str(self._waf_request_info.get("wafEnv") or ""),
            timeout=60,
        )
        self._merge_waf_result(result)


__all__ = [
    "PanzhiAccountCrawler",
    "DEFAULT_LIST_URL",
    "DETAIL_URL_TEMPLATE",
    "DEFAULT_HEADERS",
    "DEFAULT_COOKIES",
    "DEFAULT_USER_AGENT",
    "_query_existing_prices",
    "_parse_detail_html",
    "_build_product_info",
]
