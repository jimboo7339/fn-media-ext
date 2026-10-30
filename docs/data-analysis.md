# 飞牛影视（trim.media）数据摸底与分析

> 分析对象：`tmp/` 下 3 个 SQLite 数据库快照
> 数据时点：2026-10-23 15:22（+08:00）
> 目标：判断"谁看过哪些剧 + 时间线"这类扩展功能的可行性、数据边界与坑

---

## 0. 结论速览（TL;DR）

1. **可以做。** 三份库覆盖了「媒体元数据 + 用户观看记录 + 登录/活动日志」三类信息，足以支撑一个"按用户展开的观看时间线"。
2. **核心表是 `item_user_play`**：414 条有效记录 / 7 个用户 / 31 部剧 / 6 部电影 / 38 个散片，时间跨度 **2025-06-30 → 2026-10-23**。
3. **它不是事件流，而是"状态覆盖式"记录**：每个 (用户, 条目) 只有一行，只保留**首次观看时间**、**最后进度时间**、**当前进度**、**是否已看**。中间重看、暂停、退出全部丢失。
4. **`watched=1` 的记录共 233 条，且 `ts` 全为 0**——它们是"标记已看"，不是"播完"。其中 **152 条集中在 2026-01-27 09:25:43–09:26:02**，是一次性批量导入/批量标记，做时间线时必须单独识别，否则会出现一根 152 条记录的"柱子"。
5. **两个补充数据源很有价值**：
   - `user_activity_log`：滚动保留约 **28 天**（2026-09-25 → 2026-10-23，8921 条），其中 `/play/record` 心跳**固定 15 秒一次**（1505 条），可反推"最近在看什么、看了多久"。
   - `user_token`：**1093 条**登录会话（2025-04-19 起），带设备、客户端、IP，可做"设备/登录时间线"。
6. **三个必须先解决的数据坑**：
   - **时间戳毫秒/秒混用**（同一张库内混着，见附录 B）；
   - **父子链断裂**（95 集没有季、33 集有季但季找不到剧），但可用 `tmdb_id`/`trim_id` 兜底复原；
   - **同一部剧跨库重复**（18 个 `trim_id` 对应多条 `TV`），必须按 `trim_id` 去重，否则统计翻倍。
7. **海报文件不在库里**，只有相对路径（`/2f/14/RXF...webp`），需要走飞牛图片接口、NAS 本地 meta 目录或 TMDB CDN（`item_archive` 存了 TMDB `poster_path`，覆盖观看列表中 27/34 个剧）。

---

## 1. 文件与表总览

| 文件 | 大小 | 表数 | 角色 |
|---|---:|---:|---|
| `trimmedia.db` | 27.6 MiB | 28 | **主库**：媒体条目、文件、流信息、用户、观看记录、媒体库 |
| `trimmedia_ext.db` | 10.6 MiB | 4 | **元数据缓存**：TMDB/豆瓣原始 JSON 归档（剧/季/集/影） |
| `trimactivity.db` | 2.2 MiB | 3 | **活动库**：登录 token、登录码、页面/接口访问日志 |

完整性：三库 `PRAGMA integrity_check` 均为 `ok`，`journal_mode=wal`（快照已合并，无残留 `-wal`）。

### 主要表（按重要度）

| 表 | 行数 | 说明 |
|---|---:|---|
| `item` | 7,193 | 媒体条目：库根/目录/剧/季/集/电影/直播频道/未刮削文件 |
| `item_media` | 6,033 | 物理文件（路径、大小、类型） |
| `media_stream` | 7,423 | 流信息（编码、分辨率、**时长**） |
| `item_ancestor` | 7,203 | 条目 → 所属媒体库（MediaDB） |
| `item_user_play` | **415** | **观看记录（核心）** |
| `item_user` | 35 | 用户 ↔ 媒体库可见性 |
| `user` | 11 | 用户（含 2 个系统/已删除账号） |
| `mediadb_config` | 9 | 9 个媒体库定义 |
| `item_tag` | 16,268 | 标签（Genre/Resolution/Decade 等） |
| `item_archive` | 4,589 | TMDB 原始 JSON（tv 88 / season 127 / episode 4,373 / movie 1） |
| `user_activity_log` | 8,921 | 接口访问日志（约 28 天滚动） |
| `user_token` | 1,093 | 登录会话（token、设备、IP） |

