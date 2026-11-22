"""站牌发车包：生成语义、模板最少化、离线还原、错误说明、旧包兼容。"""
import copy
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app import board
from app.gtfs import format_gtfs_time
from conftest import upload, valid_files


def make_pkg(client, *, version=None, stop="S2", start="2026-11-23", days=1):
    params = {"stop_id": stop, "start_date": start, "days": days}
    if version is None:
        version = client.get("/api/versions/current").json()["id"]
    params["version_id"] = version
    r = client.get("/api/board/package", params=params)
    return r


def all_entries(pkg):
    """把包中还原后的全部发车记录展开，便于断言。"""
    return board.expand_package(pkg)


def test_package_only_non_terminal_departures_and_fields(client):
    upload(client, valid_files())
    r = make_pkg(client, stop="S2", start="2026-11-23", days=1)
    assert r.status_code == 200, r.text
    pkg = r.json()

    assert pkg["schema"] == "gtfs-stop-board"
    assert pkg["schema_version"] == 1
    assert pkg["source_version"]["id"] == client.get("/api/versions/current").json()["id"]
    assert pkg["stop"]["stop_id"] == "S2"
    assert pkg["range"] == {"start_date": "2026-11-23", "end_date": "2026-11-23",
                            "days": 1}

    days = board.expand_package(pkg)["days"]
    assert len(days) == 1
    # 2026-11-23 周一：R1/T1 在 S2 的停站序号 2（非末站）；R2/N1 的 S2 为序号 2（非末站）
    deps = days[0]["departures"]
    assert {(d["trip_id"], d["stop_sequence"], d["departure_clock"])
            for d in deps} == {("T1", 2, "08:12:00"), ("N1", 2, "23:58:00")}
    for d in deps:
        assert d["route_id"]
        assert d["service_date"] == "2026-11-23"
    # 每条都有线路元数据
    assert pkg["trips"]["T1"]["route_short_name"] == "1路"
    assert pkg["trips"]["N1"]["route_short_name"] == "夜1路"
    assert deps[0]["route"]["route_short_name"] == "1路"


def test_terminal_stop_excluded(client):
    upload(client, valid_files())
    # S1 是 T1/N1 的起点、T2 的终点；作为发车源时 T2 的停靠是末站，应排除
    pkg = make_pkg(client, stop="S1", days=1).json()
    deps = board.expand_package(pkg)["days"][0]["departures"]
    assert {(d["trip_id"], d["stop_sequence"]) for d in deps} == {("T1", 1), ("N1", 1)}
    # S4 是 T1/N1 的末站，但也是 T2 的起点 → 只保留 T2 的非末站发车
    pkg = make_pkg(client, stop="S4", days=1).json()
    deps = board.expand_package(pkg)["days"][0]["departures"]
    assert {(d["trip_id"], d["stop_sequence"]) for d in deps} == {("T2", 1)}


def test_after_midnight_falls_on_actual_departure_day(client):
    upload(client, valid_files())
    # N1 于 2026-11-23（运营日）在 S3 的 24:22 发车，实际为 11-24 00:22
    pkg = make_pkg(client, stop="S3", start="2026-11-23", days=2).json()
    days = {d["date"]: d for d in board.expand_package(pkg)["days"]}
    # 11-23 当天凌晨的发车来自前一运营日（11-22，ALL 服务）
    n1_23 = [d for d in days["2026-11-23"]["departures"] if d["trip_id"] == "N1"]
    assert len(n1_23) == 1
    assert n1_23[0]["departure_clock"] == "00:22:00"
    assert n1_23[0]["service_date"] == "2026-11-22"
    # 11-23 运营日的 24:22 归入 11-24 00:22
    n1_24 = [d for d in days["2026-11-24"]["departures"] if d["trip_id"] == "N1"]
    assert len(n1_24) == 1
    d = n1_24[0]
    assert d["departure_clock"] == "00:22:00"
    assert d["day_offset"] == 1
    assert d["service_date"] == "2026-11-23"      # 原运营日可追溯
    assert d["route_id"] == "R2"
    assert d["stop_sequence"] == 3
    # 11-23 23:58 的 S2 发车（N1 序号 2）仍归入 11-23
    pkg2 = make_pkg(client, stop="S2", start="2026-11-23", days=2).json()
    days2 = {x["date"]: x for x in board.expand_package(pkg2)["days"]}
    assert [x["departure_clock"] for x in days2["2026-11-23"]["departures"]
            if x["trip_id"] == "N1"] == ["23:58:00"]


