"""前端离线校验/还原逻辑与后端的一致性测试。

网页脚本（app/static/index.html 内联 <script>）是站牌断网时唯一的还原实现，
因此必须与 app/board.py 的规则保持一致。这里用 Node 在沙箱中加载页面脚本，
对同一批包比较两边的校验问题与还原结果。缺少 node 时跳过。
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import board  # noqa: E402
from conftest import valid_files, upload  # noqa: E402

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static", "index.html")
NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const code = html.match(/<script>([\s\S]*)<\/script>/)[1];
function el(){return{value:'',innerHTML:'',textContent:'',files:[],dataset:{},
  classList:{add(){},remove(){},contains(){return false}},querySelectorAll(){return[]},
  appendChild(){},click(){},remove(){},addEventListener(){},setAttribute(){}}}
const els = new Map();
const document = {getElementById(id){if(!els.has(id))els.set(id,el());return els.get(id)},
  querySelectorAll(){ return []; }, createElement(){ return el(); },
  body:{appendChild(){}}};
const sandbox = {document, window:{addEventListener(){}}, navigator:{onLine:true},
  fetch:()=>{throw new Error('no network')}, console, setTimeout, JSON, Date, Number,
  String, Array, Object, Map, Set, Math, RegExp, parseInt, isNaN};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const input = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = input.packages.map(pkg => {
  const problems = sandbox.validatePackage(pkg)
    .map(x => [x.path, x.severity]).sort();
  let expanded = null;
  try { expanded = sandbox.expandPackage(pkg); } catch (e) { expanded = null; }
  return {problems, expanded};
});
fs.writeFileSync(process.argv[4], JSON.stringify(out));
"""


def _node_run(tmp_path, packages):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps({"packages": packages}, ensure_ascii=False),
                   encoding="utf-8")
    out = tmp_path / "out.json"
    subprocess.run([NODE, str(harness), STATIC, str(inp), str(out)],
                   check=True, capture_output=True, timeout=120)
    return json.loads(out.read_text(encoding="utf-8"))


def _py_signature(pkg):
    problems = sorted([p["path"], p["severity"]]
                      for p in board.validate_package(pkg))
    try:
        expanded = board.expand_package(pkg)
    except board.PackageError:
        expanded = None
    return problems, expanded


@pytest.mark.skipif(NODE is None, reason="node 未安装，跳过前端一致性测试")
def test_js_matches_python(client, tmp_path):
    upload(client, valid_files())
    vid = client.get("/api/versions/current").json()["id"]

    packages = []
    for stop in ("S1", "S2", "S3", "S4"):
        for start, days in (("2026-11-23", 1), ("2026-11-23", 7),
                            ("2026-11-01", 31), ("2026-02-23", 10)):
            r = client.get("/api/board/package", params={
                "version_id": vid, "stop_id": stop,
                "start_date": start, "days": days})
            assert r.status_code == 200
            packages.append(r.json())

    import copy
    good = packages[0]
    corrupt = []
    for mutate in (
        lambda p: p.update(schema_version=99),
        lambda p: p["weekdays"]["monday"]["template"].append({"route_id": "R1"}),
        lambda p: p["weekdays"]["monday"]["days"][0].update(date="2026-01-01"),
        lambda p: p["trips"].pop("T1", None),
        lambda p: p.update(stop={}),
        lambda p: p.pop("source_version", None),
        lambda p: p["stats"].update(total_records=999),
        lambda p: p["weekdays"]["monday"]["days"][0].update(remove=[{
            "route_id": "R9", "trip_id": "ZZ", "stop_sequence": 1,
            "departure_clock": "01:00:00", "day_offset": 0}]),
        lambda p: p["weekdays"]["monday"]["days"].pop(),
    ):
        bad = copy.deepcopy(good)
        mutate(bad)
        corrupt.append(bad)

    allpkgs = packages + corrupt
    js = _node_run(tmp_path, allpkgs)
    assert len(js) == len(allpkgs)
    for i, pkg in enumerate(allpkgs):
        py_problems, py_expanded = _py_signature(pkg)
        assert js[i]["problems"] == py_problems, f"package {i} problems differ"
        if py_expanded is None:
            assert js[i]["expanded"] is None, f"package {i}: JS should reject"
        else:
            assert js[i]["expanded"] is not None, f"package {i}: JS should restore"
            assert js[i]["expanded"] == py_expanded, f"package {i} expand differs"