空表或基本无用：`item_user_favorite`（收藏 0 条）、`user_permission`（0）、`tag`（0）、`download_task`（0）、`media_delete`（0）。

---

## 2. 数据模型与关键关系

```
mediadb_config (媒体库, 9 个)
   └─ item(type=MediaDB)
        └─ item(type=Directory)            目录（未刮削的文件夹）
        └─ item(type=TV)                   剧
             └─ item(type=Season)          季
                  └─ item(type=Episode)    集
        └─ item(type=Movie)                电影（已刮削）
        └─ item(type=Video)                未刮削的视频文件
        └─ item(type=LiveChannel)          直播频道

item ──1:N── item_media (物理文件) ──1:N── media_stream (音视频流, 含 duration)

user ──1:N── item_user_play (观看记录) ──N:1── item
user ──1:N── user_token (登录会话) ──1:N── user_activity_log (接口访问)
```

`item_ancestor` 的语义比较弱：**只记录"条目属于哪个 MediaDB"，不记录父子层级**。剧集层级要用 `item.parent_guid` 自己向上爬。

---

## 3. 核心表字段语义

### 3.1 `item`（媒体条目）

| 字段 | 含义 / 注意点 |
|---|---|
| `guid` | 主键；未刮削目录/文件用 `fv_` 前缀 |
| `type` | `MediaDB` / `Directory` / `TV` / `Season` / `Episode` / `Movie` / `Video` / `LiveChannel` |
| `title` | 标题；`Video` 类型就是**文件名**（如 `1级第1讲 推理比较.mp4`） |
| `parent_guid` | 上级；**可能悬空**（指向已删除条目） |
| `trim_id` / `tmdb_id` | 刮削 ID；**剧/季/集/电影 100% 有值**（Episode 4683/4683、TV 96/96） |
| `season_number` / `episode_number` | 集号；孤儿集的 `episode_number` 可能为 -1 |
| `posters` / `backdrops` | **相对路径**，不是 URL，也不是本地绝对路径 |
| `path` / `dir` / `filename` | 未刮削条目才有路径；已刮削的 `Episode` 多为空（真实路径在 `item_media`） |
| `runtime` | **分钟**（不是秒！） |
| `status` | 语义混杂：刮削条目是 TMDB 状态（`Ended`/`Returning Series`…），其余是 `'1'` |
| `create_time` / `update_time` | **毫秒** |

### 3.2 `item_user_play`（观看记录，核心）

| 字段 | 含义 / 注意点 |
|---|---|
| `item_guid` | 被观看条目；**可能是剧、季、集、电影、散片**（不一定是集） |
| `user_guid` | 用户 |
| `ts` | 播放进度（**秒**）；`watched=1` 时恒为 0 |
| `watched` | 0=未看完（有进度）/ 1=已看/标记已看 |
| `create_time` | **首次**产生记录的时间（毫秒） |
| `update_time` | **最后**一次进度上报时间（毫秒） |
| `visible` | 1=在"最近观看"中可见；**16 条为 0**（被隐藏） |
| `resolution` | 清晰度：`4k` 147、`原画` 30、`720` 21、`1080` 13…，198 条为空 |
| `type` / `media_guid` / `video_guid` / `audio_guid` / `subtitle_guid` | 冗余/关联字段，与 `item` 一致 |

**唯一性**：`(item_guid, user_guid)` 唯一，无重复。
**更新语义**：每看一次就覆盖 `update_time` 与 `ts`，所以**同一集看 3 遍只会留下最后一遍**。
**孤儿**：1 条记录的 `item_guid` 已不存在。

### 3.3 `item_media` / `media_stream`

- `item_media.type`：1=视频（5,832）、3=字幕（201）；一个条目可能对应多个文件。
- `media_stream.duration`：**秒**；但只有 **1,281 条视频流（1,280 个条目）** 有值，其余 4,552 条 `duration=0`、`codec_name=''`（strm/网盘挂载未探测）。
- 观看进度百分比只能对**有 duration 的 111/182 条在播记录**计算，其余需用 `item.runtime` 或 TMDB 单集时长兜底。
- 媒体总大小约 **1.16 TB**。

