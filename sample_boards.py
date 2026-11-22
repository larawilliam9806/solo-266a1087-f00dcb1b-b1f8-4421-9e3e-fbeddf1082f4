#!/usr/bin/env python3
"""生成样例站牌包：发布样例运行图并导出某站连续多天的 JSON。

用法：python3 sample_boards.py [输出目录]
"""
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import board, db  # noqa: E402
from app.gtfs import validate_version  # noqa: E402


def main(out_dir: str) -> int:
    data_dir = ROOT / "sample_data" / "valid"
    files = {name: (data_dir / f"{name}.csv").read_bytes()
             for name in ("stops", "routes", "trips", "stop_times", "calendar")}
    tables, errors, _ = validate_version(files)
    if tables is None:
        print("样例数据校验失败：", errors, file=sys.stderr)
        return 1

    os.environ.setdefault("GTFS_DB_PATH",
                          os.path.join(tempfile.mkdtemp(), "sample.db"))
    db.init_db()
    conn = db.connect()
    try:
        vid = db.publish_version(conn, "样例运行图", tables)
        for stop_id, days in (("S2", 7), ("S3", 3)):
            pkg = board.build_board_package(conn, vid, stop_id,
                                            date(2026, 11, 23), days)
            out = Path(out_dir) / f"board-{stop_id}-2026-11-23-{days}d.json"
            out.write_text(json.dumps(pkg, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            print(f"写出 {out}（来源版号 v{vid}，"
                  f"{pkg['stats']['total_records']} 条模板/增删记录）")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "sample_data")))