def test_same_clock_departures_listed_separately(client):
    # 两个不同班次在同一钟点、同一站点发车
    files = valid_files(
        trips="route_id,service_id,trip_id\nR1,WKD,A\nR1,WKD,B\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "A,08:00:00,08:10:00,S1,1\nA,08:30:00,08:30:00,S2,2\n"
                   "A,09:00:00,09:00:00,S3,3\n"
                   "B,08:05:00,08:10:00,S1,1\nB,08:30:00,08:30:00,S2,2\n"
                   "B,09:00:00,09:00:00,S3,3\n")
    assert upload(client, files).status_code == 201
    pkg = make_pkg(client, stop="S2", days=1).json()
    deps = board.expand_package(pkg)["days"][0]["departures"]
    same = [d for d in deps if d["departure_clock"] == "08:30:00"]
    assert {d["trip_id"] for d in same} == {"A", "B"}
    assert len(same) == 2


def test_template_minimal_and_shared_across_weeks(client):
    upload(client, valid_files())
    # 2026-11-23 起 14 天 = 每个星期几各 2 天
    pkg = make_pkg(client, stop="S2", start="2026-11-23", days=14).json()
    assert pkg["stats"]["is_minimal"] is True
    assert pkg["stats"]["total_records"] == pkg["stats"]["minimum_records"]
    # 每个出现的星期几最多一套模板；两周共用
    for name, group in pkg["weekdays"].items():
        assert len(group["days"]) == 2
        assert isinstance(group["template"], list)
    # 工作日两周相同 → 模板 + 0 增删
    mon = pkg["weekdays"]["monday"]
    assert len(mon["template"]) == 2
    assert all(day["add"] == [] and day["remove"] == [] for day in mon["days"])


def test_template_chooses_common_departures(client):
    # 某星期几 3 个自然日中，一条发车出现 2 次，另一条只出现 1 次
    # 出现 2 次：进模板 1+1=2， 不进 2 → 并列，进模板
    # 出现 1 次：进模板 1+2=3， 不进 1 → 不进模板（记 add）
    files = valid_files(
        trips="route_id,service_id,trip_id\n"
              "R1,ALL,T1\nR1,ALL,T2\nR1,ALL,T3\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:00:00,08:00:00,S1,1\nT1,08:30:00,08:30:00,S2,2\n"
                   "T1,09:00:00,09:00:00,S3,3\n"
                   "T2,09:00:00,09:00:00,S1,1\nT2,09:30:00,09:30:00,S2,2\n"
                   "T2,10:00:00,10:00:00,S3,3\n"
                   "T3,10:00:00,10:00:00,S1,1\nT3,10:30:00,10:30:00,S2,2\n"
                   "T3,11:00:00,11:00:00,S3,3\n")
    assert upload(client, files).status_code == 201
    # 2026-11-23/30, 12-07 为周一
    pkg = make_pkg(client, stop="S2", start="2026-11-23", days=15).json()
    mon = pkg["weekdays"]["monday"]
    assert [d["date"] for d in mon["days"]] == [
        "2026-11-23", "2026-11-30", "2026-12-07"]
    # 三条每天出现 → 进模板 1+0 优于不进 3
    assert len(mon["template"]) == 3
    assert pkg["stats"]["is_minimal"] is True
    # 还原后每个周一都有 3 班
    days = board.expand_package(pkg)["days"]
    for d in days:
        if d["weekday"] == "monday":
            assert len(d["departures"]) == 3


