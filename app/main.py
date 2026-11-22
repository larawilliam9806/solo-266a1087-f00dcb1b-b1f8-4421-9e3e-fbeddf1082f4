"""FastAPI HTTP 层：版本上传发布 + 时刻查询。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date as date_type
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .gtfs import WEEKDAY_COLUMNS, format_gtfs_time, validate_version

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="公交 GTFS 时刻表发布", lifespan=lifespan)


# ---------------- 发布 ----------------

@app.post("/api/versions", status_code=201)
async def publish(
    label: str = Form(..., min_length=1, max_length=200),
    stops: UploadFile = File(...),
    routes: UploadFile = File(...),
    trips: UploadFile = File(...),
    stop_times: UploadFile = File(...),
    calendar: UploadFile = File(...),
):
    """上传同一版本的 5 个 CSV。整版校验通过才发布，否则记录错误并保持当前正式版。"""
    uploads = {"stops": stops, "routes": routes, "trips": trips,
               "stop_times": stop_times, "calendar": calendar}
    contents: dict[str, bytes] = {}
    for name, up in uploads.items():
        contents[name] = await up.read()

    tables, errors, truncated = validate_version(contents)
    conn = db.connect()
    try:
        if tables is None:
            vid = db.record_failed_version(conn, label, errors)
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "版本校验失败，未发布；当前正式版保持不变",
                    "version_id": vid,
                    "error_count": len(errors),
                    "truncated": truncated,
                    "errors": errors,
                })
        vid = db.publish_version(conn, label, tables)
        counts = {name: len(rows) for name, rows in tables.items()}
    finally:
        conn.close()
    return {"message": "版本已发布", "version_id": vid, "label": label,
            "status": "published", "counts": counts}


# ---------------- 版本信息 ----------------

@app.get("/api/versions")
def list_versions():
    conn = db.connect()
    try:
        cur_id = db.current_version_id(conn)
        rows = conn.execute(
            "SELECT id, label, status, error_count, created_at, published_at "
            "FROM versions ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    return {"current_version_id": cur_id,
            "versions": [dict(r) for r in rows]}


@app.get("/api/versions/current")
def current_version():
    conn = db.connect()
    try:
        vid = db.current_version_id(conn)
        if vid is None:
            raise HTTPException(404, "尚未发布任何版本")
        row = conn.execute(
            "SELECT id, label, status, created_at, published_at "
            "FROM versions WHERE id = ?", (vid,)).fetchone()
    finally:
        conn.close()
    return dict(row)


@app.get("/api/versions/{version_id}")
def version_detail(version_id: int):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id, label, status, error_count, created_at, published_at "
            "FROM versions WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"版本 {version_id} 不存在")
        errors = conn.execute(
            "SELECT file, row, field, message FROM version_errors "
            "WHERE version_id = ? ORDER BY id", (version_id,)).fetchall()
    finally:
        conn.close()
    result = dict(row)
    result["errors"] = [dict(e) for e in errors]
    return result


# ---------------- 查询（始终读取同一完整正式版） ----------------

def _require_current(conn) -> int:
    vid = db.current_version_id(conn)
    if vid is None:
        raise HTTPException(404, "尚未发布任何版本")
    return vid


@app.get("/api/routes")
def list_routes():
    conn = db.connect()
    try:
        vid = _require_current(conn)
        rows = conn.execute(
            "SELECT route_id, route_short_name, route_long_name, route_type "
            "FROM routes WHERE version_id = ? ORDER BY route_id", (vid,)).fetchall()
    finally:
        conn.close()
    return {"version_id": vid, "routes": [dict(r) for r in rows]}


@app.get("/api/routes/{route_id}/schedule")
def route_schedule(
    route_id: str,
    date: str = Query(..., description="运营日，格式 YYYY-MM-DD"),
):
    """按线路 + 运营日查询有序停站时刻。

    时刻以 GTFS 形式返回（小时可 >= 24，表示跨午夜后仍属该运营日），
    并附 day_offset（相对运营日的天数偏移）与实际钟点。
    """
    try:
        svc_date = date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, f"date '{date}' 不是合法的 YYYY-MM-DD 日期")
    weekday_col = WEEKDAY_COLUMNS[svc_date.weekday()]

    conn = db.connect()
    try:
        # 请求开始时读取一次指针，之后所有查询都固定在这个完整版本上
        vid = _require_current(conn)
        route = conn.execute(
            "SELECT route_id, route_short_name, route_long_name, route_type "
            "FROM routes WHERE version_id = ? AND route_id = ?",
            (vid, route_id)).fetchone()
        if route is None:
            raise HTTPException(404, f"线路 '{route_id}' 在当前版本 {vid} 中不存在")

        services = conn.execute(
            f"SELECT service_id FROM calendar WHERE version_id = ? "
            f"AND start_date <= ? AND end_date >= ? AND {weekday_col} = 1",
            (vid, date, date)).fetchall()
        service_ids = [r["service_id"] for r in services]

        trips_out = []
        if service_ids:
            placeholders = ",".join("?" for _ in service_ids)
            trips = conn.execute(
                f"SELECT trip_id, service_id, trip_headsign, direction_id "
                f"FROM trips WHERE version_id = ? AND route_id = ? "
                f"AND service_id IN ({placeholders})",
                (vid, route_id, *service_ids)).fetchall()
            for t in trips:
                stops = conn.execute(
                    "SELECT st.stop_sequence, st.stop_id, s.stop_name, "
                    "st.arrival_secs, st.departure_secs "
                    "FROM stop_times st JOIN stops s "
                    "  ON s.version_id = st.version_id AND s.stop_id = st.stop_id "
                    "WHERE st.version_id = ? AND st.trip_id = ? "
                    "ORDER BY st.stop_sequence",
                    (vid, t["trip_id"])).fetchall()
                trips_out.append({
                    "trip_id": t["trip_id"],
                    "service_id": t["service_id"],
                    "headsign": t["trip_headsign"],
                    "direction_id": t["direction_id"],
                    "stop_times": [_stop_time_dict(s) for s in stops],
                })
            trips_out.sort(
                key=lambda t: t["stop_times"][0]["departure_secs"]
                if t["stop_times"] else 0)
    finally:
        conn.close()

    return {
        "version_id": vid,
        "route": dict(route),
        "date": date,
        "weekday": weekday_col,
        "trip_count": len(trips_out),
        "trips": trips_out,
    }


def _stop_time_dict(row) -> dict:
    arr, dep = row["arrival_secs"], row["departure_secs"]
    return {
        "stop_sequence": row["stop_sequence"],
        "stop_id": row["stop_id"],
        "stop_name": row["stop_name"],
        "arrival": format_gtfs_time(arr),
        "departure": format_gtfs_time(dep),
        "arrival_secs": arr,
        "departure_secs": dep,
        "arrival_day_offset": arr // 86400,
        "departure_day_offset": dep // 86400,
        "arrival_clock": format_gtfs_time(arr % 86400),
        "departure_clock": format_gtfs_time(dep % 86400),
    }


# ---------------- 网页 ----------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
