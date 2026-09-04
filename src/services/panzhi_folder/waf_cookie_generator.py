#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盼之代售 (panzhi.com) WAF Cookie 生成器。

基于 panzhi_waf_cookie_generator.py 移植，并保留其完整行为：
静态浏览器指纹模板、可选 g4f 指纹生成、custom_decode、批量生成与
异步 cookie 刷新管理器。

核心算法:
    1. 浏览器指纹数据 →  ^ 分隔的字段列表
    2. 随机化 hash 字段（模拟不同浏览器会话）
    3. LZW 压缩 → 自定义 Base64 编码 → ssxmod_itna
    4. 精简字段 + 再压缩编码 → ssxmod_itna2

用法:
    from services.panzhi_folder.waf_cookie_generator import generate_cookies

    cookies = generate_cookies()
    # → {"ssxmod_itna": "1-Cu0QY5D...", "ssxmod_itna2": "1-Cu0QY5D...", "timestamp": 17...}
"""

import asyncio
import random
import time
from typing import Any, Callable, Dict, List, Optional, Union

try:
    from g4f.Provider.qwen.fingerprint import generate_fingerprint as _generate_fingerprint  # type: ignore
except ModuleNotFoundError:
    _generate_fingerprint = None

# ═══════════════════════════════════════════════════════════════
# 配置常量
# ═══════════════════════════════════════════════════════════════

# 自定义 Base64 字符集（盼之专用，与标准 Base64 不同）
CUSTOM_BASE64_CHARS = "DGi0YA7BemWnQjCl4_bR3f8SKIF9tUz/xhr2oEOgPpac=61ZqwTudLkM5vHyNXsVJ"

# 需要随机化的 hash 字段位置及其类型
#   "split" : "count|hash" 格式，只替换 hash 部分
#   "full"  : 直接替换为随机 uint32
HASH_FIELDS: Dict[int, str] = {
    16: "split",  # plugin hash
    17: "full",   # canvas hash
    18: "full",   # UA hash 1
    31: "full",   # UA hash 2
    34: "full",   # URL hash
    36: "full",   # doc attribute hash → 随机 10-100
}

# ── WAF 浏览器指纹模板（来自 panzhi_publish_helper.py）──
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


# ═══════════════════════════════════════════════════════════════
# LZW 压缩（JS-faithful port）
# ═══════════════════════════════════════════════════════════════

def lzw_compress(data: Optional[str], bits: int, char_func: Callable[[int], str]) -> str:
    """JS LZW 压缩算法的 Python 逐行翻译。

    Args:
        data: 待压缩字符串
        bits: 输出每组位数（固定 6，与自定义 Base64 匹配）
        char_func: 索引 → 字符的映射函数

    Returns:
        压缩后的字符串
    """
    if data is None:
        return ""

    dictionary: Dict[str, int] = {}
    dict_to_create: Dict[str, bool] = {}
    c = ""
    wc = ""
    w = ""
    enlarge_in = 2
    dict_size = 3
    num_bits = 2
    result: List[str] = []
    value = 0
    position = 0

    for i in range(len(data)):
        c = data[i]
        if c not in dictionary:
            dictionary[c] = dict_size
            dict_size += 1
            dict_to_create[c] = True
        wc = w + c
        if wc in dictionary:
            w = wc
        else:
            if w in dict_to_create:
                if ord(w[0]) < 256:
                    for _ in range(num_bits):
                        value = (value << 1)
                        if position == bits - 1:
                            position = 0
                            result.append(char_func(value))
                            value = 0
                        else:
                            position += 1
                    char_code = ord(w[0])
                    for _ in range(8):
                        value = (value << 1) | (char_code & 1)
                        if position == bits - 1:
                            position = 0
                            result.append(char_func(value))
                            value = 0
                        else:
                            position += 1
                        char_code >>= 1
                else:
                    char_code = 1
                    for _ in range(num_bits):
                        value = (value << 1) | char_code
                        if position == bits - 1:
                            position = 0
                            result.append(char_func(value))
                            value = 0
                        else:
                            position += 1
                        char_code = 0
                    char_code = ord(w[0])
                    for _ in range(16):
                        value = (value << 1) | (char_code & 1)
                        if position == bits - 1:
                            position = 0
                            result.append(char_func(value))
                            value = 0
                        else:
                            position += 1
                        char_code >>= 1
                enlarge_in -= 1
                if enlarge_in == 0:
                    enlarge_in = 2 ** num_bits
                    num_bits += 1
                del dict_to_create[w]
            else:
                char_code = dictionary[w]
                for _ in range(num_bits):
                    value = (value << 1) | (char_code & 1)
                    if position == bits - 1:
                        position = 0
                        result.append(char_func(value))
                        value = 0
                    else:
                        position += 1
                    char_code >>= 1
            enlarge_in -= 1
            if enlarge_in == 0:
                enlarge_in = 2 ** num_bits
                num_bits += 1
            dictionary[wc] = dict_size
            dict_size += 1
            w = c

    # flush remaining
    if w != "":
        if w in dict_to_create:
            if ord(w[0]) < 256:
                for _ in range(num_bits):
                    value = (value << 1)
                    if position == bits - 1:
                        position = 0
                        result.append(char_func(value))
                        value = 0
                    else:
                        position += 1
                char_code = ord(w[0])
                for _ in range(8):
                    value = (value << 1) | (char_code & 1)
                    if position == bits - 1:
                        position = 0
                        result.append(char_func(value))
                        value = 0
                    else:
                        position += 1
                    char_code >>= 1
            else:
                char_code = 1
                for _ in range(num_bits):
                    value = (value << 1) | char_code
                    if position == bits - 1:
                        position = 0
                        result.append(char_func(value))
                        value = 0
                    else:
                        position += 1
                    char_code = 0
                char_code = ord(w[0])
                for _ in range(16):
                    value = (value << 1) | (char_code & 1)
                    if position == bits - 1:
                        position = 0
                        result.append(char_func(value))
                        value = 0
                    else:
                        position += 1
                    char_code >>= 1
            enlarge_in -= 1
            if enlarge_in == 0:
                enlarge_in = 2 ** num_bits
                num_bits += 1
            del dict_to_create[w]
        else:
            char_code = dictionary[w]
            for _ in range(num_bits):
                value = (value << 1) | (char_code & 1)
                if position == bits - 1:
                    position = 0
                    result.append(char_func(value))
                    value = 0
                else:
                    position += 1
                char_code >>= 1
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 2 ** num_bits
            num_bits += 1

    # end-of-stream marker (2)
    char_code = 2
    for _ in range(num_bits):
        value = (value << 1) | (char_code & 1)
        if position == bits - 1:
            position = 0
            result.append(char_func(value))
            value = 0
        else:
            position += 1
        char_code >>= 1

    # pad to complete
    while True:
        value = (value << 1)
        if position == bits - 1:
            result.append(char_func(value))
            break
        position += 1

    return "".join(result)


# ═══════════════════════════════════════════════════════════════
# 自定义 Base64 编码
# ═══════════════════════════════════════════════════════════════

def custom_encode(data: Optional[str], url_safe: bool) -> str:
    """LZW 压缩 + 自定义 Base64 编码。

    Args:
        data: 原始字符串
        url_safe: True → 不加尾部 = 填充；False → 标准 Base64 填充

    Returns:
        编码后的字符串
    """
    if data is None:
        return ""

    base64_chars = CUSTOM_BASE64_CHARS
    compressed = lzw_compress(data, 6, lambda index: base64_chars[index])

    if not url_safe:
        mod = len(compressed) % 4
        if mod == 1:
            return compressed + "==="
        if mod == 2:
            return compressed + "=="
        if mod == 3:
            return compressed + "="
        return compressed

    return compressed


def custom_decode(data: Optional[str]) -> str:
    if not data:
        return ""
    if data.startswith("1-"):
        data = data[2:]

    alphabet = CUSTOM_BASE64_CHARS
    values = [alphabet.index(ch) for ch in data if ch in alphabet]
    if not values:
        return ""

    value = values[0]
    position = 32
    index = 1

    def read_bits(num_bits: int) -> int:
        nonlocal value, position, index
        bits = 0
        maxpower = 1 << num_bits
        power = 1
        while power != maxpower:
            if value & position:
                bits |= power
            position >>= 1
            if position == 0:
                position = 32
                value = values[index] if index < len(values) else 0
                index += 1
            power <<= 1
        return bits

    dictionary: Dict[int, Union[int, str]] = {0: 0, 1: 1, 2: 2}
    enlarge_in = 4
    dict_size = 4
    num_bits = 3

    first = read_bits(2)
    if first == 0:
        current = chr(read_bits(8))
    elif first == 1:
        current = chr(read_bits(16))
    elif first == 2:
        return ""
    else:
        return ""

    dictionary[3] = current
    result = [current]
    previous = current

    while True:
        code = read_bits(num_bits)
        if code == 0:
            dictionary[dict_size] = chr(read_bits(8))
            code = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif code == 1:
            dictionary[dict_size] = chr(read_bits(16))
            code = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif code == 2:
            return "".join(result)

        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1

        if code in dictionary:
            entry = str(dictionary[code])
        elif code == dict_size:
            entry = previous + previous[0]
        else:
            return "".join(result)

        result.append(entry)
        dictionary[dict_size] = previous + entry[0]
        dict_size += 1
        enlarge_in -= 1
        previous = entry

        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1


# ═══════════════════════════════════════════════════════════════
# 数据解析 / 处理
# ═══════════════════════════════════════════════════════════════

def random_hash() -> int:
    """生成随机 uint32 hash。"""
    return random.randint(0, 0xFFFFFFFF)


def generate_device_id() -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(20))


def parse_real_data(real_data: str) -> List[str]:
    """按 ^ 拆分指纹数据。"""
    return real_data.split("^")


def process_fields(fields: List[str]) -> List[Union[str, int]]:
    """处理指纹字段：随机化 hash 位置，更新时间戳。

    Args:
        fields: 原始字段列表

    Returns:
        处理后的字段列表
    """
    processed: List[Union[str, int]] = list(fields)
    current_timestamp = int(time.time() * 1000)

    for idx, typ in HASH_FIELDS.items():
        if idx >= len(processed):
            continue
        if typ == "split":
            val = str(processed[idx])
            parts = val.split("|")
            if len(parts) == 2:
                processed[idx] = f"{parts[0]}|{random_hash()}"
        elif typ == "full":
            if idx == 36:
                processed[idx] = random.randint(10, 100)
            else:
                processed[idx] = random_hash()

    # field 33: 当前时间戳
    if 33 < len(processed):
        processed[33] = current_timestamp

    return processed


# ═══════════════════════════════════════════════════════════════
# Cookie 生成
# ═══════════════════════════════════════════════════════════════

def generate_cookies(
    real_data: Optional[str] = None,
    ssxmod_itna2_data: Optional[str] = None,
    fingerprint_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """生成盼之 WAF cookie（ssxmod_itna / ssxmod_itna2）。

    使用内置浏览器指纹模板生成随机化指纹数据，经 LZW + 自定义 Base64 编码。

    Args:
        real_data: 浏览器指纹原始字符串（^ 分隔）。传 None 则使用内置模板。
        ssxmod_itna2_data: itna2 原始数据。传 None 则自动从 processed_fields 拼接。
        fingerprint_options: 可选的 g4f 指纹生成参数。

    Returns:
        {
            "ssxmod_itna":  str,   # cookie 值
            "ssxmod_itna2": str,   # cookie 值
            "timestamp":    int,   # 当前毫秒时间戳
        }
    """
    if fingerprint_options is None:
        fingerprint_options = {}

    now = int(time.time() * 1000)
    if real_data:
        fingerprint = real_data
    else:
        if _generate_fingerprint is None:
            raise RuntimeError("real_data is required when optional g4f fingerprint module is not installed")
        fingerprint = _generate_fingerprint(fingerprint_options)
    fields = parse_real_data(fingerprint)
    processed_fields = process_fields(fields)

    ssxmod_itna = "1-" + custom_encode(fingerprint, True)

    if ssxmod_itna2_data is None:
        ssxmod_itna2_data = "^".join(map(str, [
            processed_fields[0],
            processed_fields[1],
            processed_fields[23] if len(processed_fields) > 23 else "P",
            0, "", 0, "", "", 0,
            0, 0,
            processed_fields[32] if len(processed_fields) > 32 else 11,
            processed_fields[33] if len(processed_fields) > 33 else now,
            0, 0, 0, 0, 0,
        ]))
    ssxmod_itna2 = "1-" + custom_encode(ssxmod_itna2_data, True)

    return {
        "ssxmod_itna": ssxmod_itna,
        "ssxmod_itna2": ssxmod_itna2,
        "timestamp": int(processed_fields[33]) if len(processed_fields) > 33 else now,
    }


def get_fingerprint_data(now: Optional[int] = None) -> str:
    """获取浏览器指纹原始数据（用于 generate_cookies 的 real_data）。

    Args:
        now: 毫秒时间戳，传 None 则自动取当前时间

    Returns:
        ^ 分隔的指纹字符串
    """
    if now is None:
        now = int(time.time() * 1000)
    return WAF_FINGERPRINT_TEMPLATE.format(now=now)


def get_event_data(now: Optional[int] = None) -> str:
    """获取鼠标事件原始数据（用于 generate_cookies 的 ssxmod_itna2_data）。

    Args:
        now: 毫秒时间戳，传 None 则自动取当前时间

    Returns:
        ^ 分隔的事件字符串
    """
    if now is None:
        now = int(time.time() * 1000)
    return WAF_EVENT_TEMPLATE.format(now=now)


def generate_batch(
    count: int = 10,
    real_data: Optional[str] = None,
    fingerprint_options: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    return [
        generate_cookies(real_data=real_data, fingerprint_options=fingerprint_options or {})
        for _ in range(count)
    ]


__all__ = [
    "generate_cookies",
    "generate_batch",
    "get_fingerprint_data",
    "get_event_data",
    "lzw_compress",
    "custom_encode",
    "custom_decode",
    "generate_device_id",
    "CUSTOM_BASE64_CHARS",
]


# ssxmod_manager.py / ssxmod_manager_async.py

_current_cookies: Dict[str, Any] = {
    "ssxmod_itna": "",
    "ssxmod_itna2": "",
    "timestamp": 0,
}

REFRESH_INTERVAL_SECONDS = 15 * 60

_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None
_stop_event = asyncio.Event()


async def refresh_cookies():
    """Refresh SSXMOD cookies (async wrapper)."""
    global _current_cookies
    try:
        result = await asyncio.to_thread(generate_cookies)
        async with _lock:
            _current_cookies = {
                "ssxmod_itna": result["ssxmod_itna"],
                "ssxmod_itna2": result["ssxmod_itna2"],
                "timestamp": result["timestamp"],
            }
        print(_current_cookies)
        print("SSXMOD Cookie 已刷新", "SSXMOD")
    except Exception as e:
        print("SSXMOD Cookie 刷新失败", "SSXMOD", "", str(e))
    return _current_cookies


async def _refresh_loop() -> None:
    """Background refresh loop."""
    try:
        await refresh_cookies()
        while not _stop_event.is_set():
            try:
                await asyncio.wait_for(_stop_event.wait(), timeout=REFRESH_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                await refresh_cookies()
    finally:
        _stop_event.clear()


def init_ssxmod_manager() -> None:
    """Start the background refresh loop."""
    global _task
    if _task is not None and not _task.done():
        return
    _stop_event.clear()
    _task = asyncio.create_task(_refresh_loop())
    print(
        f"SSXMOD 管理器已启动，刷新间隔 {REFRESH_INTERVAL_SECONDS / 60:.0f} 分钟",
        "SSXMOD",
    )


async def stop_refresh() -> None:
    """Stop the background refresh loop."""
    global _task
    if _task is None:
        return
    _stop_event.set()
    try:
        await _task
    finally:
        _task = None
        print("SSXMOD 定时刷新已停止", "SSXMOD")


async def get_ssxmod_itna() -> str:
    async with _lock:
        return str(_current_cookies.get("ssxmod_itna", ""))


async def get_ssxmod_itna2() -> str:
    async with _lock:
        return str(_current_cookies.get("ssxmod_itna2", ""))


async def get_cookies() -> Dict[str, Any]:
    async with _lock:
        return dict(_current_cookies)
