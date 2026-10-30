# 排查手册：日志一直滚 `Invalid HTTP request received`

## 先说结论：本应用已经默认处理掉这条噪音

从 v 本次更新起，应用会**把这条特定告警吞掉，改成每 5 分钟汇总一条**，并把来源地址带上，例如：

```
INFO:  已忽略 126 次非 HTTP 请求（约 5 分钟内），最近来自 192.168.1.10:54628。这说明有本机或网关组件在探测这个端口，请求已被丢弃，不影响本应用。
```

计数同时暴露在 `/health` 的 `bad_requests` / `last_bad_peer` 字段里。想看原始告警就设 `QUIET_BAD_HTTP=0`。

**通常到这里就不用往下看了。** 下面几节是「想知道到底是谁在探」时用的。

## 这是什么

有人在往 8787 端口发**非 HTTP 的字节**，uvicorn 的 HTTP 解析器认不出来，直接关掉连接并打了这行警告。

两个要点：

- 请求**根本没进到应用**（连路由都没到），所以这不是本程序的 bug。合法的 HTTP 请求不会产生它——包括 Docker 自己的 `HEALTHCHECK`、浏览器的正常页面访问、以及实时在线页面每 10 秒一次的 `/partials/live` 轮询。
- 想抓到元凶，**必须在容器运行时抓，而且要找对地方**。见第 2 步的说明。

## 第 0 步：先判断值不值得查（10 秒）

```bash
docker logs --since 10s fn-media-ext 2>&1 | grep -c "Invalid HTTP"
```

- **0～2** → 零星扫描，忽略即可，跳到最后一节静音
- **一直在涨** → 继续

## 第 1 步：读时间戳（常常直接定性）

```bash
docker logs -t --tail=300 fn-media-ext | grep "Invalid HTTP" | tail -30
```

从这批时间戳里看三件事：

| 看什么 | 说明 |
|---|---|
| **间隔是否固定** | 固定到 ±0.05 秒 → 机器行为（探针/监控）；忽快忽慢 → 扫描 |
| **是否成对出现**（同一秒内两条，相差几十毫秒） | 同一个客户端**并发**开了两条连接，典型是 IPv4+IPv6 并行（Happy Eyeballs）或两项检查并发 |
| **间隔是否在变化** | 例如 6.19s 收紧到 5.16s → 带退避的自动重试，不是固定 cron |

一个真实例子（30 条 / 88 秒，约每分钟 21 条）：

```
组间间隔(秒)   6.20 6.22 6.17 6.19 6.21 6.16 6.18 6.17 │ 5.18 5.15 5.17 6.19 5.16 6.18 5.18
组内间隔(毫秒)  21 11 14 29 10 20 14 15 12 17 11 18 20 14
```

结论：成对 + 间隔稳定 → 一个自动化客户端在**反复并发尝试两条连接、失败后退避重试**。人不会这么规律，随机扫描不会成对。

## 第 2 步：抓包（必须容器运行时，且要抓容器 IP）

> **不要用「停容器 + 自己占端口」的办法。** 如果探针探的是**容器自己的 IP**（172.x）而不是宿主端口，容器一停那个 IP 就消失了，你什么都抓不到——这是本手册上一版踩过的坑。

### 2a. 先看宿主机有没有 tcpdump（3 秒，有就最省事）

```bash
which tcpdump
```

有的话直接用，连容器都不用管：

```bash
tcpdump -i any -nn -e 'tcp port 8787 and tcp[tcpflags] & tcp-syn != 0'
```

### 2b. 没有 tcpdump，也不想下载镜像

**Docker 容器默认带 `NET_RAW` 权限**，容器里的 Python 用标准库就能抓包；而且容器内部正是最理想的观测点——不管探测打到宿主端口还是直连容器 IP，流量最后一定经过容器的 `eth0`。把下面整段贴进 NAS 的命令行：

