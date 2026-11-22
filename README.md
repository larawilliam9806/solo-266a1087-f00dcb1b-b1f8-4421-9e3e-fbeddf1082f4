# 公交 GTFS 时刻表发布系统

FastAPI + SQLite + 本地文件的公交时刻表发布与查询网页。数据工程师一次性上传同一版本的
5 个 CSV（stops / routes / trips / stop_times / calendar），系统整版校验通过后才发布；
公众按「线路 + 运营日」查询有序停站时刻，也可为电子站牌下载可离线还原的发车包。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据库保存在命名卷 `gtfs-data` 中，容器重启数据不丢。

### 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

### 运行测试

```bash
pip install pytest httpx
pytest tests/ -q
```

## 网页功能

- **时刻查询**：选择运营日和线路，展示当日各班次的停站序列与到发时刻；
  跨午夜时刻显示为 `24:20:00` 并标注「+1天」。
- **站牌发车包**：见下节。选一个已发布的运行图、一个站点和连续 1～31 个自然日，
  下载可离线使用的站牌 JSON；网页断网后仍可打开该 JSON 还原发车表。
- **发布新版本**：一次上传 5 个 CSV + 版本名称。通过则立即生效；
  失败则列出全部错误行（文件 / 行号 / 字段 / 原因），当前正式版不受影响。
- **版本记录**：查看所有版本（含失败版本）及其错误明细。

也可以用 `sample_data/valid/`（可通过）和 `sample_data/invalid/`（含典型错误）
两组样例数据在网页上直接试。

## 电子站牌发车包

站牌常处于断网环境，因此发车表以**一次性下载的 JSON 包**分发，而不是每次联网查询。

**生成**：在网页「站牌发车包」页签（或调用 API）选择

- 一个**已发布**的运行图（不必是当前正式版，历史版本也可用）；
- 一个站点；
- 起点自然日（北京时间）与连续 1～31 天。

包内规则：

- 只纳入该站点在所选运行图中的**非末站停靠**发车（同一班次 `stop_sequence`
  最大的停靠是末站，末站不发车）。
- GTFS 时刻按北京时间解释：运营日 D 的 `24:20` 是 D 次日凌晨 00:20。
  记录按**实际发车日**归入所选自然日，同时保留原运营日。
- 同一钟点的多班车**分别列出**，每条记录都含线路、班次、停站序号、发车钟点
  与原运营日（`service_date`），可逐条追溯。

**压缩与最少记录**：每个星期几最多一套发车模板，每个自然日只记录相对模板的
增删。对某个星期几的 m 个自然日、某条候选发车在 p 个日子出现：

- 放进模板：1 条模板记录 + `m - p` 条删除；
- 不放进模板：p 条新增。

逐条取较小值即为最优，因此 `模板 + 新增 + 删除` 的记录数**最少**。
包内 `stats` 给出 `total_records` 与下界 `minimum_records`，两者相等时
`is_minimal` 为 `true`。

**离线还原**：包是自包含的（含来源版号、线路与班次元数据），网页加载后
即使断网也可打开，按自然日还原发车表并显示来源版号。已发布新版本后，
旧包仍按包内数据还原，不受当前正式版影响。

**包有错时**：逐条列出 `path` 与原因（例如字段缺失、日期落在错误的星期几、
模板引用不存在、统计不一致等），无法还原；**已经成功打开的包仍保留**，
可继续切换查看。

### 包格式（schema_version = 1）

```jsonc
{
  "schema": "gtfs-stop-board",
  "schema_version": 1,
  "timezone": "Asia/Shanghai",
  "source_version": {"id": 3, "label": "2026冬季运行图", "published_at": "..."},
  "stop": {"stop_id": "S2", "stop_name": "人民广场"},
  "range": {"start_date": "2026-11-23", "end_date": "2026-11-29", "days": 7},
  "weekdays": {
    "monday": {
      "weekday": "monday", "weekday_label": "周一",
      "template": [
        {"route_id": "R1", "trip_id": "T1", "stop_sequence": 2,
         "departure_clock": "08:12:00", "day_offset": 0}
      ],
      "days": [
        {"date": "2026-11-23", "add": [], "remove": []}
      ]
    }
    // ... tuesday ... sunday
  },
  "trips": {
    "T1": {"route_id": "R1", "route_short_name": "1路",
           "route_long_name": "中央车站-机场", "route_type": 3,
           "headsign": "机场方向", "direction_id": 0}
  },
  "stats": {"days": 7, "departures": 14, "template_records": 2,
            "add_records": 0, "remove_records": 0, "total_records": 2,
            "minimum_records": 2, "is_minimal": true}
}
```

