"""GTFS CSV 解析与整版校验。

纯标准库实现，不依赖 FastAPI，便于单元测试。

校验规则：
1. 结构：必备列齐全、字段类型合法（整数/日期/时刻/0-1 标志）。
2. 表内唯一：stops.stop_id、routes.route_id、calendar.service_id、
   trips.trip_id 唯一；stop_times 内 (trip_id, stop_sequence) 唯一。
3. 跨表引用：trips.route_id -> routes、trips.service_id -> calendar、
   stop_times.trip_id -> trips、stop_times.stop_id -> stops。
4. 时刻递增：同一 trip 按 stop_sequence 排序后，
   每行 departure >= arrival，且下一站到站时刻 > 上一站发车时刻（严格递增）。
5. 跨午夜：时刻允许 HH >= 24（如 25:10:00），统一换算为
   “自运营日 00:00 起的秒数”存储与比较。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date

MAX_ERRORS = 1000  # 单版本最多记录的错误条数，防止超大错误响应

TIME_RE = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")
DATE_RE = re.compile(r"^\d{8}$")

FILES = ("stops", "routes", "trips", "stop_times", "calendar")

REQUIRED_COLUMNS = {
    "stops": ["stop_id", "stop_name"],
    "routes": ["route_id"],
    "trips": ["route_id", "service_id", "trip_id"],
    "stop_times": ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
    "calendar": [
        "service_id", "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday", "start_date", "end_date",
    ],
}

WEEKDAY_COLUMNS = ["monday", "tuesday", "wednesday", "thursday",
                   "friday", "saturday", "sunday"]


@dataclass
class VError:
    file: str
    row: int | None      # CSV 文件行号（表头为第 1 行）
    field: str | None
    message: str

    def as_dict(self) -> dict:
        return {"file": self.file, "row": self.row,
                "field": self.field, "message": self.message}


def parse_gtfs_time(value: str) -> int | None:
    """解析 GTFS 时刻（允许 HH>=24）为自运营日 00:00 起的秒数。"""
    m = TIME_RE.match(value.strip())
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return h * 3600 + mi * 60 + s


def format_gtfs_time(secs: int) -> str:
    """秒数格式化为 GTFS 时刻字符串，小时可 >= 24。"""
    h, rem = divmod(secs, 3600)
    mi, s = divmod(rem, 60)
    return f"{h:02d}:{mi:02d}:{s:02d}"


def parse_service_date(value: str) -> date | None:
    """解析 YYYYMMDD 服务日期。"""
    value = value.strip()
    if not DATE_RE.match(value):
        return None
    try:
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None


class _ErrorBag:
    def __init__(self) -> None:
        self.errors: list[VError] = []
        self.truncated = False

    def add(self, file: str, row: int | None, field: str | None, message: str) -> None:
        if len(self.errors) >= MAX_ERRORS:
            self.truncated = True
            return
        self.errors.append(VError(file, row, field, message))

    def __bool__(self) -> bool:
        return bool(self.errors)


def _read_csv(name: str, content: bytes, bag: _ErrorBag) -> list[dict] | None:
    """读取 CSV 为 dict 行列表，每行附带 _row（文件行号，表头=1）。"""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        bag.add(name, None, None, "文件不是有效的 UTF-8 编码")
        return None
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        bag.add(name, None, None, "文件为空或缺少表头")
        return None
    headers = [h.strip() if h else "" for h in reader.fieldnames]
    missing = [c for c in REQUIRED_COLUMNS[name] if c not in headers]
    if missing:
        bag.add(name, 1, None, f"缺少必备列: {', '.join(missing)}")
        return None
    rows: list[dict] = []
    for lineno, raw in enumerate(reader, start=2):
        if None in raw:  # 列数多于表头
            bag.add(name, lineno, None, "行列数多于表头列数")
            continue
        row = {k.strip(): (v.strip() if v is not None else "")
               for k, v in raw.items() if k is not None}
        row["_row"] = lineno
        rows.append(row)
    if not rows:
        bag.add(name, None, None, "文件没有数据行")
        return None
    return rows


def _check_unique(rows: list[dict], name: str, key: str, bag: _ErrorBag) -> set[str]:
    seen: dict[str, int] = {}
    ids: set[str] = set()
    for r in rows:
        v = r[key]
        if not v:
            bag.add(name, r["_row"], key, f"{key} 不能为空")
            continue
        if v in seen:
            bag.add(name, r["_row"], key,
                    f"{key} '{v}' 重复（首次出现于第 {seen[v]} 行）")
            continue
        seen[v] = r["_row"]
        ids.add(v)
    return ids


def validate_version(files: dict[str, bytes]) -> tuple[dict[str, list[tuple]] | None,
                                                     list[dict], bool]:
    """校验一个完整版本的 5 个 CSV。

    返回 (tables, errors, truncated)：
    - tables: 校验通过时，各表待入库的行（tuple 列表）；失败时为 None。
    - errors: VError.as_dict() 列表。
    - truncated: 错误是否因达到上限被截断。
    """
    bag = _ErrorBag()
    raw: dict[str, list[dict] | None] = {}
    for name in FILES:
        content = files.get(name)
        if content is None:
            bag.add(name, None, None, "缺少上传文件")
            raw[name] = None
        else:
            raw[name] = _read_csv(name, content, bag)

    # ---------- stops ----------
    stop_ids: set[str] = set()
    stops_out: list[tuple] = []
    if raw["stops"]:
        stop_ids = _check_unique(raw["stops"], "stops", "stop_id", bag)
        for r in raw["stops"]:
            lat = lon = None
            if r.get("stop_lat"):
                try:
                    lat = float(r["stop_lat"])
                except ValueError:
                    bag.add("stops", r["_row"], "stop_lat", "stop_lat 不是数字")
            if r.get("stop_lon"):
                try:
                    lon = float(r["stop_lon"])
                except ValueError:
                    bag.add("stops", r["_row"], "stop_lon", "stop_lon 不是数字")
            if r["stop_id"] in stop_ids:
                stops_out.append((r["stop_id"], r.get("stop_name", ""), lat, lon))

    # ---------- routes ----------
    route_ids: set[str] = set()
    routes_out: list[tuple] = []
    if raw["routes"]:
        route_ids = _check_unique(raw["routes"], "routes", "route_id", bag)
        for r in raw["routes"]:
            rtype = r.get("route_type", "")
            rtype_val = None
            if rtype:
                try:
                    rtype_val = int(rtype)
                except ValueError:
                    bag.add("routes", r["_row"], "route_type", "route_type 不是整数")
            if r["route_id"] in route_ids:
                routes_out.append((r["route_id"], r.get("route_short_name", ""),
                                   r.get("route_long_name", ""), rtype_val))

    # ---------- calendar ----------
    service_ids: set[str] = set()
    calendar_out: list[tuple] = []
    if raw["calendar"]:
        service_ids = _check_unique(raw["calendar"], "calendar", "service_id", bag)
        for r in raw["calendar"]:
            flags: list[int] = []
            ok = True
            for col in WEEKDAY_COLUMNS:
                v = r[col]
                if v not in ("0", "1"):
                    bag.add("calendar", r["_row"], col, f"{col} 必须为 0 或 1")
                    ok = False
                else:
                    flags.append(int(v))
            start = parse_service_date(r["start_date"])
            end = parse_service_date(r["end_date"])
            if start is None:
                bag.add("calendar", r["_row"], "start_date",
                        f"start_date '{r['start_date']}' 不是合法的 YYYYMMDD 日期")
                ok = False
            if end is None:
                bag.add("calendar", r["_row"], "end_date",
                        f"end_date '{r['end_date']}' 不是合法的 YYYYMMDD 日期")
                ok = False
            if start and end and start > end:
                bag.add("calendar", r["_row"], "start_date", "start_date 晚于 end_date")
                ok = False
            if ok and r["service_id"] in service_ids:
                calendar_out.append((r["service_id"], *flags,
                                     start.isoformat(), end.isoformat()))

    # ---------- trips ----------
    trip_ids: set[str] = set()
    trips_out: list[tuple] = []
    trip_route: dict[str, str] = {}
    if raw["trips"]:
        trip_ids = _check_unique(raw["trips"], "trips", "trip_id", bag)
        for r in raw["trips"]:
            ok = True
            if r["route_id"] and r["route_id"] not in route_ids:
                bag.add("trips", r["_row"], "route_id",
                        f"route_id '{r['route_id']}' 在 routes 中不存在")
                ok = False
            if not r["route_id"]:
                bag.add("trips", r["_row"], "route_id", "route_id 不能为空")
                ok = False
            if r["service_id"] and r["service_id"] not in service_ids:
                bag.add("trips", r["_row"], "service_id",
                        f"service_id '{r['service_id']}' 在 calendar 中不存在")
                ok = False
            if not r["service_id"]:
                bag.add("trips", r["_row"], "service_id", "service_id 不能为空")
                ok = False
            direction = r.get("direction_id", "")
            direction_val = None
            if direction:
                if direction in ("0", "1"):
                    direction_val = int(direction)
                else:
                    bag.add("trips", r["_row"], "direction_id",
                            "direction_id 必须为 0 或 1")
                    ok = False
            if ok and r["trip_id"] in trip_ids:
                trips_out.append((r["route_id"], r["service_id"], r["trip_id"],
                                  r.get("trip_headsign", ""), direction_val))
                trip_route[r["trip_id"]] = r["route_id"]

    # ---------- stop_times ----------
    stop_times_out: list[tuple] = []
    if raw["stop_times"]:
        # 先做单行类型校验与引用校验
        per_trip: dict[str, list[dict]] = {}
        for r in raw["stop_times"]:
            ok = True
            if r["trip_id"] and r["trip_id"] not in trip_ids:
                bag.add("stop_times", r["_row"], "trip_id",
                        f"trip_id '{r['trip_id']}' 在 trips 中不存在")
                ok = False
            if not r["trip_id"]:
                bag.add("stop_times", r["_row"], "trip_id", "trip_id 不能为空")
                ok = False
            if r["stop_id"] and r["stop_id"] not in stop_ids:
                bag.add("stop_times", r["_row"], "stop_id",
                        f"stop_id '{r['stop_id']}' 在 stops 中不存在")
                ok = False
            if not r["stop_id"]:
                bag.add("stop_times", r["_row"], "stop_id", "stop_id 不能为空")
                ok = False
            try:
                seq = int(r["stop_sequence"])
                if seq < 0:
                    raise ValueError
            except ValueError:
                bag.add("stop_times", r["_row"], "stop_sequence",
                        f"stop_sequence '{r['stop_sequence']}' 不是非负整数")
                ok = False
                seq = -1
            arr = parse_gtfs_time(r["arrival_time"])
            if arr is None:
                bag.add("stop_times", r["_row"], "arrival_time",
                        f"arrival_time '{r['arrival_time']}' 不是合法的 HH:MM:SS 时刻")
                ok = False
            dep = parse_gtfs_time(r["departure_time"])
            if dep is None:
                bag.add("stop_times", r["_row"], "departure_time",
                        f"departure_time '{r['departure_time']}' 不是合法的 HH:MM:SS 时刻")
                ok = False
            if ok and dep < arr:
                bag.add("stop_times", r["_row"], "departure_time",
                        f"发车时刻 {r['departure_time']} 早于到站时刻 {r['arrival_time']}")
                ok = False
            if ok:
                rec = {"_row": r["_row"], "trip_id": r["trip_id"], "seq": seq,
                       "stop_id": r["stop_id"], "arr": arr, "dep": dep}
                per_trip.setdefault(r["trip_id"], []).append(rec)

        # 每个 trip：stop_sequence 唯一 + 时刻严格递增
        for trip_id, recs in per_trip.items():
            by_seq: dict[int, dict] = {}
            unique_recs: list[dict] = []
            for rec in sorted(recs, key=lambda x: (x["seq"], x["_row"])):
                prev = by_seq.get(rec["seq"])
                if prev is not None:
                    bag.add("stop_times", rec["_row"], "stop_sequence",
                            f"trip '{trip_id}' 的 stop_sequence {rec['seq']} "
                            f"重复（首次出现于第 {prev['_row']} 行）")
                    continue
                by_seq[rec["seq"]] = rec
                unique_recs.append(rec)
            for prev, cur in zip(unique_recs, unique_recs[1:]):
                if cur["arr"] <= prev["dep"]:
                    bag.add(
                        "stop_times", cur["_row"], "arrival_time",
                        f"trip '{trip_id}' 时刻未递增：第 {cur['seq']} 站到站 "
                        f"{format_gtfs_time(cur['arr'])} 不晚于第 {prev['seq']} 站发车 "
                        f"{format_gtfs_time(prev['dep'])}")
                stop_times_out.append((trip_id, cur["seq"], cur["stop_id"],
                                       cur["arr"], cur["dep"]))
            if unique_recs:
                first = unique_recs[0]
                stop_times_out.append((trip_id, first["seq"], first["stop_id"],
                                       first["arr"], first["dep"]))

        # 每个有效 trip 必须至少有一条 stop_times
        if raw["trips"]:
            for r in raw["trips"]:
                tid = r["trip_id"]
                if tid in trip_ids and tid not in per_trip:
                    bag.add("trips", r["_row"], "trip_id",
                            f"trip '{tid}' 没有任何 stop_times 记录")

    if bag:
        return None, [e.as_dict() for e in bag.errors], bag.truncated

    tables = {
        "stops": stops_out,
        "routes": routes_out,
        "calendar": calendar_out,
        "trips": trips_out,
        "stop_times": stop_times_out,
    }
    return tables, [], False
