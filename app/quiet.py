"""把 uvicorn 的「非 HTTP 请求」噪音日志收成一条聚合信息。

背景：有些客户端（NAS 上的网关、代理、监控组件）会往这个端口发非 HTTP 字节，
uvicorn 的解析器拒绝后每次都会打一条 WARNING。经抓包确认：

- 这些连接只是被拒掉，不进应用、不影响功能；
- 频率可达每分钟 20 次以上，把日志刷满，真正的告警反而被淹没。

这里做的是「外科手术式」处理：只吞掉这一条特定消息，其它告警照旧；
同时按周期汇总成一条 INFO，并把对端地址带上——这样既不刷屏，也不瞎。

如果连这条汇总也不想看见，设 `QUIET_SUMMARY=0`：只静默计数、不打任何日志，
日志里彻底不再出现相关内容，计数仍然留在 `/health` 里可查。
"""
from __future__ import annotations

import logging
import os
import sys
import time

BAD_HTTP_MSG = "Invalid HTTP request received."

ENABLED = os.getenv("QUIET_BAD_HTTP", "1") not in ("0", "false", "no")
# 汇总开关：设 0 就只计数、一条也不打（计数仍在 /health 里）
SUMMARY = os.getenv("QUIET_SUMMARY", "1") not in ("0", "false", "no")
# 汇总周期（秒）；<=0 一律视为「不汇总」，免得有人误设 0 反而变成每条都打
REPORT_SECONDS = float(os.getenv("QUIET_REPORT_SECONDS", "300"))

_STATE = {"since_report": 0, "total": 0, "last_report": time.time(), "last_peer": ""}


def stats() -> dict:
    return {
        "bad_requests": _STATE["total"],
        "bad_requests_since_report": _STATE["since_report"],
        "last_bad_peer": _STATE["last_peer"],
        "quiet_summary": SUMMARY,
    }


def _peer_from_stack() -> str:
    """uvicorn 的日志里没有对端地址，但它是在协议对象的栈帧里打的，去那儿取。"""
    try:
        frame = sys._getframe(2)
    except ValueError:
        return ""
    for _ in range(32):
        if frame is None:
            break
        obj = frame.f_locals.get("self")
        transport = getattr(obj, "transport", None)
        if transport is not None:
            try:
                peer = transport.get_extra_info("peername")
            except Exception:  # noqa: BLE001
                peer = None
            if peer:
                if isinstance(peer, (tuple, list)) and len(peer) >= 2:
                    return f"{peer[0]}:{peer[1]}"
                return str(peer)
        frame = frame.f_back
    return ""


class BadHTTPFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.getMessage() != BAD_HTTP_MSG:
                return True
        except Exception:  # noqa: BLE001
            return True

        _STATE["since_report"] += 1
        _STATE["total"] += 1
        peer = _peer_from_stack()
        if peer:
            _STATE["last_peer"] = peer

        now = time.time()
        elapsed = now - _STATE["last_report"]
        if SUMMARY and REPORT_SECONDS > 0 and elapsed >= REPORT_SECONDS:
            logging.getLogger("uvicorn.error").info(
                "已忽略 %d 次非 HTTP 请求（约 %d 分钟内），最近来自 %s。"
                "这说明有本机或网关组件在探测这个端口，请求已被丢弃，不影响本应用。",
                _STATE["since_report"],
                max(1, round(elapsed / 60)),
                _STATE["last_peer"] or "未知",
            )
            _STATE["since_report"] = 0
            _STATE["last_report"] = now

        return False  # 丢掉这条 WARNING


def install() -> bool:
    if not ENABLED:
        return False
    flt = BadHTTPFilter()
    for name in ("uvicorn.error", "uvicorn"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, BadHTTPFilter) for f in logger.filters):
            logger.addFilter(flt)
    return True