### 3.4 `user` / `user_token` / `user_activity_log`

- `user`：11 行，`is_admin=1` 仅 `admin`；`default-user-template`、`test007_deleted_*` 是系统/已删账号。
- `user.last_login_time`：**秒**（同一张表里 `create_time` 却是毫秒）。
- `user_token`：每次登录/设备授权一条，`create_time == update_time`，字段 `app_name`/`device`/`ip` 可用。
  客户端分布：Android 663、Web 218、空 187、AfuseKt 17、TV-Code 8、Cinetry 1。
  IP：局域网 381、公网 556、IPv6 156。
- `user_activity_log`：`path` 是路由模板（`/v/api/v1/play/record`、`/v/api/v1/item/list`…），**不含条目 ID**；`create_time` 秒级。
  共 37 个不同路由。时间范围 **2026-09-25 20:38 → 2026-10-23 15:11**，疑似按条数/天数滚动清理，**不能当长期历史**。

### 3.5 `item_archive`（TMDB 原始 JSON）

- 结构：`{cleanData, movie, episode, tv, season, person}`，其中 `tv` 含 `name / poster_path / backdrop_path / genres / vote_average / number_of_seasons / overview`。
- `episode` 含 `name / overview / runtime / air_date / still_path`。
- 可用来补：**题材（genres）**、简介、单集时长、TMDB 海报路径。
- 注意 `item_archive` 的 `item_type` 是小写（`tv`/`season`/`episode`/`movie`），与 `item.type` 大小写不同。

---

## 4. 数据画像

### 4.1 媒体库（9 个）

| 库 | 类型 | 条目数 | 说明 |
|---|---|---:|---|
| 媒体库1 | TV | 2,462 | 主力剧集库 |
| 幼小教育 | Others | 1,015 | 958 个未刮削视频 |
| 媒体库2 | TV | 1,001 | |
| 媒体库3 | TV | 792 | |
| 媒体库4 | TV | 665 | |
| adult | Others | 268 | 172 个散片，**含成人内容** |
| 电影 | Movie | 97 | 仅 9 部刮削成 `Movie`，其余是 `Directory`/`Video` |
| 媒体库5 | TV | 79 | |
| 媒体库6 | TV | 68 | |

> 另有 **158 条观看记录** 指向 `/vol02/.../影视资源/...`，其 MediaDB 已不存在（`item_ancestor` 悬空），属于"幽灵库"，做筛选时要容错。

### 4.2 用户

| 用户 | 角色 | 观看记录 | 剧 | 影 | 散片 | 已看 | 在播 | 理论时长* |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `user1` | 普通 | 192 | 24 | 3 | 1 | 70 | 122 | ~101 h |
| `admin` | 管理员 | 170 | 12 | 2 | 4 | 156 | 14 | ~107 h |
| `adult` | 普通 | 34 | 0 | 0 | 34 | 6 | 28 | ~4 h |
| `user2` | 普通 | 10 | 2 | 0 | 0 | 0 | 10 | ~5 h |
| `user3` | 普通 | 5 | 1 | 0 | 0 | 1 | 4 | ~3 h |
| `user4` | 普通 | 2 | 0 | 0 | 2 | 0 | 2 | ~0 h |
| `test01` | 普通 | 1 | 0 | 1 | 0 | 0 | 1 | ~0 h |
| `tv01` / `iptv` / `test007_deleted` | — | 0 | — | — | — | — | — | — |

\* 按"已看集数 × 单集时长"估算，**批量标记会让该值虚高**（admin 的 156 条里有 152 条是批量标记），只能做"片库覆盖量"参考，不能当真实观看时长。

### 4.3 观看记录（`item_user_play`）

