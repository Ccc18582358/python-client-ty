"""database 包：SQLite 数据访问层

对外只暴露 `DBManagerV2`，所有 DB 操作都走它。
不直接 import sqlite3 出来——避免多模块各开连接（WAL 已配，多 reader + 1 writer）。
"""
