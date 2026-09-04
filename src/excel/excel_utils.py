#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xlsx 工具函数

提供：
  - check_xlsx_protection(xlsx_path) -> dict  检测每个 sheet 是否被保护
  - unprotect_xlsx(xlsx_path, backup=True) -> dict  删除所有 sheetProtection 节点

支持：
  - 无密码保护（<sheetProtection selectLockedCells="1"/>）
  - 有密码保护（SHA-512 hash 的 <sheetProtection algorithmName="..." hashValue="..." .../>）

原理：xlsx = zip 包，每个 sheet 在 xl/worksheets/sheetN.xml。
保护节点 <sheetProtection ... />（自闭合）的 attribute 里可能含 base64 字符（/, +），
正则必须用 [^>]*? 匹配（不能用 [^/>]*，否则卡在 base64 的 / 上）。
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

# sheetProtection 节点：<sheetProtection ... />  （自闭合）
# attribute 里可能含 base64 字符（/, +），用 [^>]*? 不用 [^/>]*
_SHEET_PROTECTION_RE = re.compile(
    r'<sheetProtection\b[^>]*?/>',
    re.DOTALL,
)


def check_xlsx_protection(xlsx_path: str) -> Dict[str, dict]:
    """检测 xlsx 中每个 sheet 的保护状态

    Returns:
        {
            'has_protection': bool,        # 是否有任意 sheet 受保护
            'protected_sheets': [          # 受保护的 sheet 列表
                {
                    'sheet_file': 'xl/worksheets/sheet1.xml',
                    'has_password': bool,  # 是否有密码（SHA-512 hash）
                    'algorithm': str|None, # 算法名
                },
                ...
            ],
            'total_sheets': int,           # sheet 总数
        }
    """
    result = {
        'has_protection': False,
        'protected_sheets': [],
        'total_sheets': 0,
    }
    p = Path(xlsx_path).resolve()
    if not p.exists() or not p.is_file():
        return result

    try:
        with zipfile.ZipFile(p, 'r') as z:
            sheet_files = sorted(
                n for n in z.namelist()
                if n.startswith('xl/worksheets/') and n.endswith('.xml')
            )
            result['total_sheets'] = len(sheet_files)
            for fname in sheet_files:
                try:
                    content = z.read(fname).decode('utf-8', errors='ignore')
                except Exception:
                    continue
                m = _SHEET_PROTECTION_RE.search(content)
                if not m:
                    continue
                node = m.group(0)
                has_pwd = 'algorithmName=' in node and 'hashValue=' in node
                algo_m = re.search(r'algorithmName="([^"]+)"', node)
                result['protected_sheets'].append({
                    'sheet_file': fname,
                    'has_password': has_pwd,
                    'algorithm': algo_m.group(1) if algo_m else None,
                    'node': node,
                })
                result['has_protection'] = True
    except zipfile.BadZipFile:
        logger.error(f"[excel_utils] {p.name} 不是有效的 xlsx (zip) 文件")
    except Exception as e:
        logger.error(f"[excel_utils] 检测保护时出错: {e}")

    return result