- 总 415 行，有效 414 行；`watched=1` 233 行 / `watched=0` 182 行。
- 条目类型：Episode 357、Video 39、Movie 6、Season 5、TV 4、Directory 2、LiveChannel 1。
- **日期跨度**：2025-06-30 → 2026-10-23；**有活动的天数 76 天**。
- 分布极不均匀：2026-01-27 一天 152 条（批量），2026-08-26 36 条（批量），2026-06-24 21 条，其余日均个位数。
- 时段分布（按最后观看时间）：09 点 153、12 点 48、08 点 40、16 点 37、22 点 14——**9 点峰值完全由批量标记造成**。
- 热度榜（按集数）：剧E 62、剧F 36、剧G 32、剧H 29、剧C 28、剧D 27、剧I 25、剧J 24、剧B 23、剧K 18、剧L 15、剧A 13。
- **多人共看**：`剧K` 3 人；另有 9 部剧 2 人看过（剧D、剧C、剧E、剧H、剧L、剧M、剧I、剧B、剧A）。
- 真实播放进度（`watched=0` 共 182 条，其中 6 条 `ts=0`）：111 条能算出百分比，**中位数 83%**——大多数是"看到接近结尾但未标记完成"。

按月的记录分布（按 `update_time` 归月；可直观看出 2026-01 的批量污染）：

| 月份 | 记录 | 用户 | 其中集 | 已看 |
|---|---:|---:|---:|---:|
| 2025-06 | 1 | 1 | 1 | 0 |
| 2025-12 | 1 | 1 | 1 | 1 |
| 2026-01 | **154** | 2 | 147 | **153** |
| 2026-02 | 5 | 2 | 1 | 2 |
| 2026-03 | 6 | 2 | 4 | 0 |
| 2026-04 | 35 | 2 | 33 | 30 |
| 2026-05 | 4 | 2 | 1 | 0 |
| 2026-06 | 24 | 2 | 0 | 4 |
| 2026-07 | 36 | 4 | 29 | 4 |
| 2026-08 | 75 | 3 | 74 | 36 |
| 2026-09 | 33 | 4 | 26 | 0 |
| 2026-10 | 40 | 3 | 40 | 3 |

> 2026-01 的 154 条里 152 条是 `admin` 的批量标记；剔除后"真实活跃"集中在 2026-04、07、08、09、10。

### 4.4 登录 / 活动

- **登录**：1,093 次，2025-04-19 → 2026-10-23。`user1` 601 次、`admin` 399 次、`adult` 67 次。
- **近 28 天真实播放**（`/play/record` 心跳 × 15s）：
  - `user1` 1,127 次心跳 ≈ **4.7 小时**，22 个播放片段；
  - `user2` 378 次 ≈ **1.6 小时**，5 个片段。
  - 其余用户在该窗口内没有播放心跳（`admin` 有 2,218 条请求，但全是浏览/管理接口）。
- 心跳片段可精确到分钟，例如 `user2` 2026-10-15 19:52–20:26 连续看了 36 分钟。

---

## 5. "谁看过哪些剧 + 时间线" 可行性

### 5.1 能回答

- 某个用户看过哪些剧/电影，各自第几集，首看/最近看时间；
- 某一天/某一周谁在追什么（按天聚合）；
- 某部剧的"分集观看者矩阵"：哪一集谁看了、进度多少、是否看完；
- 多人对比：同一部剧被谁看过、谁先看；
- 追剧节奏：单集看完耗时（`update_time - create_time`）、连续追更天数、断更多久；
- 近 28 天真实播放时长与活跃时段（来自心跳）；
- 设备/登录时间线（来自 `user_token`）。

### 5.2 时间线怎么构造（推荐口径）

围绕 `item_user_play` 派生 **3 类事件**，而不是直接把 415 行铺开：

| 事件 | 时间 | 过滤条件 |
|---|---|---|
| `started` 开始观看 | `create_time` | 全部；同一批次只留 1 条 |
| `progress` 最近进度 | `update_time` | `watched=0` 且 `ts>0` |
| `finished` 看完/标记 | `update_time` | `watched=1` |
| `bulk_marked` 批量标记 | 批次起始时间 | 同用户 60s 内 ≥5 条，聚合成 1 条卡片："批量标记了 X 集" |

> 单条 `create_time == update_time` 且 `ts=0` 时，只生成一个 `finished`/`bulk_marked` 事件，避免重复。

**排重与归并**：以 `trim_id`（剧）为维度聚合，`Episode` 向上爬到 `TV`；爬不到时用 `tmdb_id` 找唯一 `TV`；再不行才回退到季/文件夹名。

### 5.3 真实样例（直接来自数据）