def test_calendar_gaps_become_add_remove(client):
    # 两个 service 各只覆盖一个周一：两条发车各出现 1/2 天
    files = valid_files(
        calendar="service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                 "start_date,end_date\n"
                 "A,1,1,1,1,1,1,1,20261123,20261123\n"
                 "B,1,1,1,1,1,1,1,20261130,20261130\n",
        trips="route_id,service_id,trip_id\nR1,A,TA\nR1,B,TB\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "TA,08:00:00,08:00:00,S1,1\nTA,08:30:00,08:30:00,S2,2\n"
                   "TA,09:00:00,09:00:00,S3,3\n"
                   "TB,08:00:00,08:00:00,S1,1\nTB,08:30:00,08:30:00,S2,2\n"
                   "TB,09:00:00,09:00:00,S3,3\n")
    assert upload(client, files).status_code == 201
    pkg = make_pkg(client, stop="S2", start="2026-11-23", days=15).json()
    mon = pkg["weekdays"]["monday"]
    # TA 出现在 1 天、TB 出现在另 1 天：各自进模板(1+1=2) 与不进(1) 比较
    # 均不进模板：2 条 add，0 删除
    assert mon["template"] == []
    total_add = sum(len(d["add"]) for d in mon["days"])
    assert total_add == 2
    assert pkg["stats"]["is_minimal"] is True


def test_package_validates_ok_and_expand_rejects_corrupt(client):
    upload(client, valid_files())
    pkg = make_pkg(client, stop="S2", start="2026-11-23", days=7).json()

    ok = client.post("/api/board/validate", json=pkg).json()
    assert ok["ok"] is True and ok["error_count"] == 0
    expanded = client.post("/api/board/expand", json=pkg).json()
    assert len(expanded["days"]) == 7
    assert expanded["source_version"]["id"] == pkg["source_version"]["id"]

    bad = copy.deepcopy(pkg)
    bad["weekdays"]["monday"]["template"].append({"route_id": "R1"})
    result = client.post("/api/board/validate", json=bad).json()
    assert result["ok"] is False
    assert any("trip_id" in p["message"] for p in result["problems"])
    r = client.post("/api/board/expand", json=bad)
    assert r.status_code == 422
    assert r.json()["detail"]["problems"]


def test_package_error_explains_why(client):
    upload(client, valid_files())
    pkg = make_pkg(client, stop="S2", start="2026-11-23", days=7).json()

    cases = []

    missing_trip = copy.deepcopy(pkg)
    missing_trip["trips"].pop("T1")
    cases.append((missing_trip, "缺少线路/方向元数据"))

    wrong_date = copy.deepcopy(pkg)
    wrong_date["weekdays"]["monday"]["days"][0]["date"] = "2026-12-01"  # 周二却放在周一
    cases.append((wrong_date, "不是monday"))

    unknown_version = copy.deepcopy(pkg)
    unknown_version["schema_version"] = 99
    cases.append((unknown_version, "不支持的包格式版本"))

    missing_day = copy.deepcopy(pkg)
    missing_day["weekdays"]["monday"]["days"].pop()
    cases.append((missing_day, "应有 1 个monday"))

    bad_remove = copy.deepcopy(pkg)
    # 引用一条根本不在模板里的发车作为删除项
    bad_remove["weekdays"]["monday"]["days"][0]["remove"].append(
        {"route_id": "R1", "trip_id": "T2", "stop_sequence": 2,
         "departure_clock": "10:32:00", "day_offset": 0})
    cases.append((bad_remove, "删除记录不在模板中"))

    for bad, needle in cases:
        problems = board.validate_package(bad)
        assert any(needle in p["message"] for p in problems), (needle, problems)
        with pytest.raises(board.PackageError):
            board.expand_package(bad)