def unprotect_xlsx(xlsx_path: str, backup: bool = True) -> Dict[str, any]:
    """强制解除 xlsx 的所有工作表保护

    不管有没有密码，直接删除所有 <sheetProtection ... /> 节点。
    - 无密码的：直接删除
    - 有密码的：删除节点（hash 一起被删），效果等同于"清除保护密码"

    Args:
        xlsx_path: xlsx 文件路径
        backup: 是否先备份（备份名带时间戳，默认 *_原版备份_YYYYMMDD_HHMMSS.xlsx）

    Returns:
        {
            'success': bool,
            'removed': int,                # 删了几个 sheetProtection 节点
            'backup_path': str|None,
            'protected_sheets': [          # 修复前检测到的受保护 sheet
                ... (同 check_xlsx_protection)
            ],
            'error': str|None,             # 失败原因
        }
    """
    result = {
        'success': False,
        'removed': 0,
        'backup_path': None,
        'protected_sheets': [],
        'error': None,
    }
    p = Path(xlsx_path).resolve()
    if not p.exists() or not p.is_file():
        result['error'] = f"文件不存在: {p}"
        return result

    # 1. 先检测
    check = check_xlsx_protection(str(p))
    result['protected_sheets'] = check['protected_sheets']
    if not check['has_protection']:
        logger.info(f"[excel_utils] {p.name} 无保护，无需处理")
        result['success'] = True
        return result

    n_pwd = sum(1 for s in check['protected_sheets'] if s['has_password'])
    n_no_pwd = len(check['protected_sheets']) - n_pwd
    logger.warning(
        f"[excel_utils] {p.name} 检测到 {len(check['protected_sheets'])} 个 sheet 受保护"
        f"（其中 {n_pwd} 个有密码 / {n_no_pwd} 个无密码），开始强制解除..."
    )

    # 2. 备份
    if backup:
        backup_path = p.with_name(
            p.stem + f"_原版备份_{datetime.now():%Y%m%d_%H%M%S}" + p.suffix
        )
        try:
            shutil.copy2(p, backup_path)
            result['backup_path'] = str(backup_path)
            logger.info(f"[excel_utils] 备份: {backup_path.name}")
        except Exception as e:
            result['error'] = f"备份失败: {e}"
            return result

    # 3. 读取所有 zip 内容
    try:
        with zipfile.ZipFile(p, 'r') as zin:
            all_files = zin.infolist()
            contents = {info.filename: zin.read(info.filename) for info in all_files}
    except zipfile.BadZipFile:
        result['error'] = f"文件不是有效的 xlsx (zip)"
        return result
    except Exception as e:
        result['error'] = f"读取失败: {e}"
        return result

    # 4. 删保护节点
    removed = 0
    for fname in list(contents.keys()):
        if not (fname.startswith('xl/worksheets/') and fname.endswith('.xml')):
            continue
        try:
            text = contents[fname].decode('utf-8')
        except UnicodeDecodeError:
            text = contents[fname].decode('utf-8', errors='ignore')
        if 'sheetProtection' not in text:
            continue
        new_text, n = _SHEET_PROTECTION_RE.subn('', text)
        if n > 0:
            contents[fname] = new_text.encode('utf-8')
            removed += n

    if removed == 0:
        # 检查后说有但实际没删到——奇怪
        logger.warning(f"[excel_utils] {p.name} 检测说有保护但删除数为 0")
        result['success'] = True
        return result

    # 5. 重写 xlsx
    tmp_path = p.with_suffix(p.suffix + '.tmp')
    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zout:
            for info in all_files:
                zout.writestr(info, contents[info.filename])
        shutil.move(tmp_path, p)
    except Exception as e:
        result['error'] = f"写回失败: {e}"
        # 清理临时文件
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        return result

    result['success'] = True
    result['removed'] = removed
    logger.warning(
        f"[excel_utils] ✅ {p.name} 已强制解除保护（删 {removed} 个 sheetProtection 节点）"
        f"{'  备份: ' + Path(result['backup_path']).name if result['backup_path'] else ''}"
    )
    return result


def auto_unprotect_if_needed(xlsx_path: str) -> bool:
    """如果 xlsx 受保护，自动强制解除（带备份）

    Returns:
        True = 已就绪（无保护或已破解）
        False = 失败（文件不存在 / 读不出 / 写不回）

    用法：
        # 任何 calc 初始化之前调
        if not auto_unprotect_if_needed(excel_path):
            raise RuntimeError("Excel 文件无法处理")
    """
    if not xlsx_path or not os.path.exists(xlsx_path):
        logger.error(f"[excel_utils] auto_unprotect: 文件不存在 {xlsx_path}")
        return False
    res = unprotect_xlsx(xlsx_path, backup=False)
    if not res['success']:
        logger.error(f"[excel_utils] auto_unprotect 失败: {res.get('error')}")
        return False
    return True