`剧A` 的分集观看记录（两人、同一天错峰看）：

| 用户 | 时间 | 集 | 进度 | 状态 |
|---|---|---|---:|---|
| user1 | 10-22 12:44 | S1E4 | 3004s | 在播 |
| user1 | 10-22 13:16 | S1E5 | 2946s | 在播 |
| user1 | 10-23 12:02 | S1E1 | 2745s | 在播 |
| user1 | 10-23 12:11 | S1E6 | 2947s | 在播 |
| user1 | 10-23 12:19 | S1E7 | 616s | 在播 |
| user1 | 10-23 12:46 | S1E2 | 2838s | 在播 |
| user1 | 10-23 13:04 | S1E3 | 1499s | 在播 |
| user1 | 10-23 13:18 | S1E8 | 0s | 已看 |
| user3 | 10-23 14:12 | S1E1 | 802s | 在播 |
| user3 | 10-23 14:20 | S1E11 | 1451s | 在播 |
| user3 | 10-23 14:40 | S1E12 | 2762s | 在播 |
| user3 | 10-23 14:43 | S1E13 | 0s | 已看 |
| user3 | 10-23 15:22 | S1E14 | 2705s | 在播 |

这类数据做"剧集 × 用户 × 时间"的泳道图效果会很好，也能体现"跳集"。

---

## 6. 数据质量与坑（按影响排序）

1. **覆盖式状态，不是事件流**：一集看多次只留最后一次；无法复原"二刷"、暂停点序列。
2. **`watched=1` 与 `ts=0` 绑定**：无法区分"真的看完"和"手动标记"。233 条里大部分是标记。
3. **两次批量导入**：
   - `admin` 152 条：2026-01-27 09:25:43–09:26:02（38 秒内）；
   - `user1` 36 条：2026-08-26 08:46:05–08:46:20（15 秒内，全部是《剧F》）。
   建议：前端默认折叠为一条"批量标记"事件，并提供"显示明细"开关。
4. **父子链断裂**：
   - 95 集没有 `parent_guid`；
   - 2 个季找不到所属剧（33 集受影响）；
   - 但 100% 有 `tmdb_id`/`trim_id`，**用 TMDB 维表兜底可 100% 复原剧名**（实测 414/414 可解析）。
5. **同剧跨库重复**：18 个 `trim_id` 对应多条 `TV`。例如 `剧C` 有 3 条 `TV`，分别挂 16/40/11 集，**观看记录也分散在其中 2 条**（17 + 11 = 28 集）；按 `TV.guid` 分组会把一部剧拆成两部。**所有统计必须按 `trim_id` 归并。**
6. **时长缺失**：182 条在播记录中只有 111 条能算百分比；strm/云盘文件没探测到时长。回退顺序建议：`media_stream.duration` → `item.runtime × 60` → `item_archive.episode.runtime × 60` → 默认 45 分钟。
7. **散片/未刮削条目**：`Video` 类型标题就是文件名（`1.ts`、`IMG_350.MOV`、含成人内容的长标题）。需要按 `dir` 归组、做文件名清洗或默认折叠。
8. **活动日志只有 28 天且无内容 ID**：不能补全历史；心跳只能算"时长/时段"，要知道"在看哪部"必须靠 `update_time` 与心跳结束时间做时间对齐（实测仅 49/1505 能对上，因为 `update_time` 只保留最后一次）。
9. **时间单位混乱**：见附录 B，同一个库里秒和毫秒混用；`item.runtime` 是分钟。
10. **图片缺失**：DB 里只有相对路径；需要额外来源。
11. **隐私**：
    - `user.passwd` 有 1 条真实 argon2 哈希（其余是 `{{SECRET_xxx}}` 占位）；
    - `user_token.token` 是明文 token；
    - `user_activity_log.ip`、`user_token.ip/device` 属个人数据；
    - `adult` 库为成人内容。
    → Web 端**绝不应**暴露 `user`/`user_token` 表，成人库建议默认隐藏（配置开关）。
12. **`visible=0`**：16 条记录在飞牛里是隐藏的，时间线默认应排除。
13. **幽灵媒体库**：158 条记录所属 MediaDB 已删除（`/vol02/...`），库筛选要容错。

---

