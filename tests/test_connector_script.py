"""The Google Apps Script connector, executed in node against a fake sheet (skipped without node)."""
import json
import os
import shutil
import subprocess

import pytest

from vidaudcont import resources

NODE = shutil.which("node")
HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_connector_rows_and_write(tmp_path):
    grid = [["", "", "Link", "", ""],
            ["", "", "https://www.youtube.com/watch?v=4Y3EGPjhiXE", "start-0:13", "АМЕРИКАНСКИЙ АКЦЕНТ"],
            ["1", "TVMariel", "https://youtu.be/MK1f4-h5cxs?si=YR-O_Jz77pPyYIvU", "", ""],
            ["", "", "not a link", "", ""],
            ["", "", "https://www.youtube.com/watch?v=iuAMFrjiKXg&list=PLslYwFi71y1wSTkMKmXb3b0qrQBlQwyFQ", "", ""]]
    colors = {"3,1": {"bg": "#ff0000"}, "5,3": {"fg": "#cc0000"}}
    req = {"grid": grid, "colors": colors, "requests": [
        {"method": "GET", "body": {"key": "bad", "action": "ping"}},
        {"method": "GET", "body": {"key": "k123", "action": "ping"}},
        {"method": "GET", "body": {"key": "k123", "action": "rows"}},
        {"method": "POST", "body": {"key": "k123", "action": "write",
                                    "updates": [{"row": 3, "cuts": "3:10-3:17"}, {"row": 5, "cuts": "good", "note": "X"}]}},
        {"method": "POST", "body": {"key": "k123", "action": "nope"}},
    ]}
    p = tmp_path / "req.json"
    p.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    out = subprocess.run([NODE, os.path.join(HERE, "gs_harness.js"), resources.asset("VidAudCont_connector.gs"), str(p)],
                         capture_output=True, text=True, check=True)
    d = json.loads(out.stdout)
    r = d["responses"]
    assert r[0] == {"ok": False, "error": "wrong key"}
    assert r[1]["ok"] and r[1]["last_row"] == 5 and r[1]["version"] == 3
    rows = r[2]["rows"]
    assert [(x["row"], x["id"]) for x in rows] == [(2, "4Y3EGPjhiXE"), (3, "MK1f4-h5cxs"), (5, "iuAMFrjiKXg")]
    assert rows[0]["cuts"] == "start-0:13" and rows[0]["note"] == "АМЕРИКАНСКИЙ АКЦЕНТ"
    assert (rows[0]["bg"], rows[0]["fg"]) == ([], "#000000")
    assert rows[1]["bg"] == ["#ff0000"] and rows[2]["fg"] == "#cc0000"   # a red row, a red link
    assert r[3] == {"ok": True, "written": 2}
    assert d["grid"][2][3] == "3:10-3:17" and d["grid"][4][3] == "good" and d["grid"][4][4] == "X"
    assert d["formats"]["3,4"] == "@"  # written as plain text
    assert r[4]["ok"] is False


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_connector_puts_transcripts_one_after_another(tmp_path):
    grid = [[], ["", ""], ["https://www.youtube.com/watch?v=AAAAAAAAAA1", "old text"], [], [],
            ["https://youtu.be/BBBBBBBBBB2", "second"]]                   # rows 1-6, with empty rows between
    link = "https://www.youtube.com/watch?v="
    req = {"grid": grid, "requests": [
        {"method": "POST", "body": {"key": "k123", "action": "compact"}},
        {"method": "POST", "body": {"key": "k123", "action": "append_texts", "updates": [
            {"id": "CCCCCCCCCC3", "link": link + "CCCCCCCCCC3", "parts": ["third", "and more"]},
            {"id": "AAAAAAAAAA1", "link": link + "AAAAAAAAAA1", "parts": ["new text"]}]}},
    ]}
    p = tmp_path / "req.json"
    p.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    out = subprocess.run([NODE, os.path.join(HERE, "gs_harness.js"), resources.asset("VidAudCont_connector.gs"), str(p)],
                         capture_output=True, text=True, check=True)
    d = json.loads(out.stdout)
    r = d["responses"]
    assert r[0] == {"ok": True, "rows": 2, "removed": 4}
    assert r[1]["ok"] and r[1]["rows"] == [3, 1]                  # a new video gets the next row, a known one its own
    g = [[c for c in line] for line in d["grid"]]
    assert g[0][:2] == [link + "AAAAAAAAAA1", "new text"]
    assert g[1][:2] == ["https://youtu.be/BBBBBBBBBB2", "second"]
    assert g[2][:3] == [link + "CCCCCCCCCC3", "third", "and more"]
    assert all(not any(line) for line in g[3:])


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_connector_starts_an_empty_sheet_with_a_header(tmp_path):
    req = {"grid": [], "requests": [{"method": "POST", "body": {"key": "k123", "action": "append_texts", "updates": [
        {"id": "AAAAAAAAAA1", "link": "https://www.youtube.com/watch?v=AAAAAAAAAA1", "parts": ["text"]}]}}]}
    p = tmp_path / "req.json"
    p.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    out = subprocess.run([NODE, os.path.join(HERE, "gs_harness.js"), resources.asset("VidAudCont_connector.gs"), str(p)],
                         capture_output=True, text=True, check=True)
    d = json.loads(out.stdout)
    assert d["responses"][0]["rows"] == [2]
    assert d["grid"][0][:2] == ["Ссылка", "Расшифровка"] and d["grid"][1][1] == "text"
