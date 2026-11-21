"""测试共用的隔离数据库：每个测试文件建自己的临时 app.db。

pytest 按文件名字母序收集与执行，config 又是模块级单例——谁最后改指
谁生效。所以每个文件的 client 工厂里都要先调 fresh_db()，保证文件之间
互不污染（真正的生产库 data/app.db 永远不碰）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app import config, db


def fresh_db(prefix: str = "fnxt-test-") -> Path:
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    config.DATA_DIR = tmp
    config.APP_DB = tmp / "app.db"
    config.SNAP_DIR = tmp / "snap"
    config.DB_DIR = tmp / "no-such-db"
    config.IMG_ROOT = tmp / "img"
    db.init()
    return tmp