## 7. 能力边界

| 能做 | 不能做（仅凭这三份库） |
|---|---|
| 谁看过哪些剧/集、首看/最近看时间 | 每次播放的完整会话与暂停点 |
| 分集观看矩阵、追剧节奏、断更 | 真实观看时长（28 天窗口以外） |
| 多人共看与先后顺序 | 页面内点击/浏览行为（活动日志无内容 ID） |
| 近 28 天真实播放时长与时段 | 收藏（表为空）、评分、评论 |
| 登录设备/IP 时间线 | 已删除媒体/用户的观看记录（级联删除） |
| 题材/年代/清晰度统计（标签+TMDB） | 精确的"看完时刻"（标记 vs 播放不可分） |

### 数据可信度分级

| 级别 | 指标 | 说明 |
|---|---|---|
| 🟢 高 | 看过哪些剧/集、首看/最近时间、用户归属 | 直接来自时间戳，可靠 |
| 🟢 高 | 登录/设备/IP、活跃时段 | `user_token`、`user_activity_log` 原始记录 |
| 🟡 中 | 进度百分比、在播/已看 | 182 条在播记录中仅 111 条有真实片长，其余靠 `runtime` 近似 |
| 🟡 中 | 题材分布 | 依赖 TMDB 归档，覆盖 27/34 |
| 🔴 低 | "真实观看时长"、"追剧速度" | 被批量标记和覆盖式记录污染，只能做相对参考 |
| 🔴 低 | "某个时刻在看哪部"（28 天窗口） | 心跳与 `update_time` 仅 49/1505 能对齐 |

---

## 8. 初步技术方案建议（供讨论）

### 8.1 数据层

不改原库，**只读 ATTACH 三库**，在导入阶段生成一张宽表/视图：

```sql
-- 伪代码：watch_event
SELECT
  p.user_guid,
  i.guid                AS item_guid,
  i.type                AS item_type,
  COALESCE(tv.trim_id, i.trim_id)      AS show_key,
  COALESCE(tv.title, i.title)          AS show_title,
  i.season_number, i.episode_number, i.title AS episode_title,
  p.create_time, p.update_time, p.ts, p.watched, p.visible, p.resolution,
  d.duration            AS duration_sec,
  CASE WHEN p.watched=1 THEN 'finished'
       WHEN p.ts=0        THEN 'started'
       ELSE 'progress' END AS event_kind
FROM item_user_play p
JOIN item i ON i.guid = p.item_guid
LEFT JOIN item s  ON s.guid = i.parent_guid
LEFT JOIN item tv ON tv.guid = s.parent_guid AND tv.type='TV'
LEFT JOIN (SELECT item_guid, MIN(duration) duration
           FROM item_media m JOIN media_stream ms ON ms.media_guid=m.guid
           WHERE ms.codec_type='video' AND ms.duration>0 GROUP BY item_guid) d
       ON d.item_guid = i.guid
WHERE p.visible = 1
```

剧名兜底（父链断裂时）：

```sql
SELECT title FROM item
WHERE type='TV' AND tmdb_id = :tmdb_id
ORDER BY create_time LIMIT 1;
```

### 8.2 服务/前端

| 方案 | 优点 | 缺点 |
|---|---|---|
| **A. FastAPI + SQLite + React/Vite**（推荐） | 后端简单、SQL 直接复用、前端时间线组件成熟 | 需要构建产物 |
| B. Next.js 全栈 | 一体化、SSR | 依赖重，SQLite 驱动在 Windows 上稍麻烦 |
| C. 构建期导出 JSON + 纯静态站点 | 部署最简（丢到 NAS 静态目录即可） | 数据更新需重新生成；无法做增量查询 |

### 8.3 页面草图

1. **总览**：用户切换、时间范围、近 7/30 天播放时长、在看列表。
2. **时间线**（主视图）：按天分组；每天若干卡片（海报、剧名、SxEy、集标题、进度条、首次/最近时间）；批量标记折叠；支持"人 × 剧"泳道视图。
3. **剧集详情**：该剧每集的观看者/进度/时间矩阵，可横向对比用户。
4. **用户画像**：题材分布、活跃时段热力图、连续追剧天数、设备分布。
5. **登录/设备**：可选，仅管理员可见。

