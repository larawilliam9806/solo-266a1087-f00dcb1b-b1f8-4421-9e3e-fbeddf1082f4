"""校验逻辑单元测试（纯标准库路径）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.gtfs import format_gtfs_time, parse_gtfs_time, validate_version
from conftest import valid_files


def err_fields(errors):
    return {(e["file"], e["field"]) for e in errors}


# ---------- 时刻解析 ----------

def test_parse_time_normal():
    assert parse_gtfs_time("08:05:09") == 8 * 3600 + 5 * 60 + 9
    assert parse_gtfs_time("00:00:00") == 0


def test_parse_time_after_midnight():
    assert parse_gtfs_time("24:00:00") == 86400
    assert parse_gtfs_time("25:10:00") == 86400 + 3600 + 600


def test_parse_time_invalid():
    assert parse_gtfs_time("8:5:0") is None
    assert parse_gtfs_time("12:60:00") is None
    assert parse_gtfs_time("abc") is None
    assert parse_gtfs_time("") is None


def test_format_time_roundtrip():
    assert format_gtfs_time(0) == "00:00:00"
    assert format_gtfs_time(90061) == "25:01:01"


# ---------- 整版校验 ----------

def test_valid_version_passes():
    tables, errors, truncated = validate_version(valid_files())
    assert errors == []
    assert not truncated
    assert set(tables) == {"stops", "routes", "trips", "stop_times", "calendar"}
    assert len(tables["stop_times"]) == 11


def test_missing_column():
    tables, errors, _ = validate_version(valid_files(
        stops="stop_id,stop_lat\nS1,31.2\n"))
    assert tables is None
    assert any(e["file"] == "stops" and "缺少必备列" in e["message"] for e in errors)


def test_cross_reference_trip_route():
    tables, errors, _ = validate_version(valid_files(
        trips="route_id,service_id,trip_id\nR9,WKD,T1\n"))
    assert tables is None
    assert ("trips", "route_id") in err_fields(errors)


def test_cross_reference_trip_service():
    _, errors, _ = validate_version(valid_files(
        trips="route_id,service_id,trip_id\nR1,NOSVC,T1\n"))
    assert ("trips", "service_id") in err_fields(errors)


def test_cross_reference_stop_times_trip_and_stop():
    _, errors, _ = validate_version(valid_files(
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T9,08:00:00,08:00:00,SX,1\n"))
    fields = err_fields(errors)
    assert ("stop_times", "trip_id") in fields
    assert ("stop_times", "stop_id") in fields


def test_duplicate_stop_sequence():
    _, errors, _ = validate_version(valid_files(
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:00:00,08:00:00,S1,1\n"
                   "T1,08:10:00,08:12:00,S2,1\n"))
    assert ("stop_times", "stop_sequence") in err_fields(errors)


def test_times_must_increase():
    _, errors, _ = validate_version(valid_files(
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:00:00,08:00:00,S1,1\n"
                   "T1,07:50:00,07:55:00,S2,2\n"))
    assert ("stop_times", "arrival_time") in err_fields(errors)


def test_equal_times_not_increasing():
    # 相邻站时刻相同也算不递增
    _, errors, _ = validate_version(valid_files(
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:00:00,08:00:00,S1,1\n"
                   "T1,08:00:00,08:05:00,S2,2\n"))
    assert ("stop_times", "arrival_time") in err_fields(errors)


def test_departure_before_arrival():
    _, errors, _ = validate_version(valid_files(
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:10:00,08:05:00,S1,1\n"))
    assert ("stop_times", "departure_time") in err_fields(errors)


def test_after_midnight_increasing_ok():
    # 23:55 -> 24:20 跨午夜递增合法
    tables, errors, _ = validate_version(valid_files(
        trips="route_id,service_id,trip_id\nR2,ALL,N1\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "N1,23:55:00,23:58:00,S1,1\n"
                   "N1,24:20:00,24:22:00,S2,2\n"
                   "N1,25:01:00,25:01:00,S3,3\n"))
    assert errors == []
    secs = [r[3] for r in sorted(tables["stop_times"], key=lambda r: r[1])]
    assert secs == sorted(secs)
    assert secs[-1] == 25 * 3600 + 60


def test_after_midnight_decreasing_rejected():
    # 24:10 之后接 23:59：数值上倒退，必须报错（防止把次日时刻写成当日）
    _, errors, _ = validate_version(valid_files(
        trips="route_id,service_id,trip_id\nR2,ALL,N1\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "N1,24:10:00,24:10:00,S1,1\n"
                   "N1,23:59:00,23:59:00,S2,2\n"))
    assert ("stop_times", "arrival_time") in err_fields(errors)


def test_duplicate_ids():
    _, errors, _ = validate_version(valid_files(
        stops="stop_id,stop_name\nS1,甲\nS1,乙\n"))
    assert ("stops", "stop_id") in err_fields(errors)


def test_calendar_bad_flag_and_date():
    _, errors, _ = validate_version(valid_files(
        calendar="service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                 "WKD,1,1,1,1,1,0,2,20261301,20261231\n"))
    fields = err_fields(errors)
    assert ("calendar", "sunday") in fields
    assert ("calendar", "start_date") in fields


def test_trip_without_stop_times():
    _, errors, _ = validate_version(valid_files(
        trips="route_id,service_id,trip_id\nR1,WKD,T1\nR1,WKD,T2\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:00:00,08:00:00,S1,1\n"
                   "T1,08:10:00,08:10:00,S2,2\n"))
    assert ("trips", "trip_id") in err_fields(errors)


def test_error_rows_are_csv_line_numbers():
    _, errors, _ = validate_version(valid_files(
        trips="route_id,service_id,trip_id\n"
              "R1,WKD,T1\n"      # 第 2 行
              "R9,WKD,T2\n"))    # 第 3 行
    row3 = [e for e in errors if e["file"] == "trips" and e["row"] == 3]
    assert row3 and row3[0]["field"] == "route_id"
