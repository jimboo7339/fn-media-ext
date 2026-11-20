"""``app.digest`` 加签 / 鉴权参数的回归测试。

关注的是「钉钉加签」「飞书签名」「Server 酱只填 SendKey」这三处，
不依赖网络：只验 URL / body 的构造是否和官方算法一致。

运行：``.venv/Scripts/python.exe tests/test_digest_sign.py``
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import digest  # noqa: E402


def test_dingtalk_url_without_secret_is_unchanged():
    url = "https://oapi.dingtalk.com/robot/send?access_token=abc"
    assert digest._dingtalk_url(url, "") == url


def test_dingtalk_url_appends_timestamp_and_sign():
    url = "https://oapi.dingtalk.com/robot/send?access_token=abc"
    out = digest._dingtalk_url(url, "SECxyz")
    assert out.startswith(url + "&timestamp=")
    assert "&sign=" in out
    # 用同一时间戳重算一次，确认 sign 的算法与官方一致
    query = urllib.parse.parse_qs(urllib.parse.urlparse(out).query)
    ts = query["timestamp"][0]
    string_to_sign = f"{ts}\nSECxyz"
    expected = urllib.parse.quote_plus(base64.b64encode(
        hmac.new(b"SECxyz", string_to_sign.encode(), hashlib.sha256).digest()))
    assert query["sign"][0] == urllib.parse.unquote_plus(expected)


def test_feishu_sign_matches_official_example():
    # 飞书官方文档示例：timestamp=1599360473, secret=demo ->
    # l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8=
    got = digest._feishu_sign("demo", "1599360473")
    assert got == "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8="


def test_feishu_sign_uses_key_with_newline():
    sign = digest._feishu_sign("SEC", "1700000000")
    key = b"1700000000\nSEC"
    expected = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()
    assert sign == expected


def test_serverchan_url_from_key_only():
    out = digest._serverchan_url("", "SCT123")
    assert out == "https://sctapi.ftqq.com/SCT123.send"


def test_serverchan_url_leaves_explicit_url_alone():
    url = "https://sctapi.ftqq.com/abc.send"
    assert digest._serverchan_url(url, "unused") == url


def test_serverchan_url_fills_placeholder():
    out = digest._serverchan_url("https://sctapi.ftqq.com/{key}.send", "K")
    assert out == "https://sctapi.ftqq.com/K.send"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for fn in fns:
        try:
            fn()
            print("  ok  ", fn.__name__)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print("  FAIL", fn.__name__, "->", exc)
    print(f"{ok}/{len(fns)} passed")