### 8.4 刷新与部署

- 当前 `tmp/*.db` 是**一次性快照**，没有同步机制；需要确认：手动拷贝 / 定时 rsync / 走飞牛 API。
- 若部署在 NAS 上，可直接读原库（只读打开）+ 直接读 meta 目录海报；若部署在 Windows，则需拷贝快照 + 走 TMDB 图片。

---

## 9. 需要你确认的问题

1. **部署位置**：Windows 本机 / 飞牛 NAS（Docker）/ 其它？这决定海报与数据同步方案。
2. **数据更新**：只读这三份快照，还是要定时从 NAS 同步？用什么方式（rsync/共享目录/API）？
3. **访问控制**：仅自己看，还是多人访问？要不要登录？成人库（`adult`）是否默认隐藏？
4. **时间线形态**：单人纵向时间流 / 多人泳道对比 / 剧集甘特图，优先做哪个？
5. **海报**：允许访问 TMDB CDN 吗？还是必须离线/走 NAS 代理？
6. **只读边界**：确认不写回飞牛库（不做"标记已看"之类的写操作）？

---

## 附录 A：可复现的关键 SQL

```sql
-- 1) 用户观看概览
SELECT u.username, COUNT(*) n, SUM(p.watched) finished,
       MIN(datetime(p.create_time/1000,'unixepoch','+8 hours')) first,
       MAX(datetime(p.update_time/1000,'unixepoch','+8 hours')) last
FROM item_user_play p JOIN user u ON u.guid=p.user_guid
GROUP BY p.user_guid ORDER BY n DESC;

-- 2) 每用户在看/已看剧集（剧名用父链兜底）
SELECT u.username, COALESCE(tv.title, s.title, i.title) show_title,
       COUNT(*) eps, SUM(p.watched) watched_eps
FROM item_user_play p
JOIN item i ON i.guid=p.item_guid
JOIN user u ON u.guid=p.user_guid
LEFT JOIN item s  ON s.guid=i.parent_guid
LEFT JOIN item tv ON tv.guid=s.parent_guid AND tv.type='TV'
WHERE i.type='Episode'
GROUP BY p.user_guid, show_title ORDER BY eps DESC;

-- 3) 批量标记识别（60 秒窗口）
SELECT user_guid, create_time/60000 AS minute_bucket, COUNT(*) n
FROM item_user_play
GROUP BY user_guid, minute_bucket HAVING n >= 5;

-- 4) 近 28 天播放心跳
SELECT u.username, COUNT(*)*15/60.0 AS minutes
FROM user_activity_log l JOIN user u ON u.guid=l.user_guid
WHERE l.path='/v/api/v1/play/record' GROUP BY l.user_guid;

-- 5) 跨库重复的剧
SELECT trim_id, COUNT(*) c FROM item WHERE type='TV'
GROUP BY trim_id HAVING c > 1;
```

## 附录 B：时间字段单位对照

| 表.字段 | 单位 |
|---|---|
| `item.create_time/update_time` | 毫秒 |
| `item.runtime` | **分钟** |
| `item_user_play.create_time/update_time` | 毫秒 |
| `item_user_play.ts` | **秒** |
| `item_media.*`、`media_stream.duration` | 毫秒 / **秒** |
| `item_archive.create_time/update_time` | 毫秒 |
| `item_ancestor.*`、`mediadb_config.*` | 毫秒 |
| `field_lock.*`、`person.*`、`sys_metadata.*` | **秒** |
| `user.last_login_time` | **秒** |
| `user.create_time/update_time` | 毫秒 |
| `user_token.*`、`login_code.*`、`user_activity_log.*` | **秒** |
| `user_data.*` | **秒** |

> 建议在导入层统一转成 UTC 毫秒并显式标注时区（数据默认按 `Asia/Shanghai` 展示）。

## 附录 C：数据时点与规模

- 最新观看记录：2026-10-23 15:22（`user3` / 剧A S1E14）
- 最新登录：2026-10-23 15:22
- 媒体库总大小：约 1.16 TB / 6,033 个文件 / 7,193 个条目
- 有效观看记录：414 条 / 7 个用户 / 31 部剧 / 6 部电影 / 38 个散片
