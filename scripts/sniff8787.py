#!/usr/bin/env python3
"""抓 8787 端口的连接，把「谁连的」和「发了什么」直接打出来。

不需要装任何东西：Docker 容器默认带 NET_RAW 权限，标准库就够。

用法（在 NAS 的项目目录里）：

    docker exec -i fn-media-ext python - < scripts/sniff8787.py

或者换个端口：

    docker exec -i fn-media-ext python - 8787 < scripts/sniff8787.py

自检（不抓包，只验证解析逻辑，可在任何机器上跑）：

    python scripts/sniff8787.py --selftest
"""
from __future__ import annotations

import select
import socket
import struct
import sys
import time

PORT = 8787
TLS = "TLS 握手（有人在用 https:// 连这个明文端口）"
H2 = "HTTP/2 明文探测"
HTTP_OK = "合法 HTTP 请求（不是元凶）"


def classify(payload: bytes) -> str:
    if not payload:
        return ""
    if payload[:3] == b"\x16\x03\x01" or payload[:3] == b"\x16\x03\x00":
        return TLS
    if payload.startswith(b"PRI * HTTP/2.0"):
        return H2
    if payload[:4] in (b"GET ", b"POST", b"HEAD", b"OPTI"):
        return HTTP_OK
    return "非 HTTP 字节（未知）"


def ipv4(frame: bytes, off: int):
    ihl = (frame[off] & 0x0F) * 4
    proto = frame[off + 9]
    src = ".".join(str(b) for b in frame[off + 12 : off + 16])
    dst = ".".join(str(b) for b in frame[off + 16 : off + 20])
    return src, dst, proto, ihl


def ipv6(frame: bytes, off: int):
    proto = frame[off + 6]
    def addr(at: int) -> str:
        return ":".join("%x" % int.from_bytes(frame[at + i * 2 : at + i * 2 + 2], "big") for i in range(8))

    return addr(off + 8), addr(off + 24), proto, 40


def parse(frame: bytes):
    """以太网帧 -> (网卡无关的) src, dst, sport, dport, flags, payload。"""
    if len(frame) < 14:
        return None
    etype = int.from_bytes(frame[12:14], "big")
    off = 14
    if etype == 0x8100 and len(frame) >= 18:          # VLAN
        etype = int.from_bytes(frame[16:18], "big")
        off = 18
    if etype == 0x0800:
        if len(frame) < off + 20:
            return None
        src, dst, proto, ihl = ipv4(frame, off)
        hdr = ihl
    elif etype == 0x86DD:
        if len(frame) < off + 40:
            return None
        src, dst, proto, hdr = ipv6(frame, off)
    else:
        return None
    if proto != 6:                                     # 只看 TCP
        return None
    t = off + hdr
    if len(frame) < t + 20:
        return None
    sport = int.from_bytes(frame[t : t + 2], "big")
    dport = int.from_bytes(frame[t + 2 : t + 4], "big")
    flags = frame[t + 13]
    data_off = (frame[t + 12] >> 4) * 4
    return src, dst, sport, dport, flags, frame[t + data_off :]


def flag_names(f: int) -> str:
    out = []
    for bit, name in ((0x02, "SYN"), (0x10, "ACK"), (0x08, "PSH"), (0x01, "FIN"), (0x04, "RST")):
        if f & bit:
            out.append(name)
    return ",".join(out) or "-"


def selftest() -> int:
    def eth(payload: bytes, etype: int) -> bytes:
        return b"\x00" * 12 + struct.pack("!H", etype) + payload

    def ipv4_tcp(src: str, dst: str, sport: int, dport: int, flags: int, data: bytes) -> bytes:
        tcp = struct.pack("!HHIIBBHHH", sport, dport, 0, 0, 5 << 4, flags, 8192, 0, 0) + data
        total = 20 + len(tcp)
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, total, 1, 0, 64, 6, 0,
                         socket.inet_aton(src), socket.inet_aton(dst))
        return eth(ip + tcp, 0x0800)

    cases = [
        ("合法 GET", ipv4_tcp("172.17.0.1", "172.17.0.2", 40000, 8787, 0x18, b"GET /partials/live HTTP/1.1\r\n")),
        ("TLS 握手", ipv4_tcp("172.17.0.1", "172.17.0.2", 40001, 8787, 0x18, bytes.fromhex("16030100050100000100"))),
        ("h2c 前言", ipv4_tcp("172.17.0.1", "172.17.0.2", 40002, 8787, 0x18, b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")),
        ("纯 SYN", ipv4_tcp("172.17.0.1", "172.17.0.2", 40003, 8787, 0x02, b"")),
        ("别的端口（应忽略）", ipv4_tcp("172.17.0.1", "172.17.0.2", 40004, 80, 0x18, b"GET / HTTP/1.1\r\n")),
        ("乱码", ipv4_tcp("172.17.0.1", "172.17.0.2", 40005, 8787, 0x18, bytes(range(20)))),
    ]
    ok = True
    for name, frame in cases:
        r = parse(frame)
        if r is None:
            print(f"  {name:16} -> 解析为 None")
            ok &= name.startswith("别的端口")
            continue
        src, dst, sp, dp, fl, pay = r
        if dp != PORT:
            print(f"  {name:16} -> 忽略（目标端口 {dp}）")
            continue
        print(f"  {name:16} -> {src}:{sp} -> {dst}:{dp} [{flag_names(fl)}] {pay[:16].hex(' ')} | {classify(pay)}")
    print("\n自检:", "通过" if ok else "失败")
    return 0 if ok else 1


def main() -> int:
    global PORT
    args = [a for a in sys.argv[1:]]
    if "--selftest" in args:
        return selftest()
    for a in args:
        if a.isdigit():
            PORT = int(a)

    socks = []
    print("可用网卡:", ", ".join(n for _, n in socket.if_nameindex()) or "无")
    for _, name in socket.if_nameindex():
        try:
            s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
            s.bind((name, 0))
            socks.append((name, s))
        except OSError as exc:
            print(f"  跳过 {name}: {exc}")
    if not socks:
        print("无法打开原始套接字（需要 root 与 NET_RAW 权限）")
        return 1

    print(f"\n监听中：只显示目标端口 {PORT} 的连接。Ctrl+C 结束。\n")
    last = time.time()
    hits = 0
    while True:
        ready, _, _ = select.select([s for _, s in socks], [], [], 1.0)
        for s in ready:
            name = next(n for n, sock in socks if sock is s)
            try:
                frame = s.recv(65535)
            except OSError:
                continue
            r = parse(frame)
            if not r:
                continue
            src, dst, sport, dport, flags, payload = r
            if dport != PORT:
                continue
            hits += 1
            stamp = time.strftime("%H:%M:%S")
            head = f"{stamp} {name:6} {src}:{sport} -> {dst}:{dport} [{flag_names(flags)}]"
            if payload:
                tail = f"{payload[:24].hex(' '):<72} | {classify(payload)}"
            else:
                tail = "(无载荷)"
            print(f"{head} {tail}", flush=True)
        if time.time() - last > 20:
            last = time.time()
            print(f"  ... 已捕获 {hits} 个连接，仍在监听", flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已停止")