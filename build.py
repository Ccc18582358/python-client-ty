#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建后处理脚本

用法：
    1. pyinstaller price-monitor-system.spec
    2. python build.py        # 把 config/ 拷到 dist/，让 exe 直接读 <exe_dir>/config/

为什么需要：
- spec 里我们故意没有把 config/ 打进 exe（否则 exe 是只读的，用户改不了 active.json）
- exe 运行时通过 _app_dir() 拿到 exe 所在目录，再去 <exe_dir>/config/ 找 active.json
- 所以要把 config/ 整个目录拷到 dist/ 里
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC_CONFIG = ROOT / "config"
DIST = ROOT / "dist"
DIST_CONFIG = DIST / "config"


def main():
    if not SRC_CONFIG.exists():
        print(f"[build] 找不到源 {SRC_CONFIG}")
        return 1

    if not DIST.exists():
        print(f"[build] 找不到 dist 目录 {DIST}，请先跑 pyinstaller")
        return 1

    # 删旧的 dist/config/ 重建（避免旧文件残留）
    if DIST_CONFIG.exists():
        shutil.rmtree(DIST_CONFIG)

    # 拷贝
    shutil.copytree(SRC_CONFIG, DIST_CONFIG)
    print(f"[build] 已复制 {SRC_CONFIG} -> {DIST_CONFIG}")

    # 列出 dist/config 里的文件
    print("[build] dist/config/ 内容：")
    for p in sorted(DIST_CONFIG.rglob("*")):
        rel = p.relative_to(DIST_CONFIG)
        size = p.stat().st_size if p.is_file() else "<dir>"
        print(f"  {rel}  ({size} B)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
