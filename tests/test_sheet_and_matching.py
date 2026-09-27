import os

import pytest

from vidaudcont import matching
from vidaudcont.sheet import SheetClient, SheetError, col_number, script_code

from .mock_sheet import MockSheet

GRID = [["", "", "Link", "", ""],
        ["", "", "https://www.youtube.com/watch?v=4Y3EGPjhiXE", "start-0:13, 6:35-end", ""],
        ["", "", "https://youtu.be/REJrguhSig4?si=abc", "", ""],
        ["", "", "https://www.youtube.com/watch?v=MK1f4-h5cxs", "", ""],
        ["", "", "https://www.youtube.com/watch?v=XH2tF8oB3cw", "", ""],
        ["", "", "https://www.youtube.com/watch?v=IXVs83Va280", "", ""]]
TITLES = {
    "4Y3EGPjhiXE": {"ok": True, "orig": "How Do You Partner with an Unengaged Patient?",
                    "ru": "Как работать с немотивированным пациентом?"},
    "REJrguhSig4": {"ok": True, "orig": "02a.Physical Exam -Head and Neck -part 1/3",
                    "ru": "02a. Физикальный осмотр — Голова и шея — часть 1/3"},
    "MK1f4-h5cxs": {"ok": True, "orig": "02b.Physical Exam -Head and Neck -part 2/3",
                    "ru": "02b.Physical Exam -Head and Neck -part 2/3"},
    "XH2tF8oB3cw": {"ok": True, "orig": "Case study clinical example: First session with a client with symptoms of social anxiety (CBT model)",
                    "ru": "Клинический пример исследования случая: первый сеанс с клиентом с симптомами социальной тревожности (модель КПТ)"},
    "IXVs83Va280": {"ok": True, "orig": "01.Complete History & Physical Exam -part 4.avi",
                    "ru": "01.Complete History & Physical Exam -part 4.avi"},
}


@pytest.fixture
def sheet():
    s = MockSheet([list(r) for r in GRID], key="k")
    yield s
    s.close()


def test_client_roundtrip(sheet):
    c = SheetClient(sheet.url, "k")
    assert c.ping()["spreadsheet"] == "Mock"
    rows = c.rows()
    assert [r["row"] for r in rows] == [2, 3, 4, 5, 6] and rows[0]["cuts"] == "start-0:13, 6:35-end"
    assert c.write([{"row": 3, "cuts": "good"}, {"row": 4, "cuts": "0:10-0:20", "note": "АМЕРИКАНСКИЙ АКЦЕНТ"}]) == 2
    assert sheet.cell(3, 4) == "good" and sheet.cell(4, 5) == "АМЕРИКАНСКИЙ АКЦЕНТ"
    bad = SheetClient(sheet.url, "wrong")
    with pytest.raises(SheetError, match="ключ"):
        bad.rows()


def test_client_repeats_passing_google_errors(sheet, monkeypatch):
    from vidaudcont import sheet as sheet_mod
    monkeypatch.setattr(sheet_mod, "RETRY_PAUSE", 0.01)
    c = SheetClient(sheet.url, "k")
    sheet.fail_gets = 2                       # Google's "file not found" page twice, then the answer
    assert [r["row"] for r in c.rows()] == [2, 3, 4, 5, 6]
    sheet.fail_posts = 3
    assert c.write([{"row": 3, "cuts": "0:01-0:09"}]) == 1 and sheet.cell(3, 4) == "0:01-0:09"
    sheet.fail_gets = 10
    with pytest.raises(SheetError, match="ошибка 404.*Попыток: 3") as e:
        SheetClient(sheet.url, "k", retries=2).rows()
    assert e.value.transient
    sheet.fail_gets, before = 0, sheet.requests
    with pytest.raises(SheetError, match="ключ"):
        SheetClient(sheet.url, "wrong").rows()
    assert sheet.requests == before + 1       # a wrong key is not a passing error: asked once
    sheet.fail_gets = 10
    with pytest.raises(InterruptedError):
        SheetClient(sheet.url, "k", cancelled=lambda: True).rows()


def test_client_rejects_non_https():
    with pytest.raises(SheetError):
        SheetClient("http://example.com/exec", "k")


def test_columns_and_script():
    assert col_number("C") == 3 and col_number("aa") == 27 and col_number("5") == 5
    code = script_code("KEY123")
    assert "const KEY = 'KEY123';" in code and "__VIDAUDCONT_KEY__" not in code


def test_matching_names_like_the_downloader(sheet, tmp_path):
    rows = [{"row": i + 1, "id": r[2].split("v=")[-1][:11] if "v=" in r[2] else r[2].split("/")[-1][:11]}
            for i, r in enumerate(GRID) if "youtu" in r[2]]
    files = {
        "Как работать с немотивированным пациентом.m4a": 2,                         # translated, "?" dropped
        "02a. Физикальный осмотр — Голова и шея — часть 1 3.m4a": 3,                 # "/" replaced
        "02b.Physical Exam -Head and Neck -part 2 3.m4a": 4,                         # original title
        "Клинический пример исследования случая первый сеанс с клиентом с симп....m4a": 5,  # cut short
        "01.Complete History & Physical Exam -part 4.avi.m4a": 6,                    # starts with a number
        "6. anything.m4a": 6,                                                         # row number prefix
        "Some title [MK1f4-h5cxs].m4a": 4,                                            # id in the name
        "Совсем другое видео.m4a": None,
    }
    paths = [os.path.join(str(tmp_path), f) for f in files]
    got = matching.match_files(paths, rows, TITLES)
    for p, want in zip(paths, files.values()):
        assert got[p][0] == want, (os.path.basename(p), got[p])


def test_title_cache_uses_both_titles(monkeypatch, tmp_path):
    monkeypatch.setattr(matching, "original_title", lambda vid: TITLES[vid]["orig"])
    monkeypatch.setattr(matching, "localized_title", lambda vid, lang="ru": TITLES[vid]["ru"])
    cache = matching.TitleCache(str(tmp_path / "t.json"))
    assert cache.fetch(list(TITLES)) == len(TITLES)
    again = matching.TitleCache(str(tmp_path / "t.json"))
    assert again.fetch(list(TITLES)) == 0  # everything cached on disk
    assert again.data["4Y3EGPjhiXE"]["ru"].startswith("Как работать")
