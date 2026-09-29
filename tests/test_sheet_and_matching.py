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


def test_chunks_for_whisper():
    from vidaudcont.engine.analyzer import speech_chunks_for_asr
    segs = [(0.0, 2.0), (2.5, 10.0), (20.0, 21.0), (30.0, 95.0), (96.0, 96.1), (99.0, 99.2)]
    assert speech_chunks_for_asr(segs) == [(0.0, 10.0), (20.0, 21.0), (30.0, 58.0), (58.0, 86.0), (86.0, 96.1)]


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
        "6.m4a": 6,                                                                   # named after the row
        "6. anything.m4a": None,                          # a number before an unknown name is not a row...
        "4 things to know.m4a": None,                     # ...titles like "7 Tips for…" start with numbers
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


def test_connection_moves_to_another_computer(sheet):
    from vidaudcont.sheet import export_connection, import_connection
    line = export_connection({"url": sheet.url, "key": "k", "sheet": "", "link_col": "C", "overwrite": True})
    assert line.startswith("VIDAUDCONT-CONNECTION:") and "k" not in line.split(":", 1)[1][:2]
    got = import_connection("  " + line[:30] + "\n" + line[30:] + "  ")   # wrapped by a messenger
    assert got == {"url": sheet.url, "key": "k", "link_col": "C"}
    assert SheetClient(got["url"], got["key"]).ping()["ok"]
    for bad in ("", "https://script.google.com/macros/s/x/exec", "VIDAUDCONT-CONNECTION:%%%"):
        with pytest.raises(ValueError):
            import_connection(bad)
    with pytest.raises(SheetError, match="Скопировать подключение"):
        SheetClient(sheet.url, "другой").rows()


def test_column_letters():
    from vidaudcont.gui.main_window import col_letter
    from vidaudcont.sheet import col_number
    assert [col_letter(n) for n in (1, 2, 3, 26, 27, 52)] == ["A", "B", "C", "Z", "AA", "AZ"]
    assert all(col_number(col_letter(n)) == n for n in range(1, 200))


def test_macos_app_brings_its_own_root_certificates(monkeypatch):
    """The Python inside the macOS app does not read the Keychain: without certifi's list the table answered
    "CERTIFICATE_VERIFY_FAILED" on a Mac."""
    import importlib
    import ssl
    import sys

    import vidaudcont
    if sys.platform != "darwin":
        pytest.skip("only macOS")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    importlib.reload(vidaudcont)
    assert os.path.isfile(os.environ["SSL_CERT_FILE"])
    assert ssl.create_default_context().cert_store_stats()["x509_ca"] > 100


def test_video_length_from_youtube_search(monkeypatch):
    assert matching.parse_length("6:46") == 406 and matching.parse_length("1:02:03") == 3723
    assert matching.parse_length("LIVE") is None and matching.parse_length(None) is None
    answer = {"contents": [{"itemSectionRenderer": {"contents": [
        {"videoRenderer": {"videoId": "BBBBBBBBBBB", "lengthText": {"simpleText": "9:00"}}},
        {"videoRenderer": {"videoId": "AAAAAAAAAAA", "lengthText": {"simpleText": "6:46"}}}]}}]}
    monkeypatch.setattr(matching, "_post_json", lambda url, body, timeout=30: answer)
    monkeypatch.undo()  # the real video_length (the test fixture replaces it), with the canned answer again
    monkeypatch.setattr(matching, "_post_json", lambda url, body, timeout=30: answer)
    assert matching.video_length("AAAAAAAAAAA") == 406
    assert matching.video_length("DDDDDDDDDDD") is None


def test_a_title_match_is_checked_against_the_files_length():
    rows = [{"row": 2, "id": "AAAAAAAAAAA"}, {"row": 3, "id": "BBBBBBBBBBB"}, {"row": 4, "id": "CCCCCCCCCCC"}]
    titles = {"AAAAAAAAAAA": {"orig": "Abdominal examination OSCE guide part 1", "ru": ""},
              "BBBBBBBBBBB": {"orig": "Abdominal examination OSCE guide part 2", "ru": ""},
              "CCCCCCCCCCC": {"orig": "Taking a history from a patient with chest pain", "ru": ""}}
    lengths = {"AAAAAAAAAAA": 400, "BBBBBBBBBBB": 612, "CCCCCCCCCCC": 300}
    one, two, chest = ("Abdominal examination OSCE guide part 1.m4a", "Abdominal examination OSCE guide part 2.m4a",
                       "Taking a history from a patient with chest pain.m4a")
    got = matching.match_files([one, two, chest], rows, titles, durations={one: 400.4, two: 399.8, chest: 301.2},
                               lengths=lengths)
    assert got[one] == (2, "название")                         # the title and the length agree
    assert got[two] == (2, "название и длительность")           # "part 2" is as long as part 1: it is part 1's file
    assert got[chest] == (4, "название")
    wrong = matching.match_files([chest], rows, titles, durations={chest: 900.0}, lengths=lengths)[chest]
    assert wrong[0] is None and "не совпадает с видео строки 4" in wrong[1]
    unknown = matching.match_files([chest], rows, titles, durations={chest: 900.0}, lengths={})[chest]
    assert unknown == (4, "название")                           # YouTube did not tell: the title decides
    assert matching.match_files([chest], rows, titles)[chest] == (4, "название")
    assert matching.length_candidates([one], rows, titles) >= {"AAAAAAAAAAA", "BBBBBBBBBBB"}
