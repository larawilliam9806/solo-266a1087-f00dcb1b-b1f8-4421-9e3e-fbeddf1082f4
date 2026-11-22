"""电子站牌发车包的生成、校验与离线还原。

包的目标是给容易断网的站牌使用：服务端生成一次 JSON 后，站牌网页
不再依赖网络即可按自然日还原发车表。

数据语义
--------
- 只包含所选版本中、所选站点的**非末站停靠**发车（同一 trip 中
  stop_sequence 最大的停站视为末站，末站不产生发车记录）。
- GTFS 时刻按北京时间理解。运营日 D 的 ``departure_secs`` 可以 >= 86400，
  实际发车日 = D + ``departure_secs // 86400``，钟点 = 秒数对 86400 取余。
  发车按实际发车日归入所选自然日；还原时同时给出原运营日。
- 同一钟点的多班车分别保留。每条记录含线路、班次、停站序号、发车钟点
  和相对运营日的天数偏移，因此可以还原出原运营日。

压缩格式（schema_version = 1）
-----------------------------
每个星期几（monday..sunday）最多一套发车模板；每个自然日只记录相对
模板的增删。对于某个星期几的 m 个自然日、某条候选发车在 p 个日子出现：

- 放进模板：1 条模板记录 + (m - p) 条删除记录
- 不放进模板：p 条新增记录

逐条取较小值即可使总记录数最小（并列时放进模板）。目标函数对每条
候选发车可加，所以这是全局最优。包内 ``stats.total_records`` 即
模板 + 新增 + 删除的记录数，``stats.minimum_records`` 给出该下界。
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from .gtfs import WEEKDAY_COLUMNS, format_gtfs_time

SCHEMA_NAME = "gtfs-stop-board"
SCHEMA_VERSION = 1
TIMEZONE = "Asia/Shanghai"
MAX_DAYS = 31

WEEKDAY_NAMES = tuple(WEEKDAY_COLUMNS)  # monday ... sunday
WEEKDAY_LABELS = {
    "monday": "周一",
    "tuesday": "周二",
    "wednesday": "周三",
    "thursday": "周四",
    "friday": "周五",
    "saturday": "周六",
    "sunday": "周日",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CLOCK_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$")


class BoardRequestError(ValueError):
    """生成站牌包时的请求错误（版本 / 站点 / 日期范围）。"""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class PackageError(ValueError):
    """站牌包内容不合法；``errors`` 为逐条原因。"""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("；".join(e["message"] for e in errors))


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def parse_iso_date(value) -> date | None:
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def weekday_name(day: date) -> str:
    return WEEKDAY_NAMES[day.weekday()]


def entry_identity(entry: dict) -> tuple:
    """发车记录在模板/增删中的稳定标识（不含绝对日期）。

    同一个星期几的不同周可以共用模板；还原时用自然日减 ``day_offset``
    得到原运营日。
    """
    return (
        str(entry.get("route_id", "")),
        str(entry.get("trip_id", "")),
        str(entry.get("stop_sequence", "")),
        str(entry.get("departure_clock", "")),
        str(entry.get("day_offset", "")),
    )


def _entry_sort_key(entry: dict) -> tuple:
    try:
        seq = int(entry.get("stop_sequence", 0))
    except (TypeError, ValueError):
        seq = 0
    try:
        offset = int(entry.get("day_offset", 0))
    except (TypeError, ValueError):
        offset = 0
    return (str(entry.get("departure_clock", "")),
            str(entry.get("route_id", "")),
            str(entry.get("trip_id", "")),
            seq, offset)


def _sort_entries(entries) -> list[dict]:
    return sorted(entries, key=_entry_sort_key)


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

def _require_version(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, label, status, published_at FROM versions WHERE id = ?",
        (version_id,)).fetchone()
    if row is None:
        raise BoardRequestError(f"版本 {version_id} 不存在", 404)
    if row["status"] != "published":
        raise BoardRequestError(
            f"版本 {version_id} 状态为 {row['status']}，未发布，不能生成站牌", 422)
    return row


def _active_services_by_date(conn: sqlite3.Connection, version_id: int,
                             dates: list[date]) -> dict[str, set[str]]:
    """计算每个运营日生效的 service_id。"""
    rows = conn.execute(
        "SELECT service_id, monday, tuesday, wednesday, thursday, friday, "
        "saturday, sunday, start_date, end_date FROM calendar "
        "WHERE version_id = ?", (version_id,)).fetchall()
    result: dict[str, set[str]] = {}
    for d in dates:
        iso = d.isoformat()
        col = weekday_name(d)
        result[iso] = {
            r["service_id"] for r in rows
            if r[col] == 1 and r["start_date"] <= iso <= r["end_date"]
        }
    return result


def _candidate_departures(conn: sqlite3.Connection, version_id: int,
                          stop_id: str) -> list[dict]:
    """该站的全部非末站停靠发车（未按自然日过滤）。"""
    rows = conn.execute(
        "SELECT st.trip_id, st.stop_sequence, st.departure_secs, "
        "       t.route_id, t.service_id, t.trip_headsign, t.direction_id, "
        "       (SELECT MAX(st2.stop_sequence) FROM stop_times st2 "
        "         WHERE st2.version_id = st.version_id "
        "           AND st2.trip_id = st.trip_id) AS last_sequence "
        "FROM stop_times st "
        "JOIN trips t ON t.version_id = st.version_id AND t.trip_id = st.trip_id "
        "WHERE st.version_id = ? AND st.stop_id = ? "
        "ORDER BY st.trip_id, st.stop_sequence, st.departure_secs",
        (version_id, stop_id)).fetchall()

    candidates: list[dict] = []
    seen: set[tuple] = set()
    for row in rows:
        last = row["last_sequence"]
        if last is None or row["stop_sequence"] >= last:
            continue  # 末站不发车
        secs = int(row["departure_secs"])
        offset, clock_secs = divmod(secs, 86400)
        entry = {
            "route_id": row["route_id"],
            "trip_id": row["trip_id"],
            "stop_sequence": int(row["stop_sequence"]),
            "departure_clock": format_gtfs_time(clock_secs),
            "day_offset": int(offset),
        }
        key = entry_identity(entry)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "service_id": row["service_id"],
            "headsign": row["trip_headsign"],
            "direction_id": row["direction_id"],
            "entry": entry,
        })
    return candidates


def _trip_metadata(conn: sqlite3.Connection, version_id: int,
                   trip_ids: set[str]) -> dict[str, dict]:
    if not trip_ids:
        return {}
    placeholders = ",".join("?" for _ in trip_ids)
    rows = conn.execute(
        f"SELECT t.trip_id, t.route_id, t.trip_headsign, t.direction_id, "
        f"       r.route_short_name, r.route_long_name, r.route_type "
        f"FROM trips t "
        f"LEFT JOIN routes r ON r.version_id = t.version_id "
        f"  AND r.route_id = t.route_id "
        f"WHERE t.version_id = ? AND t.trip_id IN ({placeholders})",
        (version_id, *sorted(trip_ids))).fetchall()
    return {
        r["trip_id"]: {
            "route_id": r["route_id"],
            "route_short_name": r["route_short_name"] or "",
            "route_long_name": r["route_long_name"] or "",
            "route_type": r["route_type"],
            "headsign": r["trip_headsign"] or "",
            "direction_id": r["direction_id"],
        }
        for r in rows
    }


def build_board_package(conn: sqlite3.Connection, version_id: int,
                        stop_id: str, start: date, days: int) -> dict:
    """生成某版本、某站、连续 ``days`` 个自然日的站牌包。"""
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_DAYS:
        raise BoardRequestError(f"连续天数必须在 1～{MAX_DAYS} 之间")
    if not stop_id:
        raise BoardRequestError("stop_id 不能为空")

    version = _require_version(conn, version_id)
    stop = conn.execute(
        "SELECT stop_id, stop_name FROM stops WHERE version_id = ? AND stop_id = ?",
        (version_id, stop_id)).fetchone()
    if stop is None:
        raise BoardRequestError(f"站点 '{stop_id}' 在版本 {version_id} 中不存在", 404)

    candidates = _candidate_departures(conn, version_id, stop_id)
    max_offset = max((c["entry"]["day_offset"] for c in candidates), default=0)

    try:
        service_dates = [start - timedelta(days=max_offset) + timedelta(days=i)
                         for i in range(max_offset + days)]
        board_dates = [start + timedelta(days=i) for i in range(days)]
    except (OverflowError, ValueError):
        raise BoardRequestError("日期范围超出可表示范围")
    active = _active_services_by_date(conn, version_id, service_dates)

    # 每个自然日 -> identity -> entry
    day_entries: dict[str, dict[tuple, dict]] = {}
    for d in board_dates:
        iso = d.isoformat()
        entries: dict[tuple, dict] = {}
        for cand in candidates:
            entry = cand["entry"]
            offset = entry["day_offset"]
            service_day = (d - timedelta(days=offset)).isoformat()
            if cand["service_id"] not in active.get(service_day, set()):
                continue
            entries[entry_identity(entry)] = dict(entry)
        day_entries[iso] = entries

    # 按自然日的星期几分组，尽量复用一套模板。
    groups: dict[str, list[str]] = {name: [] for name in WEEKDAY_NAMES}
    for d in board_dates:
        groups[weekday_name(d)].append(d.isoformat())

    weekdays_out: dict[str, dict] = {}
    used_trip_ids: set[str] = set()
    template_records = add_records = remove_records = 0
    minimum_records = 0
    departure_count = 0

    for name in WEEKDAY_NAMES:
        dates = groups[name]
        if not dates:
            continue
        m = len(dates)
        freq: Counter = Counter()
        representative: dict[tuple, dict] = {}
        for iso in dates:
            for key, entry in day_entries[iso].items():
                freq[key] += 1
                representative.setdefault(key, entry)

        # 逐条比较“进模板”与“不进模板”的记录数，取较小值。
        template_keys = {
            key for key, p in freq.items() if 1 + m - p <= p
        }
        minimum_records += sum(min(p, 1 + m - p) for p in freq.values())

        template = _sort_entries(representative[k] for k in template_keys)
        template_records += len(template)

        days_out = []
        for iso in dates:
            entries = day_entries[iso]
            add = _sort_entries(entries[k] for k in entries
                                if k not in template_keys)
            remove = _sort_entries(representative[k] for k in template_keys
                                   if k not in entries)
            add_records += len(add)
            remove_records += len(remove)
            departure_count += len(entries)
            used_trip_ids.update(e["trip_id"] for e in entries.values())
            days_out.append({"date": iso, "add": add, "remove": remove})

        weekdays_out[name] = {
            "weekday": name,
            "weekday_label": WEEKDAY_LABELS[name],
            "template": template,
            "days": days_out,
        }

    total_records = template_records + add_records + remove_records
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "timezone": TIMEZONE,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_version": {
            "id": version["id"],
            "label": version["label"],
            "published_at": version["published_at"],
        },
        "stop": {
            "stop_id": stop["stop_id"],
            "stop_name": stop["stop_name"],
        },
        "range": {
            "start_date": board_dates[0].isoformat(),
            "end_date": board_dates[-1].isoformat(),
            "days": days,
        },
        "weekdays": weekdays_out,
        "trips": _trip_metadata(conn, version_id, used_trip_ids),
        "stats": {
            "days": days,
            "departures": departure_count,
            "template_records": template_records,
            "add_records": add_records,
            "remove_records": remove_records,
            "total_records": total_records,
            "minimum_records": minimum_records,
            "is_minimal": total_records == minimum_records,
        },
    }


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _issue(path: str, message: str, severity: str = "error") -> dict:
    return {"path": path, "message": message, "severity": severity}


def _validate_entry(entry, path: str, errors: list[dict]) -> None:
    if not isinstance(entry, dict):
        errors.append(_issue(path, "发车记录必须是 JSON 对象"))
        return
    for field in ("route_id", "trip_id"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(_issue(f"{path}.{field}", f"{field} 必须是非空字符串"))
    seq = entry.get("stop_sequence")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        errors.append(_issue(f"{path}.stop_sequence",
                             "stop_sequence 必须是非负整数"))
    clock = entry.get("departure_clock")
    if not isinstance(clock, str) or not CLOCK_RE.match(clock):
        errors.append(_issue(f"{path}.departure_clock",
                             "departure_clock 必须是 00:00:00～23:59:59 的钟点"))
    offset = entry.get("day_offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        errors.append(_issue(f"{path}.day_offset",
                             "day_offset 必须是非负整数（相对运营日的天数）"))


def _validate_entry_list(value, path: str, errors: list[dict]) -> list:
    if not isinstance(value, list):
        errors.append(_issue(path, "必须是 JSON 数组"))
        return []
    for i, entry in enumerate(value):
        _validate_entry(entry, f"{path}[{i}]", errors)
    return value


def _claimed_minimum(weekdays: dict) -> int:
    """由各日最终发车集合计算模板+增删的记录数下界。"""
    total = 0
    for group in weekdays.values():
        dates = group.get("days") or []
        m = len(dates)
        template_keys = {
            entry_identity(e) for e in (group.get("template") or [])
            if isinstance(e, dict)
        }
        freq: Counter = Counter()
        for day in dates:
            if not isinstance(day, dict):
                continue
            final = set(template_keys)
            for e in day.get("remove") or []:
                if isinstance(e, dict):
                    final.discard(entry_identity(e))
            for e in day.get("add") or []:
                if isinstance(e, dict):
                    final.add(entry_identity(e))
            freq.update(final)
        total += sum(min(p, 1 + m - p) for p in freq.values())
    return total


def validate_package(data) -> list[dict]:
    """校验站牌包。

    返回问题列表，每项 ``{path, message, severity}``。``severity == "error"``
    时无法安全还原；``"warning"`` 表示仍可还原，但不满足最少记录等要求。
    """
    errors: list[dict] = []
    if not isinstance(data, dict):
        return [_issue("$", "包顶层必须是 JSON 对象")]

    if data.get("schema") != SCHEMA_NAME:
        errors.append(_issue("$.schema", f"schema 必须为 '{SCHEMA_NAME}'"))
    version = data.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        errors.append(_issue("$.schema_version",
                             f"不支持的包格式版本 {version!r}；本页面支持 {SCHEMA_VERSION}"))
    tz = data.get("timezone")
    if tz is not None and tz not in (TIMEZONE, "北京时间"):
        errors.append(_issue("$.timezone", f"timezone 必须为 {TIMEZONE}（北京时间）"))

    source = data.get("source_version")
    if not isinstance(source, dict):
        errors.append(_issue("$.source_version", "缺少来源版号 source_version"))
    else:
        if not isinstance(source.get("id"), (int, str)) or source.get("id") == "":
            errors.append(_issue("$.source_version.id", "来源版号 id 不能为空"))
        if not isinstance(source.get("label"), str) or not source.get("label"):
            errors.append(_issue("$.source_version.label", "来源版本名称不能为空"))

    stop = data.get("stop")
    if not isinstance(stop, dict) or not isinstance(stop.get("stop_id"), str) \
            or not stop.get("stop_id"):
        errors.append(_issue("$.stop.stop_id", "缺少站点 stop_id"))

    rng = data.get("range")
    start = days = None
    expected_dates: list[str] = []
    if not isinstance(rng, dict):
        errors.append(_issue("$.range", "缺少日期范围 range"))
    else:
        start = parse_iso_date(rng.get("start_date"))
        if start is None:
            errors.append(_issue("$.range.start_date",
                                 "start_date 必须是合法的 YYYY-MM-DD 日期"))
        days = rng.get("days")
        if not isinstance(days, int) or isinstance(days, bool) \
                or not 1 <= days <= MAX_DAYS:
            errors.append(_issue("$.range.days", f"days 必须是 1～{MAX_DAYS} 的整数"))
            days = None
        if start is not None and days:
            try:
                expected_dates = [(start + timedelta(days=i)).isoformat()
                                  for i in range(days)]
            except (OverflowError, ValueError):
                errors.append(_issue("$.range.start_date", "日期范围超出可表示范围"))
                expected_dates = []
            end = rng.get("end_date")
            if expected_dates and end is not None and end != expected_dates[-1]:
                errors.append(_issue(
                    "$.range.end_date",
                    f"end_date 应为 {expected_dates[-1]}，与实际范围不一致"))

    weekdays = data.get("weekdays")
    if not isinstance(weekdays, dict):
        errors.append(_issue("$.weekdays", "缺少按星期几组织的模板 weekdays"))
        weekdays = {}

    seen_dates: dict[str, str] = {}
    for name in weekdays:
        if name not in WEEKDAY_NAMES:
            errors.append(_issue(f"$.weekdays.{name}", f"未知的星期几 '{name}'"))

    for name in WEEKDAY_NAMES:
        if name not in weekdays:
            continue
        group = weekdays[name]
        path = f"$.weekdays.{name}"
        if not isinstance(group, dict):
            errors.append(_issue(path, "星期几模板必须是 JSON 对象"))
            continue
        template = _validate_entry_list(group.get("template"), f"{path}.template",
                                        errors)
        template_keys: set[tuple] = set()
        for i, entry in enumerate(template):
            if not isinstance(entry, dict):
                continue
            key = entry_identity(entry)
            if key in template_keys:
                errors.append(_issue(f"{path}.template[{i}]",
                                     "模板内发车记录重复"))
            template_keys.add(key)

        day_list = group.get("days")
        if not isinstance(day_list, list):
            errors.append(_issue(f"{path}.days", "必须按自然日列出 days 数组"))
            continue
        if start is not None and days:
            expected_count = sum(1 for iso in expected_dates
                                 if weekday_name(date.fromisoformat(iso)) == name)
            if len(day_list) != expected_count:
                errors.append(_issue(
                    f"{path}.days",
                    f"应有 {expected_count} 个{name}，实际 {len(day_list)} 个"))

        for i, day in enumerate(day_list):
            dpath = f"{path}.days[{i}]"
            if not isinstance(day, dict):
                errors.append(_issue(dpath, "自然日项必须是 JSON 对象"))
                continue
            iso = day.get("date")
            parsed = parse_iso_date(iso)
            if parsed is None:
                errors.append(_issue(f"{dpath}.date",
                                     "date 必须是合法的 YYYY-MM-DD 日期"))
                continue
            if weekday_name(parsed) != name:
                errors.append(_issue(f"{dpath}.date",
                                     f"{iso} 不是{name}，放错了模板"))
            if iso in seen_dates:
                errors.append(_issue(f"{dpath}.date",
                                     f"日期 {iso} 重复（已出现在 {seen_dates[iso]}）"))
            seen_dates[iso] = dpath
            if expected_dates and iso not in expected_dates:
                errors.append(_issue(f"{dpath}.date", f"日期 {iso} 不在所选范围内"))

            add = _validate_entry_list(day.get("add"), f"{dpath}.add", errors)
            remove = _validate_entry_list(day.get("remove"), f"{dpath}.remove",
                                          errors)
            add_keys: set[tuple] = set()
            for j, entry in enumerate(add):
                if not isinstance(entry, dict):
                    continue
                key = entry_identity(entry)
                if key in add_keys:
                    errors.append(_issue(f"{dpath}.add[{j}]", "新增记录重复"))
                if key in template_keys:
                    errors.append(_issue(f"{dpath}.add[{j}]",
                                         "新增记录已在模板中，应删除该 add"))
                add_keys.add(key)
            remove_keys: set[tuple] = set()
            for j, entry in enumerate(remove):
                if not isinstance(entry, dict):
                    continue
                key = entry_identity(entry)
                if key in remove_keys:
                    errors.append(_issue(f"{dpath}.remove[{j}]", "删除记录重复"))
                if key not in template_keys:
                    errors.append(_issue(
                        f"{dpath}.remove[{j}]",
                        "删除记录不在模板中，应改用 add 或删除该 remove"))
                remove_keys.add(key)

    if expected_dates:
        for iso in expected_dates:
            if iso not in seen_dates:
                errors.append(_issue("$.weekdays",
                                     f"日期 {iso} 没有对应的日增删记录"))
        extra = [iso for iso in seen_dates if iso not in expected_dates]
        if extra:
            errors.append(_issue("$.weekdays",
                                 "范围外日期: " + ", ".join(sorted(extra))))

    trips = data.get("trips")
    if trips is not None:
        if not isinstance(trips, dict):
            errors.append(_issue("$.trips", "trips 必须是 JSON 对象"))
        else:
            for name in weekdays:
                group = weekdays.get(name)
                if not isinstance(group, dict):
                    continue
                entries = list(group.get("template") or [])
                for day in group.get("days") or []:
                    if isinstance(day, dict):
                        entries += list(day.get("add") or [])
                        entries += list(day.get("remove") or [])
                for entry in entries:
                    if isinstance(entry, dict):
                        tid = entry.get("trip_id")
                        if tid and tid not in trips:
                            errors.append(_issue(
                                f"$.trips.{tid}",
                                f"班次 '{tid}' 缺少线路/方向元数据"))

    stats = data.get("stats")
    if isinstance(stats, dict):
        counts = {"template_records": 0, "add_records": 0, "remove_records": 0}
        for group in weekdays.values():
            if not isinstance(group, dict):
                continue
            counts["template_records"] += len(group.get("template") or [])
            for day in group.get("days") or []:
                if isinstance(day, dict):
                    counts["add_records"] += len(day.get("add") or [])
                    counts["remove_records"] += len(day.get("remove") or [])
        counts["total_records"] = sum(counts.values())
        for field, actual in counts.items():
            if stats.get(field) is not None and stats.get(field) != actual:
                errors.append(_issue(
                    f"$.stats.{field}",
                    f"统计为 {stats.get(field)} 条，实际 {actual} 条",
                    "warning"))

    if not any(e["severity"] == "error" for e in errors):
        minimum = _claimed_minimum(weekdays)
        actual = 0
        for group in weekdays.values():
            if not isinstance(group, dict):
                continue
            actual += len(group.get("template") or [])
            for day in group.get("days") or []:
                if isinstance(day, dict):
                    actual += len(day.get("add") or [])
                    actual += len(day.get("remove") or [])
        if actual != minimum:
            errors.append(_issue(
                "$.weekdays",
                f"模板与增删共 {actual} 条发车记录，不是最少；按每个星期几"
                f"选择最优模板应为 {minimum} 条"))
    return errors


# ---------------------------------------------------------------------------
# 还原
# ---------------------------------------------------------------------------

def expand_package(data: dict) -> dict:
    """把站牌包还原为按自然日排列的发车表。

    只依赖包内数据；不访问数据库或网络，因此旧包在新版本发布后仍可还原。
    """
    problems = validate_package(data)
    blocking = [p for p in problems if p["severity"] == "error"]
    if blocking:
        raise PackageError(blocking)

    start = parse_iso_date(data["range"]["start_date"])
    days = int(data["range"]["days"])
    weekday_groups = data.get("weekdays") or {}
    trip_meta = data.get("trips") or {}
    out_days = []

    for i in range(days):
        day = start + timedelta(days=i)
        iso = day.isoformat()
        name = weekday_name(day)
        group = weekday_groups[name]
        day_rec = next(d for d in group["days"] if d["date"] == iso)

        final: dict[tuple, dict] = {}
        for entry in group.get("template") or []:
            final[entry_identity(entry)] = dict(entry)
        for entry in day_rec.get("remove") or []:
            final.pop(entry_identity(entry), None)
        for entry in day_rec.get("add") or []:
            final[entry_identity(entry)] = dict(entry)

        departures = []
        for entry in _sort_entries(final.values()):
            item = dict(entry)
            item["service_date"] = (
                day - timedelta(days=int(entry["day_offset"]))).isoformat()
            item["route"] = trip_meta.get(entry["trip_id"], {})
            departures.append(item)

        out_days.append({
            "date": iso,
            "weekday": name,
            "weekday_label": WEEKDAY_LABELS[name],
            "departures": departures,
        })

    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "source_version": data["source_version"],
        "stop": data["stop"],
        "range": data["range"],
        "days": out_days,
    }
