"""Locate poster files inside the mounted Trim meta directory."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import config

_LOCK = threading.Lock()
_INDEX: dict[str, Path] = {}
_BUILT_AT = 0.0
SUFFIXES = {".webp", ".jpg", ".jpeg", ".png", ".avif"}


def build_index(force: bool = False) -> int:
    """Walk the image root once and map basename -> path (sharded dirs vary)."""
    global _BUILT_AT
    root = config.IMG_ROOT
    if not root.exists():
        return 0
    if not force and _INDEX and time.time() - _BUILT_AT < 3600:
        return len(_INDEX)
    if not _LOCK.acquire(blocking=False):
        return len(_INDEX)
    try:
        found: dict[str, Path] = {}
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if Path(name).suffix.lower() in SUFFIXES:
                    found.setdefault(name, Path(dirpath) / name)
        _INDEX.clear()
        _INDEX.update(found)
        _BUILT_AT = time.time()
        return len(_INDEX)
    finally:
        _LOCK.release()


def _safe(p: Path) -> Path | None:
    """解析符号链接后必须仍位于图片根目录内，防止索引或直接路径越界。"""
    try:
        real = p.resolve()
        if real.is_relative_to(config.IMG_ROOT.resolve()):
            return real
    except OSError:  # noqa: BLE001 - resolve 失败就当不存在
        pass
    return None


def resolve(rel: str) -> Path | None:
    rel = (rel or "").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    root = config.IMG_ROOT
    direct = root / rel
    if direct.is_file():
        hit = _safe(direct)
        if hit:
            return hit
    name = Path(rel).name
    hit = _INDEX.get(name)
    if hit and hit.is_file():
        found = _safe(hit)
        if found:
            return found
    if not _INDEX:
        build_index()
        hit = _INDEX.get(name)
        if hit and hit.is_file():
            found = _safe(hit)
            if found:
                return found
    fallback = root / name
    if fallback.is_file():
        found = _safe(fallback)
        if found:
            return found
    return None


def stats(shows: list[dict]) -> dict:
    keys = [(s["show_key"], s["poster"]) for s in shows]
    seen: dict[str, bool] = {}
    for key, poster in keys:
        if key in seen:
            continue
        seen[key] = bool(poster and resolve(poster))
    return {
        "total": len(seen),
        "ok": sum(1 for v in seen.values() if v),
        "root": str(config.IMG_ROOT),
        "index": len(_INDEX),
        "exists": config.IMG_ROOT.exists(),
    }