def test_package_reports_non_minimal_template(client):
    upload(client, valid_files())
    pkg = make_pkg(client, stop="S2", start="2026-11-23", days=14).json()
    # 合法但非最少：把模板里的一条挪到每一天的 add，记录数变多
    mon = pkg["weekdays"]["monday"]
    moved = mon["template"].pop(0)
    for day in mon["days"]:
        day["add"].append(copy.deepcopy(moved))
    pkg["stats"]["template_records"] -= 1
    pkg["stats"]["add_records"] += len(mon["days"])
    pkg["stats"]["total_records"] += len(mon["days"]) - 1
    problems = board.validate_package(pkg)
    assert any(p["severity"] == "error" and "不是最少" in p["message"]
               for p in problems)
    with pytest.raises(board.PackageError):
        board.expand_package(pkg)
    # 已打开的包不受影响（前端保留 openedPackages）；重新生成即为最少
    good = make_pkg(client, stop="S2", start="2026-11-23", days=14).json()
    assert board.validate_package(good) == []


def test_old_package_still_restores_after_new_version(client):
    upload(client, valid_files(), label="v1")
    pkg_v1 = make_pkg(client, stop="S2", start="2026-11-23", days=7).json()
    v1_id = pkg_v1["source_version"]["id"]

    # 新版本换掉线路数据
    files2 = valid_files(
        routes="route_id,route_short_name,route_long_name,route_type\n"
               "R1,1路改线,改线后,3\n",
        trips="route_id,service_id,trip_id\nR1,WKD,T1\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,07:00:00,07:00:00,S1,1\n"
                   "T1,07:30:00,07:30:00,S2,2\n")
    assert upload(client, files2, label="v2").status_code == 201
    v2_id = client.get("/api/versions/current").json()["id"]
    assert v2_id != v1_id

    # 旧包不依赖服务端当前版本，仍按包内数据还原
    result = board.expand_package(pkg_v1)
    assert result["source_version"]["id"] == v1_id
    first = result["days"][0]["departures"]
    assert any(d["trip_id"] == "T1" and d["route"]["route_short_name"] == "1路"
               for d in first)
    assert all(d["departure_clock"] != "07:00:00" for d in first)

    # 通过 API 也能还原旧包
    r = client.post("/api/board/expand", json=pkg_v1)
    assert r.status_code == 200
    assert r.json()["source_version"]["id"] == v1_id


def test_selected_published_version_not_only_current(client):
    upload(client, valid_files(), label="v1")
    v1 = client.get("/api/versions/current").json()["id"]
    # 第二版必须仍是合法版本（R2 的 trips 被删掉，线路名改为 9路）
    files2 = valid_files(
        routes="route_id,route_short_name,route_long_name,route_type\n"
               "R1,1路快线,改线后,3\n",
        trips="route_id,service_id,trip_id\nR1,WKD,T1\nR1,WKD,T2\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:00:00,08:00:00,S1,1\nT1,08:10:00,08:12:00,S2,2\n"
                   "T1,08:25:00,08:25:00,S3,3\n"
                   "T2,10:00:00,10:00:00,S3,1\nT2,10:30:00,10:32:00,S2,2\n")
    assert upload(client, files2, label="v2").status_code == 201
    v2 = client.get("/api/versions/current").json()["id"]
    assert v1 != v2

    r = make_pkg(client, version=v1, stop="S2", days=1)
    assert r.status_code == 200
    assert r.json()["source_version"]["id"] == v1
    assert r.json()["trips"]["T1"]["route_short_name"] == "1路"
    # 新版本也可指定生成
    r2 = make_pkg(client, version=v2, stop="S2", days=1)
    assert r2.status_code == 200
    assert r2.json()["source_version"]["id"] == v2
    assert r2.json()["trips"]["T1"]["route_short_name"] == "1路快线"


def test_board_versions_and_stops(client):
    upload(client, valid_files(), label="v1")
    versions = client.get("/api/board/versions").json()
    assert len(versions["versions"]) == 1
    assert versions["current_version_id"] == versions["versions"][0]["id"]

    stops = client.get("/api/board/stops",
                       params={"version_id": versions["versions"][0]["id"]}).json()
    ids = {s["stop_id"] for s in stops["stops"]}
    # S4 是 T1/N1 的末站，但仍是 T2 的起点 → 仍可作为发车源
    assert ids == {"S1", "S2", "S3", "S4"}
    departures = {s["stop_id"]: s["departures"] for s in stops["stops"]}
    assert departures["S4"] == 1

    # 失败版本不能生成
    bad = valid_files(stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                                 "TX,08:00:00,08:00:00,S1,1\n")
    assert upload(client, bad, label="bad").status_code == 422
    failed_id = [v for v in client.get("/api/versions").json()["versions"]
                 if v["status"] == "failed"][0]["id"]
    r = make_pkg(client, version=failed_id, stop="S2", days=1)
    assert r.status_code == 422
    assert "未发布" in r.json()["detail"]


