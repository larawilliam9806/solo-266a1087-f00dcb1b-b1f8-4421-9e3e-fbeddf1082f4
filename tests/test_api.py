"""API 集成测试：发布、失败保留正式版、按线路+运营日查询、跨午夜班次。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from conftest import upload, valid_files


def test_publish_and_query(client):
    r = upload(client, valid_files(), label="2026冬季")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "published"
    assert body["counts"]["stop_times"] == 11

    cur = client.get("/api/versions/current").json()
    assert cur["label"] == "2026冬季"

    routes = client.get("/api/routes").json()
    assert {r["route_id"] for r in routes["routes"]} == {"R1", "R2"}

    # 周一（2026-11-23 是周一）：WKD 与 ALL 都有效
    sch = client.get("/api/routes/R1/schedule?date=2026-11-23").json()
    assert sch["trip_count"] == 2
    t1 = sch["trips"][0]
    assert t1["trip_id"] == "T1"
    seqs = [s["stop_sequence"] for s in t1["stop_times"]]
    assert seqs == sorted(seqs)
    assert [s["stop_id"] for s in t1["stop_times"]] == ["S1", "S2", "S3", "S4"]

    # 周六（2026-11-21）：R1 只有 WKD 服务，不运营 → 无班次
    sch_sat = client.get("/api/routes/R1/schedule?date=2026-11-21").json()
    assert sch_sat["trip_count"] == 0
    # 但 R2（ALL）周六运营
    sch_sat2 = client.get("/api/routes/R2/schedule?date=2026-11-21").json()
    assert sch_sat2["trip_count"] == 1


def test_after_midnight_schedule(client):
    upload(client, valid_files())
    sch = client.get("/api/routes/R2/schedule?date=2026-11-23").json()
    n1 = sch["trips"][0]
    times = [(s["arrival"], s["arrival_day_offset"], s["arrival_clock"])
             for s in n1["stop_times"]]
    # 跨午夜：24:20 / 24:50 属于同一运营日，day_offset=1，钟点为次日凌晨
    assert times[2] == ("24:20:00", 1, "00:20:00")
    assert times[3] == ("24:50:00", 1, "00:50:00")
    # 时刻按 stop_sequence 递增排列
    arrs = [s["arrival_secs"] for s in n1["stop_times"]]
    assert arrs == sorted(arrs)


def test_failed_version_keeps_current(client):
    assert upload(client, valid_files(), label="good").status_code == 201
    good_id = client.get("/api/versions/current").json()["id"]

    bad = valid_files(stop_times=(
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,08:00:00,08:00:00,S1,1\n"
        "T1,08:10:00,08:12:00,S2,1\n"      # stop_sequence 重复
        "T1,07:00:00,07:05:00,SX,3\n"      # 未知站点 + 时刻倒退
        "T9,09:00:00,09:00:00,S1,1\n"))    # 未知 trip
    r = upload(client, bad, label="bad")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error_count"] >= 3
    assert all({"file", "row", "field", "message"} <= set(e) for e in detail["errors"])

    # 当前正式版不变，数据仍是旧版
    assert client.get("/api/versions/current").json()["id"] == good_id
    routes = client.get("/api/routes").json()
    assert routes["version_id"] == good_id
    assert {r["route_id"] for r in routes["routes"]} == {"R1", "R2"}

    # 失败版本有记录，可查询错误明细
    versions = client.get("/api/versions").json()
    failed = [v for v in versions["versions"] if v["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["error_count"] == detail["error_count"]
    errs = client.get(f"/api/versions/{failed[0]['id']}").json()["errors"]
    assert len(errs) == detail["error_count"]


def test_version_switch_atomic(client):
    assert upload(client, valid_files(), label="v1").status_code == 201
    v1 = client.get("/api/versions/current").json()["id"]

    # 第二版：线路 R1 改名、删掉 R2
    files2 = valid_files(
        routes="route_id,route_short_name,route_long_name,route_type\n"
               "R1,1路快线,中央车站-机场,3\n",
        trips="route_id,service_id,trip_id\nR1,WKD,T1\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:00:00,08:00:00,S1,1\n"
                   "T1,09:00:00,09:00:00,S4,2\n")
    assert upload(client, files2, label="v2").status_code == 201
    v2 = client.get("/api/versions/current").json()["id"]
    assert v2 != v1

    routes = client.get("/api/routes").json()
    assert routes["version_id"] == v2
    assert [r["route_short_name"] for r in routes["routes"]] == ["1路快线"]
    # 旧版本数据完整保留（不可变）
    assert client.get(f"/api/versions/{v1}").json()["status"] == "published"
    # 新版本中 R2 已不存在
    assert client.get("/api/routes/R2/schedule?date=2026-11-23").status_code == 404


def test_no_version_yet(client):
    assert client.get("/api/routes").status_code == 404
    assert client.get("/api/versions/current").status_code == 404


def test_bad_date_param(client):
    upload(client, valid_files())
    assert client.get("/api/routes/R1/schedule?date=2026-13-40").status_code == 422


def test_missing_file(client):
    r = client.post("/api/versions", data={"label": "x"}, files={
        "stops": ("stops.csv", "stop_id,stop_name\nS1,甲\n".encode(), "text/csv"),
        "routes": ("routes.csv", b"route_id\nR1\n", "text/csv"),
        "trips": ("trips.csv", b"route_id,service_id,trip_id\n", "text/csv"),
        "stop_times": ("stop_times.csv", b"trip_id\n", "text/csv"),
        # 缺 calendar
    })
    assert r.status_code == 422
