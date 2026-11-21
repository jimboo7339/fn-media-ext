"""Runtime configuration, all overridable by environment variables."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent


def _path(env: str, default: str) -> Path:
    return Path(os.getenv(env, default)).expanduser()


DATA_DIR = _path("DATA_DIR", str(BASE_DIR / "data"))
DB_DIR = _path("TRIM_DB_DIR", "/data/trim")
IMG_ROOT = _path("TRIM_IMG_ROOT", "/data/meta/img")

SNAP_DIR = DATA_DIR / "snap"
APP_DB = DATA_DIR / "app.db"

SNAPSHOT_MINUTES = int(os.getenv("SNAPSHOT_MINUTES", "10"))

# 页面访问时按需补同步：数据超过 SYNC_STALE_SECONDS 就在后台补一次；
# 陈旧超过 SYNC_BLOCKING_SECONDS（冷启动）则等它同步完再出页面。
SYNC_ON_VIEW = os.getenv("SYNC_ON_VIEW", "1") not in ("0", "false", "no")
SYNC_STALE_SECONDS = int(os.getenv("SYNC_STALE_SECONDS", "60"))
SYNC_BLOCKING_SECONDS = int(os.getenv("SYNC_BLOCKING_SECONDS", "1800"))
# 冷启动那次最多等多久；防止挂载异常时把页面请求拖住
SYNC_BLOCKING_WAIT = float(os.getenv("SYNC_BLOCKING_WAIT", "5"))
try:
    TZ = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))
except Exception:  # noqa: BLE001 - Windows without tzdata
    from datetime import timedelta, timezone

    TZ = timezone(timedelta(hours=8))

# 仅供本地预览：跳过登录。生产环境不要设置。
PREVIEW_AUTOLOGIN = os.getenv("PREVIEW_AUTOLOGIN", "") == "1"

# 反向代理出口走 HTTPS 时设 COOKIE_SECURE=1，登录 cookie 将不再走明文连接
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "") in ("1", "true", "yes")

# 随 /health 暴露、随版本一起发；发版打 tag 时同步改这里
APP_VERSION = "1.6.1"

# ---- 阈值提醒（结果并进每日日报推送）----
# 单人单日观看时长上限（分钟），0 = 关闭。估算口径与 /stats 相同。
ALERT_DAILY_MINUTES = int(os.getenv("ALERT_DAILY_MINUTES", "0") or 0)
# 深夜观看提醒（22:30–06:00 还有播放记录），默认关闭
ALERT_NIGHT = os.getenv("ALERT_NIGHT", "") in ("1", "true", "yes")

SOURCE_DBS = ("trimmedia.db", "trimmedia_ext.db", "trimactivity.db")

# 已删除媒体库的条目在飞牛的 item_ancestor 里还留着指针，它们的播放记录没有归属。
# 在应用里把它当作一个伪媒体库参与筛选，方便用户整体折叠/展开。
DEAD_LIBRARY = "已删除的媒体库"
DAY_MS = 86_400_000