def test_request_validation(client):
    upload(client, valid_files())
    vid = client.get("/api/versions/current").json()["id"]
    for days in (0, 32, -1):
        r = client.get("/api/board/package", params={
            "version_id": vid, "stop_id": "S2",
            "start_date": "2026-11-23", "days": days})
        assert r.status_code == 422
    r = client.get("/api/board/package", params={
        "version_id": vid, "stop_id": "S2", "start_date": "2026-13-01", "days": 1})
    assert r.status_code == 422
    r = client.get("/api/board/package", params={
        "version_id": vid, "stop_id": "NOPE", "start_date": "2026-11-23", "days": 1})
    assert r.status_code == 404
    r = client.get("/api/board/package", params={
        "version_id": 9999, "stop_id": "S2", "start_date": "2026-11-23", "days": 1})
    assert r.status_code == 404


def test_max_31_days_accepted(client):
    upload(client, valid_files())
    r = make_pkg(client, stop="S2", start="2026-11-01", days=31)
    assert r.status_code == 200
    pkg = r.json()
    assert pkg["range"]["days"] == 31
    assert pkg["range"]["end_date"] == "2026-12-01"
    assert len(board.expand_package(pkg)["days"]) == 31
    assert pkg["stats"]["is_minimal"] is True


def test_download_has_attachment_filename(client):
    upload(client, valid_files())
    r = make_pkg(client, stop="S2", start="2026-11-23", days=2)
    assert "attachment" in r.headers["content-disposition"]
    assert ".json" in r.headers["content-disposition"]


def test_spillover_from_day_before_range_is_included(client):
    # 运营日 11-22（周日）的 24:30 发车，实际为 11-23 00:30，应出现在 11-23 的站牌里
    files = valid_files(
        calendar="service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                 "start_date,end_date\n"
                 "SUN,0,0,0,0,0,0,1,20261122,20261122\n",
        trips="route_id,service_id,trip_id\nR2,SUN,N9\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "N9,24:00:00,24:30:00,S1,1\n"
                   "N9,25:10:00,25:10:00,S2,2\n")
    assert upload(client, files).status_code == 201
    pkg = make_pkg(client, stop="S1", start="2026-11-23", days=1).json()
    deps = board.expand_package(pkg)["days"][0]["departures"]
    assert len(deps) == 1
    d = deps[0]
    assert d["departure_clock"] == "00:30:00"
    assert d["day_offset"] == 1
    assert d["service_date"] == "2026-11-22"
    # 11-22 当天的站牌里没有它（那是前一天的运营日结果）
    pkg_prev = make_pkg(client, stop="S1", start="2026-11-22", days=1).json()
    assert board.expand_package(pkg_prev)["days"][0]["departures"] == []


def test_large_day_offset(client):
    # 时刻允许 HH >= 48：运营日 D 的 48:10 属于 D+2
    files = valid_files(
        calendar="service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                 "start_date,end_date\n"
                 "JUST,1,1,1,1,1,1,1,20261123,20261123\n",
        trips="route_id,service_id,trip_id\nR2,JUST,LONG\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "LONG,48:00:00,48:10:00,S1,1\n"
                   "LONG,48:30:00,48:30:00,S2,2\n")
    assert upload(client, files).status_code == 201
    pkg = make_pkg(client, stop="S1", start="2026-11-23", days=3).json()
    days = {d["date"]: d for d in board.expand_package(pkg)["days"]}
    assert days["2026-11-23"]["departures"] == []
    assert days["2026-11-24"]["departures"] == []
    deps = days["2026-11-25"]["departures"]
    assert len(deps) == 1 and deps[0]["service_date"] == "2026-11-23"
    assert deps[0]["day_offset"] == 2 and deps[0]["departure_clock"] == "00:10:00"