```bash
docker exec -i fn-media-ext python - <<'PYEOF'
import select, socket, sys, time

PORT = 8787

def classify(p):
    if not p: return ""
    if p[:3] in (b"\x16\x03\x01", b"\x16\x03\x00"): return "TLS 握手 —— 有人在用 https:// 连这个明文端口"
    if p.startswith(b"PRI * HTTP/2.0"): return "HTTP/2 明文探测"
    if p[:4] in (b"GET ", b"POST", b"HEAD", b"OPTI"): return "合法 HTTP 请求（不是元凶）"
    return "非 HTTP 字节（未知）"

def parse(f):
    if len(f) < 14: return None
    et = int.from_bytes(f[12:14], "big"); o = 14
    if et == 0x8100 and len(f) >= 18:
        et = int.from_bytes(f[16:18], "big"); o = 18
    if et == 0x0800:
        if len(f) < o + 20: return None
        h = (f[o] & 15) * 4; proto = f[o + 9]
        src = ".".join(map(str, f[o+12:o+16])); dst = ".".join(map(str, f[o+16:o+20]))
    elif et == 0x86DD:
        if len(f) < o + 40: return None
        h = 40; proto = f[o + 6]
        def a(x): return ":".join("%x" % int.from_bytes(f[x+i*2:x+i*2+2], "big") for i in range(8))
        src = a(o + 8); dst = a(o + 24)
    else:
        return None
    if proto != 6: return None
    t = o + h
    if len(f) < t + 20: return None
    sp = int.from_bytes(f[t:t+2], "big"); dp = int.from_bytes(f[t+2:t+4], "big")
    return src, dst, sp, dp, f[t+13], f[t + (f[t+12] >> 4) * 4:]

socks = []
for _, n in socket.if_nameindex():
    try:
        sk = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
        sk.bind((n, 0)); socks.append((n, sk))
    except OSError as e:
        print("跳过", n, e)
if not socks:
    print("打不开原始套接字（需要 root + NET_RAW）"); sys.exit(1)
print("监听 8787 中 ... Ctrl+C 结束\n")
while True:
    for sk in select.select([x for _, x in socks], [], [], 1)[0]:
        n = next(n for n, x in socks if x is sk)
        r = parse(sk.recv(65535))
        if not r or r[3] != PORT: continue
        src, dst, sp, dp, fl, pay = r
        print(f"{time.strftime('%H:%M:%S')} {n:5} {src}:{sp} -> {dst}:{dp} flags={fl:#04x} {pay[:20].hex(' ')}  {classify(pay)}", flush=True)
PYEOF
```

它会把每个连到 8787 的连接打出来，并**直接告诉你属于哪一类**，例如：

```
11:40:12 enp1s0 172.17.0.1:51422 -> 172.17.0.2:8787 flags=0x18 16 03 01 00 05 ...  TLS 握手 —— 有人在用 https:// 连这个明文端口
```

完整版（多一个 `--selftest` 自检）在仓库里：`scripts/sniff8787.py`。

### 2c. 实在想用 tcpdump 镜像

`nicolaka/netshoot` 有一百多 MB，国内拉取通常很慢，非必要不建议：

```bash
docker run --rm --net=host nicolaka/netshoot tcpdump -i any -nn -e 'tcp port 8787 and tcp[tcpflags] & tcp-syn != 0'
```

### 看输出里的网卡名和源地址

| 网卡 + 源地址 | 含义 |
|---|---|
| `lo` + `127.0.0.1` | NAS 本机某个进程连的宿主端口 |
| 源 `172.17.0.1`（docker 网关） | **宿主在探容器**（Docker 管理界面 / 网关的健康探测） |
| 源 `192.168.x.x` | 局域网里另一台设备 |
| 源是陌生公网 IP | 公网扫描 |

## 第 3 步：对号入座

| 前 16 字节长这样 | 是什么 | 怎么办 |
|---|---|---|
| `16 03 01` | **TLS 握手**，有人在用 `https://` 连这个明文端口 | 见 A |
| `50 52 49 20 2a 20 48 54 54 50` | HTTP/2 明文探测（`PRI * HTTP`） | 见 B |
| `47 45 54 20`（`GET `）+ 正常路径 | **合法请求，不是元凶**，继续看下一行 | —— |
| 乱码二进制 + 陌生公网 IP | 公网扫描 | 见 C |