- `departure_clock` 是 00:00:00～23:59:59 的钟点；
- `day_offset` 是实际发车日相对原运营日的天数（跨午夜为 1）；
  还原时原运营日 = 自然日 − `day_offset`；
- 每个自然日的最终发车 = 模板 + `add` − `remove`；
- 包内记录的关键字为 `(route_id, trip_id, stop_sequence, departure_clock,
  day_offset)`，因此同一钟点的不同班次不会互相覆盖。

## 校验规则

任一规则不满足，整版拒绝发布：

1. **结构**：必备列齐全；字段类型合法（非负整数序号、YYYYMMDD 日期、0/1 星期标志、
   HH:MM:SS 时刻）。
2. **表内唯一**：`stop_id` / `route_id` / `service_id` / `trip_id` 各自唯一；
   `stop_times` 内 `(trip_id, stop_sequence)` 唯一。
3. **跨表引用**：`trips.route_id → routes`、`trips.service_id → calendar`、
   `stop_times.trip_id → trips`、`stop_times.stop_id → stops`；
   每个 trip 至少有一条 stop_times。
4. **时刻递增**：同一 trip 按 `stop_sequence` 排序后，每行 `departure ≥ arrival`，
   且下一站到站时刻严格大于上一站发车时刻。
5. **跨午夜**：时刻允许 `HH ≥ 24`（如 `25:10:00`），内部统一换算为
  「自运营日 00:00 起的秒数」比较与存储；`24:10` 之后出现 `23:59` 视为倒退报错。

## 发布与并发语义

- **整版原子发布**：所有校验在内存中完成；通过后在**单个 SQLite 事务**内写入
  整版数据并拨动 `meta.current_version_id` 指针。查询方要么看到旧完整版本、
  要么看到新完整版本，绝不出现半新半旧。
- **失败保旧**：校验失败的版本只记录 `versions(status='failed')` 和错误明细，
  不写入任何时刻数据、不触碰指针。
- **版本不可变**：已发布版本的数据永不修改或删除；请求开始时读一次指针，
  之后该请求内所有查询都固定读取同一版本，换版期间查询结果始终完整一致。
- SQLite 开启 **WAL**，发布写入不阻塞并发查询。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/versions` | multipart 上传 `label` + 5 个 CSV；成功 201，失败 422 并返回错误行 |
| GET | `/api/versions` | 版本列表（含当前正式版指针） |
| GET | `/api/versions/current` | 当前正式版 |
| GET | `/api/versions/{id}` | 版本详情与错误明细 |
| GET | `/api/routes` | 当前版本的线路列表 |
| GET | `/api/routes/{route_id}/schedule?date=YYYY-MM-DD` | 该运营日的班次与有序停站时刻 |
| GET | `/api/board/versions` | 可生成站牌包的已发布版本（含当前正式版指针） |
| GET | `/api/board/stops?version_id=` | 该版本中可作为发车源的站点及发车条数 |
| GET | `/api/board/package?version_id=&stop_id=&start_date=&days=` | 下载站牌 JSON（附件） |
| POST | `/api/board/validate` | 校验站牌包，逐条返回问题 |
| POST | `/api/board/expand` | 用与网页相同的逻辑还原站牌包 |

### 命令行示例

```bash
# 发布
curl -F "label=2026冬季运行图" \
     -F "stops=@sample_data/valid/stops.csv" \
     -F "routes=@sample_data/valid/routes.csv" \
     -F "trips=@sample_data/valid/trips.csv" \
     -F "stop_times=@sample_data/valid/stop_times.csv" \
     -F "calendar=@sample_data/valid/calendar.csv" \
     http://localhost:8000/api/versions

# 查询夜班线路（含 24:00 之后的跨午夜班次）
curl "http://localhost:8000/api/routes/R2/schedule?date=2026-11-23"

# 下载某站连续 7 天的站牌包
curl -OJ "http://localhost:8000/api/board/package?version_id=1&stop_id=S2&start_date=2026-11-23&days=7"

# 校验并还原一个本地站牌包（服务端与网页逻辑一致）
curl -H 'Content-Type: application/json' --data @board.json http://localhost:8000/api/board/validate
curl -H 'Content-Type: application/json' --data @board.json http://localhost:8000/api/board/expand
```

## 目录结构

```
app/
  main.py        FastAPI 端点（发布 / 查询 / 版本 / 站牌包）
  gtfs.py        CSV 解析与整版校验（纯标准库）
  db.py          SQLite schema、原子发布、版本指针
  board.py       站牌包生成、校验、离线还原（纯标准库）
  static/index.html  网页（查询 / 站牌包 / 发布 / 版本记录）
tests/           pytest 单元与集成测试
sample_data/     可通过与含错误的样例 CSV
Dockerfile / docker-compose.yml
```