def test_generated_package_is_globally_minimal(client):
    """构造混合出现频率的例子，验证生成结果的记录数等于穷举最优。"""
    files = valid_files(
        calendar="service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                 "start_date,end_date\n"
                 "S12,1,1,1,1,1,1,1,20261123,20261130\n"   # 覆盖第 1、2 个周一
                 "S1,1,1,1,1,1,1,1,20261123,20261123\n"    # 仅第 1 个周一
                 "S123,1,1,1,1,1,1,1,20261123,20261207\n",  # 覆盖 3 个周一
        trips="route_id,service_id,trip_id\n"
              "R1,S12,TA\nR1,S1,TB\nR1,S123,TC\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "TA,08:00:00,08:00:00,S1,1\nTA,08:30:00,08:30:00,S2,2\n"
                   "TA,09:00:00,09:00:00,S3,3\n"
                   "TB,09:00:00,09:00:00,S1,1\nTB,09:30:00,09:30:00,S2,2\n"
                   "TB,10:00:00,10:00:00,S3,3\n"
                   "TC,10:00:00,10:00:00,S1,1\nTC,10:30:00,10:30:00,S2,2\n"
                   "TC,11:00:00,11:00:00,S3,3\n")
    assert upload(client, files).status_code == 201
    pkg = make_pkg(client, stop="S2", start="2026-11-23", days=15).json()
    mon = pkg["weekdays"]["monday"]
    assert [d["date"] for d in mon["days"]] == [
        "2026-11-23", "2026-11-30", "2026-12-07"]

    # 每个 trip 的运营日范围（均为每天运营）
    ranges = {"TA": ("2026-11-23", "2026-11-30"),
              "TB": ("2026-11-23", "2026-11-23"),
              "TC": ("2026-11-23", "2026-12-07")}
    import itertools
    best_total = 0
    for name, group in pkg["weekdays"].items():
        dates = [d["date"] for d in group["days"]]
        m = len(dates)
        presence = {t: {i for i, iso in enumerate(dates) if lo <= iso <= hi}
                    for t, (lo, hi) in ranges.items()}
        best = min(
            sum((1 + (m - len(presence[t]))) if use else len(presence[t])
                for t, use in zip(presence, choice))
            for choice in itertools.product([0, 1], repeat=3))
        best_total += best
    assert pkg["stats"]["total_records"] == best_total, (pkg["stats"], best_total)
    assert pkg["stats"]["is_minimal"] is True

    # 还原结果与直接读库一致：每个周一都有 TA/TC，第 1 个周一多 TB
    days = board.expand_package(pkg)["days"]
    got = {d["date"]: sorted(x["trip_id"] for x in d["departures"])
           for d in days if d["weekday"] == "monday"}
    assert got == {"2026-11-23": ["TA", "TB", "TC"],
                   "2026-11-30": ["TA", "TC"],
                   "2026-12-07": ["TC"]}
    """对随机规模的小例子穷举验证：逐条最优 == 全局最优。"""
    import itertools

    for m in range(1, 6):
        for pattern in itertools.product([0, 1], repeat=4):
            day_sets = []
            for i in range(m):
                day_sets.append({j for j in range(4)
                                 if pattern[j] and (i + j) % 3 != 2})
            # 枚举四条候选发车各自“进/不进模板”的全部组合
            best = None
            for choice in itertools.product([0, 1], repeat=4):
                cost = 0
                for j in range(4):
                    p = sum(1 for s in day_sets if j in s)
                    if choice[j]:
                        cost += 1 + (m - p)
                    else:
                        cost += p
                best = cost if best is None else min(best, cost)
            greedy = sum(
                min(sum(1 for s in day_sets if j in s),
                    1 + m - sum(1 for s in day_sets if j in s))
                for j in range(4))
            assert greedy == best