> 合法请求的参照：`47 45 54 20 2f 70 61 72 74 69 61 6c 73 2f 6c 69 76 65` 就是 `GET /partials/live`，是本应用实时在线页面的轮询。

### A. 被当成 HTTPS 访问

按可能性依次检查：

1. **某个网关/反代的健康检查配成了 HTTPS**，或者它在用 TLS 探测上游协议。对号入座看第 2 步是不是 `docker0` / `127.0.0.1`。
2. **浏览器 HSTS**：HSTS 按主机生效、**不区分端口**。飞牛管理界面跑在 HTTPS 上时，浏览器会把 `http://nas:8787` 也强制升级。
   - 换一个从没打开过飞牛界面的浏览器试 `http://<NAS-IP>:8787`；能打开就是它。
   - 解决：清掉该主机的 HSTS（Chrome：`chrome://net-internals/#hsts` → Delete domain）。
3. **飞牛「应用入口 / 反代」**的后端协议填成了 `https://`，改成 `http://`。
4. 书签存的是 `https://` → 改成 `http://`。

### B. 探活工具

找有没有 Uptime Kuma、Docker 端口检测、路由器插件在监控 8787：

- 地址从 `https://` 改成 `http://`；
- 探测路径改成 `/health`（专门给健康检查用）。

### C. 公网扫描

来源是陌生公网 IP → 8787 被暴露到公网了：

- 路由器上关掉 8787 的端口转发；
- 要外网访问，让飞牛自带的反代终结 HTTPS，再以 `http://` 转发到容器；
- **别把 8787 直接暴露公网**：这个应用只有一个管理员密码，没有登录限速。

## 附：源地址的含义

- 抓到的连接来自 `127.0.0.1` → NAS 本机上某个进程连的 `127.0.0.1:8787`（局域网其它机器会显示成 `192.168.x.x`）。最常见是配了反代/应用入口，转发时源地址变成回环。
- 抓不到任何东西、只有容器在跑时才有警告 → 探针多半直连**容器 IP**，不走宿主端口。

## 快速对照实验

把打开本应用的所有标签页全部关掉（手机、平板、电脑都算），再看警告还滚不滚：

- **停了** → 问题在「浏览器 → 反代 → 容器」这条链路上；
- **照旧滚** → 与浏览器无关，是后台探针或扫描。

## 结案记录（2026-10-24）

本 NAS 上抓到的元凶：**`dl302`**（PID 6973，宿主机进程，`/app/dl302`，98.9 MB 内存）。

- 行为：每 5~6 秒向 `127.0.0.1:8787` 与容器 IP:8787 各发一对连接，载荷是**封装的 IPv4 报文**（1500 字节左右，疑似隧道/直链探测流量），TCP 握手成功后被 uvicorn 以 400 拒绝；
- 资源：长期占用 ~25% CPU（对「每 6 秒发一个探测包」来说异常高），累计 28 分钟；
- 身份判断：`dl302` + `strmscrape`（strm 刮削）→ 飞牛生态的 **strm 直链/302 重定向工具**，配置里把 8787 当成了它的某个服务端口；
- 处置：已 kill。若它随容器重启复活，去它的配置里把 8787 改成正确的目标（如飞牛影视的 8005），或确认它到底想连什么。

> 写程序时的教训（与我们无关但值得记）：一个「探活」功能如果连失败都不退避，就会变成常驻 25% CPU 的空转——和本应用按需补同步的退避设计正好是反面教材。

## 最后一节：查清之后想静音

确认无害、只是嫌日志吵，在 `docker-compose.yml` 的 service 里加一行 `command` 覆盖（**不用重新构建镜像**）：

```yaml
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787",
              "--no-access-log", "--log-level", "error"]
```

然后 `docker compose up -d`。只保留 ERROR，WARNING 及以下（包括这条）都不再输出。

> 建议先查清来源再静音。持续探测本身在消耗 NAS 资源，而且如果哪天变成真正的连接问题，静音会让你看不见线索。