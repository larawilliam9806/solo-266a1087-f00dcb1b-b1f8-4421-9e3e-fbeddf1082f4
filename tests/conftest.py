import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 每个测试用例使用独立数据库文件（db.db_path() 运行时读取环境变量）
@pytest.fixture()
def db_file(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("GTFS_DB_PATH", path)
    yield path


@pytest.fixture()
def client(db_file):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


# ---------------- 样例数据 ----------------

STOPS = """\
stop_id,stop_name,stop_lat,stop_lon
S1,中央车站,31.23,121.47
S2,人民广场,31.24,121.48
S3,火车站,31.25,121.46
S4,机场,31.15,121.81
"""

ROUTES = """\
route_id,route_short_name,route_long_name,route_type
R1,1路,中央车站-机场,3
R2,夜1路,夜班环线,3
"""

CALENDAR = """\
service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
WKD,1,1,1,1,1,0,0,20260101,20261231
ALL,1,1,1,1,1,1,1,20260101,20261231
"""

TRIPS = """\
route_id,service_id,trip_id,trip_headsign,direction_id
R1,WKD,T1,机场方向,0
R1,WKD,T2,中央车站方向,1
R2,ALL,N1,夜班环线,0
"""

STOP_TIMES = """\
trip_id,arrival_time,departure_time,stop_id,stop_sequence
T1,08:00:00,08:00:00,S1,1
T1,08:10:00,08:12:00,S2,2
T1,08:25:00,08:25:00,S3,3
T1,09:00:00,09:00:00,S4,4
T2,10:00:00,10:00:00,S4,1
T2,10:30:00,10:32:00,S3,2
T2,10:45:00,10:45:00,S1,3
N1,23:30:00,23:30:00,S1,1
N1,23:55:00,23:58:00,S2,2
N1,24:20:00,24:22:00,S3,3
N1,24:50:00,24:50:00,S4,4
"""


def valid_files(**overrides):
    files = {
        "stops": STOPS.encode(),
        "routes": ROUTES.encode(),
        "trips": TRIPS.encode(),
        "stop_times": STOP_TIMES.encode(),
        "calendar": CALENDAR.encode(),
    }
    for k, v in overrides.items():
        files[k] = v.encode() if isinstance(v, str) else v
    return files


def upload(client, files, label="v1"):
    return client.post(
        "/api/versions",
        data={"label": label},
        files={name: (f"{name}.csv", content, "text/csv")
               for name, content in files.items()},
    )
