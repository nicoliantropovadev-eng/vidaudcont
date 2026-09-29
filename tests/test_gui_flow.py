"""End-to-end through the window: add file -> analyse (worker thread) -> edit timecodes -> cut -> verified."""
import os
import shutil
import threading
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QStandardPaths  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

from vidaudcont import resources  # noqa: E402


def fresh_app_state():
    """Qt test mode keeps its own settings folder between runs: start every test from an empty one."""
    QStandardPaths.setTestModeEnabled(True)
    QSettings("VidAudCont", "VidAudCont").clear()
    d = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    for name in os.listdir(d) if os.path.isdir(d) else []:
        if name.startswith(("session.json", "titles.json", "seen_files.json")):  # with spare and put-aside copies
            os.remove(os.path.join(d, name))


def wait(app, cond, timeout=600):
    t = time.time()
    while not cond():
        app.processEvents()
        time.sleep(0.05)
        assert time.time() - t < timeout, "timeout"


@pytest.mark.skipif(not os.path.isdir(resources.models_dir()), reason="models not downloaded")
def test_analyse_and_cut_in_window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    fresh_app_state()  # keep the user's session/settings untouched
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"error dialog: {a[2] if len(a) > 2 else a}"))
    from vidaudcont.gui.main_window import MainWindow
    src = str(tmp_path / "клип.m4a")
    shutil.copy(resources.asset("selftest.m4a"), src)
    w = MainWindow()
    w.add_paths([src])
    assert w.table.rowCount() == 1
    w.table.selectRow(0)
    fid = w.selected_fid()
    w.analyze_current()
    wait(app, lambda: w.items[fid]["result"] is not None or w.items[fid]["status"] == "ошибка")
    res = w.items[fid]["result"]
    assert res and res["timecodes"].startswith("start-0:0")
    assert w.tc_edit.toPlainText() == res["timecodes"]
    w.tc_edit.setPlainText("start-0:05, 0:30-0:40")  # user edits the cut list
    assert w.items[fid]["text"] == "start-0:05, 0:30-0:40"
    w.cut_current()
    wait(app, lambda: w.items[fid]["cut"] is not None or w.items[fid]["status"] == "ошибка")
    cut = w.items[fid]["cut"]
    assert cut["verification"]["lossless"]
    assert abs(cut["output_duration"] - (76.0 - 5 - 10)) < 0.3
    assert "Без перекодирования" in w.cut_info.text()
    w.close()


@pytest.mark.skipif(not os.path.isdir(resources.models_dir()), reason="models not downloaded")
def test_sheet_rows_cut_all_and_write_back(tmp_path, monkeypatch):
    """Files named like the downloader names them -> rows found -> 'Cut all' -> "<row>.m4a" + column D filled.
    A write Google keeps refusing is completed by the next 'Cut all' without cutting again; a file without a row
    is left alone instead of being saved as "name_cut"."""
    from vidaudcont import matching
    from vidaudcont import sheet as sheet_mod

    from .mock_sheet import MockSheet
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"error dialog: {a[2] if len(a) > 2 else a}"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    titles = {"AAAAAAAAAA1": ("Clinical talk one", "Клинический разговор один"),
              "BBBBBBBBBB2": ("Clinical talk two", "Клинический разговор два"),
              "CCCCCCCCCC3": ("Clinical talk three", "Клинический разговор три")}
    monkeypatch.setattr(matching, "original_title", lambda vid: titles[vid][0])
    monkeypatch.setattr(matching, "localized_title", lambda vid, lang="ru": titles[vid][1])
    monkeypatch.setattr(sheet_mod, "RETRY_PAUSE", 0.01)
    grid = [["", "", "Link", "", ""]] * 6 + [
        ["", "", "https://www.youtube.com/watch?v=AAAAAAAAAA1", "", ""],                     # row 7: empty D
        ["", "", "https://www.youtube.com/watch?v=BBBBBBBBBB2", "start-0:05", "моя пометка"],  # row 8: filled by hand
        ["", "", "https://youtu.be/CCCCCCCCCC3", "", ""]]                                     # row 9
    sheet = MockSheet([list(r) for r in grid], key="k")
    try:
        from vidaudcont.gui.main_window import MainWindow
        src_dir = tmp_path / "Загрузки"
        src_dir.mkdir()
        names = {"Клинический разговор один.m4a": 7, "Clinical talk two.m4a": 8, "Клинический разговор три.m4a": 9}
        for n in names:
            shutil.copy(resources.asset("selftest.m4a"), src_dir / n)
        w = MainWindow()
        w.settings.only_suitable = False  # the self-test clip is read speech, not a conversation
        w.sheet_cfg = {"url": sheet.url, "key": "k", "lang": "ru", "write_note": True, "overwrite": False}
        w.add_paths([str(src_dir)])  # adding files starts the row search
        wait(app, lambda: all(it.get("row") for it in w.items.values()))
        assert {os.path.basename(it["path"]): it["row"] for it in w.items.values()} == names
        w.analyze_all()
        wait(app, lambda: all(it["result"] for it in w.items.values()), timeout=900)
        three = next(fid for fid, it in w.items.items() if it["row"] == 9)
        w.items[three]["text"] = "good"  # edited by hand: nothing to cut, only renamed
        stray = str(src_dir / "Неизвестное видео.m4a")  # analysed, but its row is not known
        shutil.copy(resources.asset("selftest.m4a"), stray)
        lost = w._add_item(stray, {"status": "готово", "result": w.items[three]["result"]})
        rowed = [fid for fid in w.items if fid != lost]
        sheet.fail_posts = 5                 # the first write fails all 5 attempts, the others go through
        w.cut_all()
        wait(app, lambda: all(w.items[f].get("cut") for f in rowed) and w.worker.pending == 0, timeout=600)
        out = src_dir / "готово"
        assert sorted(os.listdir(out)) == ["7.m4a", "8.m4a", "9.m4a"]
        assert w.items[lost]["cut"] is None and not os.path.exists(src_dir / "Неизвестное видео_cut.m4a")
        failed = w._unwritten()
        assert len(failed) == 1 and "таблица: ошибка" in w.items[failed[0]]["status"]
        assert sheet.cell(w.items[failed[0]]["row"], 4) == ""
        w.cut_all()                          # writes the missing row, cuts nothing again
        wait(app, lambda: not w._unwritten() and w.net.pending == 0)
        assert "в таблице ✓" in w.items[failed[0]]["status"]
        assert sorted(os.listdir(out)) == ["7.m4a", "8.m4a", "9.m4a"]
        for f in rowed:
            it = w.items[f]
            assert it["cut"]["verification"]["lossless"], it["cut"]
        tc7 = next(it for it in w.items.values() if it.get("row") == 7)["result"]["timecodes"]
        assert sheet.cell(7, 4) == tc7                       # written
        assert sheet.cell(8, 4) == "start-0:05"              # hand-made value kept (overwrite is off)
        assert sheet.cell(8, 5) == "моя пометка"
        assert sheet.cell(9, 4) == "good"
        from vidaudcont.engine.analyzer import suitability
        res7 = next(it for it in w.items.values() if it.get("row") == 7)["result"]
        assert sheet.cell(7, 5) == " + ".join(suitability(res7))  # why it does not fit, if it does not
        assert os.path.getsize(out / "9.m4a") == os.path.getsize(src_dir / "Клинический разговор три.m4a")
        w.close()
    finally:
        sheet.close()


@pytest.mark.skipif(not os.path.isdir(resources.models_dir()), reason="models not downloaded")
def test_watch_folder_picks_up_downloads(tmp_path, monkeypatch):
    """A file finishing in the watched folder is added and analysed without any click."""
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"error dialog: {a[2] if len(a) > 2 else a}"))
    from vidaudcont.downloads import FolderWatcher
    from vidaudcont.gui.main_window import MainWindow
    w = MainWindow()
    w.watch_dir, w.watching, w.auto_analyze = str(tmp_path), True, True
    w.watcher = FolderWatcher(str(tmp_path))
    shutil.copy(resources.asset("selftest.m4a"), tmp_path / "Скачанное видео.m4a")

    def look():  # the folder is looked at in the background
        w._scan_watch()
        wait(app, lambda: not w._scanning, timeout=60)
    look()
    assert not w.items          # first look only records the size
    look()
    assert len(w.items) == 1
    it = next(iter(w.items.values()))
    wait(app, lambda: it["result"] is not None)
    assert it["result"]["timecodes"].startswith("start-0:0")
    look()
    assert len(w.items) == 1    # not added twice
    w.close()


def test_row_search_started_by_itself_retries_quietly(tmp_path, monkeypatch):
    """Google not answering while files arrive: no error window, another try a minute later."""
    from vidaudcont import sheet as sheet_mod

    from .mock_sheet import MockSheet
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"error dialog: {a[2] if len(a) > 2 else a}"))
    monkeypatch.setattr(sheet_mod, "RETRY_PAUSE", 0.01)
    sheet = MockSheet([["", "", "Link"], ["", "", "https://www.youtube.com/watch?v=AAAAAAAAAA1"]], key="k")
    try:
        from vidaudcont.gui.main_window import MainWindow
        w = MainWindow()
        w.sheet_cfg = {"url": sheet.url, "key": "k", "lang": "ru"}
        sheet.fail_gets = 100
        f = tmp_path / "Скачанное видео.m4a"
        shutil.copy(resources.asset("selftest.m4a"), f)
        w.add_paths([str(f)])
        wait(app, lambda: not w._match_queued, timeout=60)
        assert w.match_timer.isActive() and "не отвечает" in w.status_text.text()
        assert sheet.requests == 5           # the first try and 4 repeats
        w.close()
    finally:
        sheet.close()


def test_window_never_waits_for_the_folder_or_the_file_list(tmp_path, monkeypatch):
    """Opening each new download takes a moment (on Windows ~0.1 s): with hundreds of files the window froze
    for a minute. The folder is looked at in the background, and a file already in the list is not opened
    again even when the folder is spelled differently (D:/Загрузки vs D:\\Загрузки)."""
    from vidaudcont import downloads
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    opened = []

    def slow_probe(path):
        if threading.current_thread().name == "watch-folder":  # the window may look at a selected file itself
            opened.append(path)
            time.sleep(0.3)
        return {"duration": 1.0, "audio": [], "video": []}
    monkeypatch.setattr(downloads.cutter, "probe", slow_probe)
    from vidaudcont.downloads import FolderWatcher
    from vidaudcont.gui.main_window import MainWindow
    for i in range(6):
        (tmp_path / f"видео {i}.m4a").write_bytes(b"x" * 100)
    w = MainWindow()
    w.add_paths([str(tmp_path / "видео 0.m4a"), str(tmp_path / "видео 1.m4a")])  # already in the list
    w.watch_dir, w.watching, w.watch_action = str(tmp_path), True, "add"
    w.watcher = FolderWatcher(str(tmp_path) + "/./")
    w._scan_watch()
    wait(app, lambda: not w._scanning)
    t = time.time()
    w._scan_watch()                        # the second look opens the 4 new files: 1.2 s in the background
    assert time.time() - t < 0.2
    wait(app, lambda: not w._scanning)
    assert len(w.items) == 6 and len(opened) == 4
    assert all("видео 0" not in p and "видео 1" not in p for p in opened)

    fid = next(iter(w.items))              # the file list: written once, a few seconds after the changes
    w.items[fid]["status"] = "ошибка"
    session = w._session_file()
    if os.path.exists(session):
        os.remove(session)
    for _ in range(50):
        w._save_session()
    assert not os.path.exists(session) and w.save_timer.isActive()
    w._write_session()
    import json
    saved = json.load(open(session, encoding="utf-8"))
    assert len(saved) == 6 and saved[0]["status"] == "ошибка"  # a failed file is not analysed again on start
    w.close()


def test_file_list_survives_a_restart_and_bad_files(tmp_path, monkeypatch):
    """Closing and opening the window brings the list back; a damaged list is put aside (not overwritten),
    the spare copy is used, and entries whose file is missing are kept for when it comes back."""
    import json

    import numpy as np
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    from vidaudcont.gui.main_window import MainWindow
    here = tmp_path / "есть.m4a"
    shutil.copy(resources.asset("selftest.m4a"), here)
    result = {"duration": 76.0, "speech_ratio": 0.8, "timecodes": "start-0:05", "segments": [], "hints": [],
              "cuts": [{"start": 0.0, "end": 5.0, "reasons": ["x"]}], "accent": None, "topic": None, "transcript": "",
              "speech_db": np.float32(-20.5)}          # a stray numpy number must not stop the saving
    w = MainWindow()
    w.add_paths([str(here)])
    fid = next(iter(w.items))
    w.items[fid].update(result=result, status="готово", row=80, row_how="название")
    w.close()                                            # writes the list
    session = w._session_file()
    w2 = MainWindow()
    assert [(it["row"], it["result"]["timecodes"]) for it in w2.items.values()] == [(80, "start-0:05")]
    w2.close()

    gone = {"path": str(tmp_path / "перенесён.m4a"), "status": "готово", "result": result | {"speech_db": 1.0},
            "text": None, "cut": None, "row": 81, "row_how": ""}
    saved = json.load(open(session, encoding="utf-8"))
    json.dump(saved + [gone], open(session, "w", encoding="utf-8"), ensure_ascii=False)
    w3 = MainWindow()                                    # the missing file is not shown, but kept
    assert len(w3.items) == 1 and len(w3._missing) == 1
    w3._write_session()
    assert [d["row"] for d in json.load(open(session, encoding="utf-8"))] == [80, 81]
    w3.close()

    good = open(session, encoding="utf-8").read()
    open(session + ".bak", "w", encoding="utf-8").write(good)
    open(session, "w", encoding="utf-8").write(good[: len(good) // 2])  # cut off, as by a killed program
    w4 = MainWindow()
    assert len(w4.items) == 1                            # taken from the spare copy
    broken = [n for n in os.listdir(os.path.dirname(session)) if ".broken-" in n]
    assert broken                                        # the damaged list is kept aside for recovery
    w4.close()


def test_cut_all_cuts_only_medical_conversations_with_british_accent(tmp_path, monkeypatch):
    """A lecture (one voice) and an American conversation are not cut: their reason goes to column E.
    A file analysed before the conversation check gets only that check."""
    from .mock_sheet import MockSheet
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"error dialog: {a[2] if len(a) > 2 else a}"))
    asked, told = [], []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    grid = [["", "", "Link", "", ""]] + [["", "", f"https://youtu.be/{c * 11}", "", ""] for c in "ABC"]
    sheet = MockSheet(grid, key="k")
    try:
        from vidaudcont.gui.main_window import MainWindow
        base = {"duration": 76.0, "speech_ratio": 0.8, "timecodes": "start-0:05", "segments": [[5.0, 30.0]], "hints": [],
                "cuts": [{"start": 0.0, "end": 5.0, "reasons": ["x"]}], "transcript": "", "speech_db": -20.0,
                "topic": {"medical": True, "top_terms": ["pain"]}}
        british = {"top": "британский", "groups": {"британский": 1.0}}
        talk = {"conversation": True, "separation": 0.3, "minor_share": 0.4, "turns_per_min": 4.0, "reason": ""}
        lecture = {"conversation": False, "separation": 0.07, "minor_share": 0.3, "turns_per_min": 6.0,
                   "reason": "говорит в основном один человек"}
        files = {"разговор.m4a": (2, dict(base, accent=british, dialogue=talk)),
                 "лекция.m4a": (3, dict(base, accent=british, dialogue=lecture)),
                 "американцы.m4a": (4, dict(base, accent={"top": "американский", "groups": {"американский": 1.0}},
                                            dialogue=talk))}
        w = MainWindow()
        w.settings.words = ""  # made-up results: no transcription to look for words in
        w.sheet_cfg = {"url": sheet.url, "key": "k", "lang": "ru", "write_note": True}
        for name, (row, res) in files.items():
            shutil.copy(resources.asset("selftest.m4a"), tmp_path / name)
            fid = w._add_item(str(tmp_path / name), {"status": "готово", "result": res, "row": row, "row_how": "вручную"})
        w.table.selectRow(0)
        assert [w.table.item(r, 7).text() for r in range(3)] == ["✓ да", "✗ не разговор", "✗ американский акцент"]
        w.cut_all()
        wait(app, lambda: w.worker.pending == 0 and w.net.pending == 0 and w.pool.pending == 0
             and all(it.get("cut") or it.get("noted") for it in w.items.values()), timeout=300)
        assert "Не подходят (2)" in asked[0]
        assert os.listdir(tmp_path / "готово") == ["2.m4a"]
        assert sheet.cell(2, 4) == "start-0:05" and sheet.cell(2, 5) == ""
        assert sheet.cell(3, 5) == "НЕ РАЗГОВОР" and sheet.cell(4, 5) == "АМЕРИКАНСКИЙ АКЦЕНТ"
        asked.clear()
        w.cut_all()                      # nothing left: the reasons are not written again
        assert not asked and told

        old = str(tmp_path / "старый анализ.m4a")    # analysed by an older version: only the new check runs
        shutil.copy(resources.asset("selftest.m4a"), old)
        res = dict(base, accent=british, segments=[[5.0, 30.0], [40.0, 55.0], [60.0, 72.0]])
        fid = w._add_item(old, {"status": "готово", "result": res})
        w._check_dialogues()
        wait(app, lambda: "dialogue" in w.items[fid]["result"], timeout=300)
        assert "separation" in w.items[fid]["result"]["dialogue"]
        w.close()
    finally:
        sheet.close()


def test_without_the_conversation_check_accent_and_topic_still_decide(tmp_path, monkeypatch):
    """The conversation check turned off: an American recording is still not cut and gets its note in column E,
    a British lecture is cut. Turned on again, files analysed without it get only that check."""
    from .mock_sheet import MockSheet
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"error dialog: {a[2] if len(a) > 2 else a}"))
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    grid = [["", "", "Link", "", ""]] + [["", "", f"https://youtu.be/{c * 11}", "", ""] for c in "ABC"]
    sheet = MockSheet(grid, key="k")
    try:
        from vidaudcont.gui.main_window import MainWindow
        base = {"duration": 76.0, "speech_ratio": 0.8, "timecodes": "start-0:05", "segments": [[5.0, 30.0]], "hints": [],
                "cuts": [{"start": 0.0, "end": 5.0, "reasons": ["x"]}], "transcript": "", "speech_db": -20.0,
                "topic": {"medical": True, "top_terms": ["pain"]}}
        british = {"top": "британский", "groups": {"британский": 1.0}}
        lecture = {"conversation": False, "separation": 0.07, "minor_share": 0.3, "turns_per_min": 6.0,
                   "reason": "говорит в основном один человек"}
        files = {"без проверки.m4a": (2, dict(base, accent=british, dialogue=None)),
                 "лекция.m4a": (3, dict(base, accent=british, dialogue=lecture)),
                 "американцы.m4a": (4, dict(base, accent={"top": "американский", "groups": {"американский": 1.0}},
                                            dialogue=None))}
        w = MainWindow()
        w.settings.dialogue = False
        w.settings.words = ""
        w.sheet_cfg = {"url": sheet.url, "key": "k", "lang": "ru", "write_note": True}
        fids = {}
        for name, (row, res) in files.items():
            shutil.copy(resources.asset("selftest.m4a"), tmp_path / name)
            fids[name] = w._add_item(str(tmp_path / name), {"status": "готово", "result": res, "row": row,
                                                            "row_how": "вручную"})
        assert [w.table.item(r, 7).text() for r in range(3)] == ["✓ да", "✓ да", "✗ американский акцент"]
        w.cut_all()
        wait(app, lambda: w.worker.pending == 0 and w.net.pending == 0
             and all(it.get("cut") or it.get("noted") for it in w.items.values()), timeout=300)
        assert "Не подходят (1)" in asked[0] and "проверяются" not in asked[0]
        assert sorted(os.listdir(tmp_path / "готово")) == ["2.m4a", "3.m4a"]
        assert sheet.cell(2, 5) == "" and sheet.cell(3, 5) == "" and sheet.cell(4, 5) == "АМЕРИКАНСКИЙ АКЦЕНТ"

        added = []
        monkeypatch.setattr(w.pool, "add", lambda fid, path, settings, kind="analyze", extra=None:
                            added.append((fid, kind)))
        w.settings.dialogue = True
        w._check_dialogues()
        assert sorted(added) == sorted((fids[n], "dialogue") for n in ("без проверки.m4a", "американцы.m4a"))
        w.close()
    finally:
        sheet.close()


@pytest.mark.skipif(not os.path.isdir(resources.models_dir()), reason="models not downloaded")
def test_phrases_with_listed_words_are_cut_and_found_again_when_the_list_changes(tmp_path, monkeypatch):
    """The analysis cuts the sentences with a listed word and keeps the transcript; another list finds the words again
    in the kept word times at once; a file analysed before the words were looked for waits for its transcription."""
    from vidaudcont.engine.analyzer import Settings
    from vidaudcont.gui.main_window import MainWindow, SettingsDialog
    from vidaudcont.timecodes import format_cuts
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"error dialog: {a[2] if len(a) > 2 else a}"))
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    dlg = SettingsDialog(Settings(words="rainbow, exam"), {})
    assert dlg.widgets["words"].text() == "rainbow, exam"
    dlg.widgets["words"].setText(" chillies ")
    assert dlg.values()[0].words == "chillies"
    dlg._reset()
    assert dlg.values()[0].words == Settings().words

    src = str(tmp_path / "клип.m4a")
    shutil.copy(resources.asset("selftest.m4a"), src)
    w = MainWindow()
    w.settings.words = "rainbow"
    w.add_paths([src])
    fid = next(iter(w.items))
    w._queue_analysis(fid)
    wait(app, lambda: w.items[fid]["result"] is not None or w.items[fid]["status"] == "ошибка")
    it = w.items[fid]
    res = it["result"]
    assert "full_text" not in res and "word_times" not in res          # kept in their own files, not in the list
    assert "rainbow" in it["full_text"] and os.path.exists(w._words_file(src))
    [found] = res["words"]["found"]
    assert found["word"] == "rainbow" and found["end"] - found["start"] <= 8
    assert any(c["start"] <= found["start"] and found["end"] <= c["end"] and "фраза со словом «rainbow»" in c["reasons"]
               for c in res["cuts"])
    assert res["timecodes"] != format_cuts([(c["start"], c["end"]) for c in res["base_cuts"]], res["duration"])

    monkeypatch.setattr(w.pool, "add", lambda *a, **k: pytest.fail("no new transcription is needed"))
    w.settings.words = "Airlines"                                       # another list: found in the kept word times
    w._check_words()
    assert [f["word"] for f in res["words"]["found"]] == ["Airlines"] and res["words"]["list"] == ["airlines"]
    w.settings.words = ""                                               # no words: the sound rules alone
    w._check_words()
    assert res["cuts"] == res["base_cuts"] and not w._needs_words(it)

    queued = []
    monkeypatch.setattr(w.pool, "add", lambda fid, path, settings, kind="analyze", extra=None:
                        queued.append((fid, kind, (extra or {}).get("words"))))
    old = str(tmp_path / "раньше.m4a")                                  # analysed by an older version
    shutil.copy(resources.asset("selftest.m4a"), old)
    before = {k: v for k, v in res.items() if k not in ("words", "base_cuts")}
    older = w._add_item(old, {"status": "готово", "result": dict(before, cuts=list(res["base_cuts"])), "row": 5})
    w.settings.words = "rainbow"
    w._check_words()
    assert (older, "transcribe", True) in queued and fid not in [q[0] for q in queued]  # fid: word times kept
    assert w._needs_words(w.items[older]) and not w._needs_words(it)
    w.cut_all()
    assert "Ещё проверяются (1)" in asked[-1]
    w.on_done(older, "transcribe", {"text": it["full_text"], "word_times": w._load_words(it)})
    assert not w._needs_words(w.items[older])
    assert w.items[older]["result"]["timecodes"] == res["timecodes"]
    w.close()


def test_only_the_chosen_rows_are_analysed(tmp_path, monkeypatch):
    """Rows chosen in «4K Video Downloader+»: files of other rows wait (their queued work is taken back), a file without
    a row waits for it, another video row of a chosen video counts; clearing the rows lets every file go on."""
    from vidaudcont.gui.main_window import C_ROW, OUT_OF_ROWS, WAIT_ROW, DownloaderDialog, MainWindow
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    warned, told = [], []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    w = MainWindow()
    queued = []
    monkeypatch.setattr(w.pool, "add", lambda fid, path, settings, kind="analyze", extra=None: queued.append((fid, kind)))

    def take_back(fids):
        back = [q for q in queued if q[0] in fids]
        queued[:] = [q for q in queued if q[0] not in fids]
        return back
    monkeypatch.setattr(w.pool, "remove", take_back)
    w.settings.words = ""
    w.sheet_cfg = {"url": "https://script.google.com/macros/s/x/exec", "key": "k"}
    w.sheet_rows = {r: {"row": r, "id": vid * 11, "link": "", "cuts": "", "note": ""}
                    for r, vid in ((2, "A"), (3, "B"), (4, "C"), (5, "D"), (6, "B"))}   # row 6: the video of row 3
    res = {"duration": 76.0, "speech_ratio": 0.8, "timecodes": "start-0:05", "segments": [], "hints": [],
           "cuts": [{"start": 0.0, "end": 5.0, "reasons": ["x"]}], "transcript": "", "accent": None, "topic": None,
           "dialogue": {"conversation": True}}
    fid = {}
    for name, row in (("a", 2), ("b", 4), ("c", None), ("d", 6)):
        fid[name] = w._add_item(str(tmp_path / f"{name}.m4a"), {"row": row, "row_how": "вручную" if row else ""})
    fid["e"] = w._add_item(str(tmp_path / "e.m4a"), {"status": "готово", "result": dict(res), "row": 5,
                                                     "row_how": "вручную"})
    w.set_work_rows("3-4")
    assert "3-4" in w.windowTitle()
    w.analyze_all()
    status = {n: w.items[f]["status"] for n, f in fid.items()}
    assert sorted(queued) == sorted([(fid["b"], "analyze"), (fid["d"], "analyze")])
    assert status["a"] == OUT_OF_ROWS and status["c"] == WAIT_ROW and status["e"] == "готово"

    w.table.item(w._row_of(fid["c"]), C_ROW).setText("3")          # its row is typed in: it is one of the chosen
    assert (fid["c"], "analyze") in queued

    w.set_work_rows("2")                                             # others chosen: queued work is taken back
    assert queued == [(fid["a"], "analyze")]
    assert all(w.items[fid[n]]["status"] == OUT_OF_ROWS for n in "bcd")
    w.cut_all()                                                      # e (row 5) is analysed but not in the rows
    assert "Вне выбранных строк (2): 1" in told[-1]

    w.set_work_rows("")                                              # every row again
    assert sorted(queued) == sorted((fid[n], "analyze") for n in "abcd")
    w.table.selectRow(w._row_of(fid["b"]))
    w.remove_selected()                                              # taken out of the list: its queued work too
    assert (fid["b"], "analyze") not in queued and fid["b"] not in w._analysis_pending

    dlg = DownloaderDialog(list(w.sheet_rows.values()), set(), "", False, "analyze", w, work_spec="300-420")
    assert dlg.work.text() == "300-420"
    dlg.work.setText("300 до 420")
    dlg.accept()
    assert warned and dlg.result() == 0                              # not accepted: the rows are not understood
    dlg.first.setValue(3)
    dlg.last.setValue(4)
    next(b for b in dlg.findChildren(QPushButton) if b.text() == "= строки выше").click()
    assert dlg.values()[3] == "3-4"
    w.close()


def test_a_file_cut_whole_is_done_quietly(tmp_path, monkeypatch):
    """Timecodes that take the whole recording: no window, no file, the timecodes go to the table, and «Вырезать все»
    does not try the file again."""
    from .mock_sheet import MockSheet
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"a window: {a[2] if len(a) > 2 else a}"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    sheet = MockSheet([["", "", "Link", "", ""], ["", "", "https://youtu.be/AAAAAAAAAAA", "", ""]], key="k")
    try:
        from vidaudcont.gui.main_window import C_CUT, MainWindow
        w = MainWindow()
        w.settings.words, w.settings.only_suitable = "", False
        w.sheet_cfg = {"url": sheet.url, "key": "k", "lang": "ru"}
        src = tmp_path / "всё лишнее.m4a"
        shutil.copy(resources.asset("selftest.m4a"), src)
        res = {"duration": 76.0, "speech_ratio": 0.0, "timecodes": "start-end", "segments": [], "hints": [],
               "cuts": [{"start": 0.0, "end": 76.0, "reasons": ["нет речи"]}], "transcript": "", "accent": None,
               "topic": None, "dialogue": None}
        fid = w._add_item(str(src), {"status": "готово", "result": res, "row": 2, "row_how": "вручную"})
        w.cut_all()
        wait(app, lambda: w.items[fid].get("cut") and w.worker.pending == 0 and w.net.pending == 0, timeout=120)
        it = w.items[fid]
        assert it["status"] == "вырезано целиком, в таблице ✓" and it["cut"]["empty"]
        assert sheet.cell(2, 4) == "start-end"
        assert not (tmp_path / "готово").exists() or not os.listdir(tmp_path / "готово")
        assert w.table.item(w._row_of(fid), C_CUT).text() == "— всё вырезано"
        w.table.selectRow(w._row_of(fid))
        assert "файл не создан" in w.cut_info.text()
        w.cut_all()                                          # nothing left to do: it is not tried again
        assert told and told[-1].startswith("Нет проанализированных файлов")
        assert w._proper_status(it) == "вырезано целиком, в таблице ✓"
        w.close()
    finally:
        sheet.close()


def test_several_files_are_cut_at_once_and_never_share_a_name(tmp_path, monkeypatch):
    """Cuts run several at a time; two files given the same row by hand get "2.m4a" and "2 (2).m4a" instead of the
    second overwriting the first."""
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"a window: {a[2] if len(a) > 2 else a}"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    from vidaudcont.gui.main_window import MainWindow, Worker
    running, peak, lock = [0], [0], threading.Lock()
    real = Worker._cut

    def slow_cut(self, payload, report):  # as if the disk and the table took their time
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        try:
            time.sleep(0.5)
            return real(self, payload, report)
        finally:
            with lock:
                running[0] -= 1
    monkeypatch.setattr(Worker, "_cut", slow_cut)
    w = MainWindow()
    w.settings.words, w.settings.only_suitable = "", False
    res = {"duration": 76.0, "speech_ratio": 0.8, "timecodes": "start-0:05", "segments": [], "hints": [],
           "cuts": [{"start": 0.0, "end": 5.0, "reasons": ["x"]}], "transcript": "", "accent": None, "topic": None,
           "dialogue": None}
    fids = []
    for i, row in enumerate((2, 2, 3, 4, 5)):
        src = tmp_path / f"f{i}.m4a"
        shutil.copy(resources.asset("selftest.m4a"), src)
        fids.append(w._add_item(str(src), {"status": "готово", "result": dict(res), "row": row, "row_how": "вручную"}))
    w.cut_all()
    wait(app, lambda: all(w.items[f].get("cut") for f in fids) and w.worker.pending == 0, timeout=120)
    assert peak[0] > 1 and not w._cut_outputs
    assert sorted(os.listdir(tmp_path / "готово")) == ["2 (2).m4a", "2.m4a", "3.m4a", "4.m4a", "5.m4a"]
    assert all(w.items[f]["cut"]["verification"]["lossless"] for f in fids)
    w.close()


def test_rows_checked_again_by_length_are_corrected_and_reported(tmp_path, monkeypatch):
    """Two videos with the same title: both files went to the first of their rows, and the second file was called a
    repeat. «Найти строки» checks every file again by its length: each gets its own row, nothing is a repeat, and a
    file already cut under the wrong number is listed."""
    from vidaudcont import cutter, matching

    from .mock_sheet import MockSheet
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"a window: {a[2] if len(a) > 2 else a}"))
    shown = []
    monkeypatch.setattr(QMessageBox, "open", lambda self: shown.append(self.text()))
    monkeypatch.setattr(matching, "original_title", lambda vid: "Abdominal examination OSCE guide")
    monkeypatch.setattr(matching, "localized_title", lambda vid, lang="ru": "")
    monkeypatch.setattr(matching, "video_length", lambda vid: {"AAAAAAAAAA1": 30, "BBBBBBBBBB2": 76}[vid])
    grid = [["", "", "Link", "", ""], ["", "", "https://youtu.be/AAAAAAAAAA1", "", ""],
            ["", "", "https://youtu.be/BBBBBBBBBB2", "", ""]]
    sheet = MockSheet(grid, key="k")
    try:
        from vidaudcont.gui.main_window import MainWindow
        long_file = tmp_path / "Abdominal examination OSCE guide.m4a"          # 76 s: the video of row 3
        shutil.copy(resources.asset("selftest.m4a"), long_file)
        short_file = tmp_path / "Abdominal examination OSCE guide (1).m4a"     # 30 s: the video of row 2
        cutter.cut(str(long_file), [(30.0, 76.0)], out_path=str(short_file))
        w = MainWindow()
        w.settings.words = ""
        w.sheet_cfg = {"url": sheet.url, "key": "k", "lang": "ru"}
        fid = {n: w._add_item(str(f), {"row": 2, "row_how": "название"}) for n, f in (("long", long_file),
                                                                                         ("short", short_file))}
        cut_as = tmp_path / "готово" / "2.m4a"
        cut_as.parent.mkdir()
        cut_as.write_bytes(b"x")
        w.items[fid["long"]].update(cut={"output": str(cut_as), "sheet": "записано", "verification": {"lossless": True}})
        w.find_rows()
        wait(app, lambda: not w._match_queued and w.net.pending == 0, timeout=120)
        assert (w.items[fid["long"]]["row"], w.items[fid["long"]]["row_how"]) == (3, "название и длительность")
        assert w.items[fid["short"]]["row"] == 2
        assert not w.items[fid["short"]].get("skip") and not w.items[fid["long"]].get("skip")
        assert "Исправлено строк (не совпала длительность): 1" in w.status_text.text()
        assert shown and "2.m4a" in shown[-1] and "это видео строки 3" in shown[-1]

        import json
        w.items[fid["long"]].update(row=2, row_how="название")
        cache = os.path.join(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation), "titles.json")
        data = json.load(open(cache, encoding="utf-8"))
        data["BBBBBBBBBB2"]["len"] = 500                                      # no video has the file's length now
        json.dump(data, open(cache, "w", encoding="utf-8"))
        w._match_last = 0.0
        w.find_rows()
        wait(app, lambda: not w._match_queued and w.net.pending == 0, timeout=120)
        assert w.items[fid["long"]]["row"] is None
        assert "длительность файла 1:16 не совпадает с видео строки 2 (0:30)" == w.items[fid["long"]]["row_how"]
        w.close()
    finally:
        sheet.close()


def test_files_cut_before_their_background_music_was_found_are_cut_again(tmp_path, monkeypatch):
    """Background music found from the music probability kept with the analysis: a cut file whose timecodes change is
    cut again under its name and its column D replaced; one with music under most of the speech does not fit: its
    old result goes aside and column E says why."""
    from .mock_sheet import MockSheet
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"a window: {a[2] if len(a) > 2 else a}"))
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    grid = [["", "", "Link", "", ""], ["", "", "https://youtu.be/AAAAAAAAAAA", "start-0:05", ""],
            ["", "", "https://youtu.be/BBBBBBBBBBB", "start-0:05", ""]]
    sheet = MockSheet(grid, key="k")
    try:
        from vidaudcont.gui.main_window import MUSIC_ASIDE, MainWindow
        w = MainWindow()
        w.settings.words, w.settings.dialogue = "", False
        w.sheet_cfg = {"url": sheet.url, "key": "k", "lang": "ru", "write_note": True}
        done = tmp_path / "готово"
        done.mkdir()
        fids = {}
        for row, music in ((2, [0.0] * 30 + [0.13] * 30 + [0.0] * 16), (3, [0.2] * 76)):
            src = tmp_path / f"{row}src.m4a"
            shutil.copy(resources.asset("selftest.m4a"), src)
            shutil.copy(resources.asset("selftest.m4a"), done / f"{row}.m4a")      # cut before, music and all
            res = {"duration": 76.0, "speech_ratio": 0.9, "timecodes": "start-0:05", "segments": [[5.0, 76.0]],
                   "hints": [], "cuts": [{"start": 0.0, "end": 5.0, "reasons": ["тишина"]}], "transcript": "",
                   "accent": None, "topic": None, "dialogue": None, "music_per_second": music}
            fids[row] = w._add_item(str(src), {"status": "вырезано ✓, в таблице ✓", "result": res, "row": row,
                                               "row_how": "вручную",
                                               "cut": {"output": str(done / f"{row}.m4a"), "timecodes": "start-0:05",
                                                       "sheet": "записано", "verification": {"lossless": True},
                                                       "output_duration": 71.0, "source_duration": 76.0}})
        assert w._check_music() == (2, 2)
        a, b = w.items[fids[2]], w.items[fids[3]]
        assert a["recut"] and b["recut"] and a["status"] == "нужно перерезать: фоновая музыка"
        assert a["result"]["timecodes"] == "start-0:05, 0:30-1:00"
        w.cut_all()
        assert "перерезать заново (найдена фоновая музыка): 1" in asked[-1]
        wait(app, lambda: a["cut"]["timecodes"] == "start-0:05, 0:30-1:00" and w.worker.pending == 0
             and w.net.pending == 0, timeout=120)
        assert not a.get("recut") and a["cut"]["output"] == str(done / "2.m4a")
        assert abs(a["cut"]["output_duration"] - 41.0) < 0.3
        assert sheet.cell(2, 4) == "start-0:05, 0:30-1:00"                       # the program's own value replaced
        wait(app, lambda: sheet.cell(3, 5) == "МУЗЫКА ПОД РЕЧЬЮ" and w.net.pending == 0, timeout=60)
        assert b["status"] == "не подходит" and b.get("cut") is None
        assert (done / MUSIC_ASIDE / "3.m4a").exists() and not (done / "3.m4a").exists()
        w.close()
    finally:
        sheet.close()


def test_red_rows_and_repeats_are_skipped(tmp_path, monkeypatch):
    """A row marked red is left alone; of two downloads of the same video only one is used; repeated links
    get "ПОВТОР строки N" in column E from the downloader dialog."""
    from vidaudcont import matching

    from .mock_sheet import MockSheet
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"error dialog: {a[2] if len(a) > 2 else a}"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    titles = {"AAAAAAAAAA1": ("Talk one", "Разговор один"), "BBBBBBBBBB2": ("Talk two", "Разговор два")}
    monkeypatch.setattr(matching, "original_title", lambda vid: titles[vid][0])
    monkeypatch.setattr(matching, "localized_title", lambda vid, lang="ru": titles[vid][1])
    grid = [["", "", "Link", "", ""],
            ["", "", "https://youtu.be/AAAAAAAAAA1", "", ""],     # row 2
            ["", "", "https://youtu.be/BBBBBBBBBB2", "", ""],     # row 3: marked red
            ["", "", "https://youtu.be/AAAAAAAAAA1", "", ""]]     # row 4: the video of row 2 again
    sheet = MockSheet(grid, key="k")
    sheet.colors = {3: {"bg": ["#ff0000"], "fg": "#000000"}}
    try:
        from vidaudcont.gui.main_window import C_FIT, DownloaderDialog, MainWindow
        res = {"duration": 76.0, "speech_ratio": 0.8, "timecodes": "start-0:05", "segments": [], "hints": [],
               "cuts": [{"start": 0.0, "end": 5.0, "reasons": ["x"]}], "transcript": "", "accent": None, "topic": None}
        w = MainWindow()
        w.settings.only_suitable = False
        w.settings.words = ""
        w.sheet_cfg = {"url": sheet.url, "key": "k", "lang": "ru"}
        fids = {}
        for name in ("Разговор один.m4a", "Разговор один (1).m4a", "Разговор два.m4a"):
            shutil.copy(resources.asset("selftest.m4a"), tmp_path / name)
            fids[name] = w._add_item(str(tmp_path / name), {"status": "готово", "result": dict(res)})
        w.find_rows()
        wait(app, lambda: not w._match_queued and w.net.pending == 0)
        it = {n: w.items[f] for n, f in fids.items()}
        assert [it[n].get("row") for n in fids] == [2, 2, 3]
        assert it["Разговор один.m4a"].get("skip") is None
        assert "повтор" in it["Разговор один (1).m4a"]["skip"] and "красным" in it["Разговор два.m4a"]["skip"]
        assert "Разговор один.m4a" in it["Разговор один (1).m4a"]["skip"]     # says which file is used
        assert w.table.item(w._row_of(fids["Разговор два.m4a"]), C_FIT).text() == "✗ пропуск: красная строка"
        w.cut_all()
        wait(app, lambda: it["Разговор один.m4a"].get("cut") and w.worker.pending == 0 and w.net.pending == 0)
        assert os.listdir(tmp_path / "готово") == ["2.m4a"]
        assert it["Разговор два.m4a"].get("cut") is None and it["Разговор один (1).m4a"].get("cut") is None

        dlg = DownloaderDialog(list(w.sheet_rows.values()), set(), "", False, False, w)
        assert dlg.links() == []                                   # 2 is in D now, 3 is red, 4 repeats 2
        assert dlg.repeats_to_mark() == [{"row": 4, "note": "ПОВТОР строки 2"}]
        old = DownloaderDialog(list(w.sheet_rows.values()), set(), "", False, False, w, script_version=1)
        assert any("старый" in lbl.text() for lbl in old.findChildren(type(old.count)))
        w.close()
    finally:
        sheet.close()


def test_the_same_file_twice_in_the_list_becomes_one(tmp_path, monkeypatch):
    """A file saved twice in the list (older versions could) is restored once, the analysed entry kept;
    a saved entry for a missing file is dropped once the file is back in the list."""
    import json
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    from vidaudcont.gui.main_window import MainWindow
    f = tmp_path / "видео.m4a"
    shutil.copy(resources.asset("selftest.m4a"), f)
    res = {"duration": 76.0, "speech_ratio": 0.8, "timecodes": "good", "segments": [], "hints": [], "cuts": [],
           "transcript": "", "accent": None, "topic": None}
    w = MainWindow()
    session = w._session_file()
    w.close()
    entries = [{"path": str(f), "status": "ожидает анализа", "result": None, "text": None, "cut": None, "row": 7},
               {"path": str(tmp_path) + "/./" + f.name, "status": "готово", "result": res, "text": None, "cut": None,
                "row": 7},
               {"path": str(tmp_path / "позже.m4a"), "status": "готово", "result": res, "text": None, "cut": None}]
    json.dump(entries, open(session, "w", encoding="utf-8"), ensure_ascii=False)
    w = MainWindow()
    assert len(w.items) == 1 and next(iter(w.items.values()))["result"]["timecodes"] == "good"
    assert len(w._missing) == 1
    shutil.copy(resources.asset("selftest.m4a"), tmp_path / "позже.m4a")    # the missing file comes back
    w.add_paths([str(tmp_path / "позже.m4a")])
    w._write_session()
    saved = json.load(open(session, encoding="utf-8"))
    assert sorted(os.path.basename(d["path"]) for d in saved) == ["видео.m4a", "позже.m4a"]
    wait(app, lambda: any(isinstance(c, QMessageBox) and c.isVisible() for c in w.children()), timeout=10)
    app.processEvents()               # the notice about missing files is shown without stopping the program
    w.close()


class FakeClipboard:
    """The real clipboard hangs on CI machines without a desktop."""
    def __init__(self):
        self.value = ""

    def setText(self, text):
        self.value = text

    def text(self):
        return self.value


def test_sheet_dialog_copies_the_connection_to_another_computer(monkeypatch):
    app = QApplication.instance() or QApplication([])
    from vidaudcont.gui import main_window
    from vidaudcont.gui.main_window import SheetDialog
    board = FakeClipboard()
    monkeypatch.setattr(main_window, "clipboard", lambda: board)
    first = SheetDialog({"url": "https://script.google.com/macros/s/AKfy/exec", "key": "secret1", "cuts_col": "F"})
    first._export()
    second = SheetDialog({})                       # a new computer: its own new key...
    assert second.cfg["key"] != "secret1"
    second._import()                               # ...replaced by the one the script in the table knows
    v = second.values()
    assert (v["url"], v["key"], v["cuts_col"]) == ("https://script.google.com/macros/s/AKfy/exec", "secret1", "F")
    board.setText("что-то другое")
    second._import()
    assert "не подключение" in second.ping_result.text()
    app.processEvents()


@pytest.mark.skipif(not os.path.isdir(resources.models_dir()), reason="models not downloaded")
def test_every_video_is_transcribed_into_its_own_sheet(tmp_path, monkeypatch):
    """With a sheet for transcripts set, every file with a row is transcribed — fitting or not, red rows too, a second
    copy of a video not — and row N of that sheet gets the link (A) and the text (B)."""
    from .mock_sheet import MockSheet
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"error dialog: {a[2] if len(a) > 2 else a}"))
    grid = [["", "", "Link", "", ""], ["", "", "https://youtu.be/AAAAAAAAAA1&list=X", "", ""],
            ["", "", "https://youtu.be/BBBBBBBBBB2", "", "НЕ РАЗГОВОР"]]
    texts = []
    sheet = MockSheet(grid, key="k", sheets={"Расшифровки": texts})
    sheet.version = 2  # an older script in the table: texts go to the rows of the links
    try:
        from vidaudcont.gui.main_window import C_TEXT, MainWindow
        w = MainWindow()
        w.sheet_cfg = {"url": sheet.url, "key": "k", "text_sheet": "Расшифровки"}
        w.sheet_rows = {r["row"]: r for r in [{"row": 2, "id": "AAAAAAAAAA1", "link": grid[1][2]},
                                              {"row": 3, "id": "BBBBBBBBBB2", "link": grid[2][2]}]}
        fids = []
        for name, row, skip in (("один.m4a", 2, None), ("два.m4a", 3, "строка отмечена красным"),
                                ("один (1).m4a", 2, "повтор: это же видео (строка 2) — файл один.m4a")):
            shutil.copy(resources.asset("selftest.m4a"), tmp_path / name)
            fids.append(w._add_item(str(tmp_path / name), {"row": row, "status": "готово", "skip": skip}))
        w._check_texts()
        assert w._text_pending == {fids[0], fids[1]}                      # not the second copy
        wait(app, lambda: not w._text_pending, timeout=300)
        w._flush_texts()
        wait(app, lambda: w.items[fids[0]].get("text_written") and w.items[fids[1]].get("text_written"), timeout=60)
        assert sheet.cell(2, 1, "Расшифровки") == "https://www.youtube.com/watch?v=AAAAAAAAAA1"
        assert "carrot" in sheet.cell(2, 2, "Расшифровки").lower()          # the self-test clip is read speech
        assert sheet.cell(3, 1, "Расшифровки").endswith("BBBBBBBBBB2") and sheet.cell(3, 2, "Расшифровки")
        assert w.items[fids[2]].get("full_text") is None
        assert w.table.item(w._row_of(fids[0]), C_TEXT).text() == "✓ в таблице"
        assert w.items[fids[0]]["status"] == "готово"          # the transcription does not take over the status
        assert sheet.cell(2, 4) == ""                                     # the main sheet is left alone
        from vidaudcont.gui import main_window
        monkeypatch.setattr(main_window, "CELL_MAX", 40)                  # a long text goes on in C, D, …
        w.items[fids[0]]["text_written"] = False
        w._flush_texts()
        wait(app, lambda: w.items[fids[0]].get("text_written") and w.net.pending == 0, timeout=60)
        full = w.items[fids[0]]["full_text"]
        cells = [sheet.cell(2, c, "Расшифровки") for c in range(2, 2 + (len(full) + 39) // 40)]
        assert "".join(cells) == full and all(len(c) <= 40 for c in cells)
        w.close()
    finally:
        sheet.close()


def test_downloads_folder_gives_each_file_once(tmp_path):
    """A file taken from the downloads folder and then removed from the list is not picked up again."""
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    from vidaudcont.downloads import FolderWatcher
    from vidaudcont.gui.main_window import MainWindow
    w = MainWindow()
    w.auto_analyze = False
    w.watch_dir, w.watching = str(tmp_path), True
    w.watcher = FolderWatcher(str(tmp_path))
    shutil.copy(resources.asset("selftest.m4a"), tmp_path / "готовое видео.m4a")

    def look():
        w._scan_watch()
        wait(app, lambda: not w._scanning, timeout=60)
    look()
    look()
    assert len(w.items) == 1
    w.table.selectRow(0)
    w.remove_selected()                     # removed from the list, the file stays in the folder
    assert not w.items
    w.close()
    w2 = MainWindow()                       # even after a restart
    w2.auto_analyze = False
    w2.watch_dir, w2.watching = str(tmp_path), True
    w2.watcher = FolderWatcher(str(tmp_path))
    for _ in range(2):
        w2._scan_watch()
        wait(app, lambda: not w2._scanning, timeout=60)
    assert not w2.items
    w2.add_paths([str(tmp_path / "готовое видео.m4a")])   # adding it by hand still works
    assert len(w2.items) == 1
    w2.close()


def test_name_check_dialog_renames_and_keeps_the_list_right(tmp_path, monkeypatch):
    import subprocess

    from vidaudcont import matching

    from .mock_sheet import MockSheet
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(matching, "original_title", lambda vid: {"AAAAAAAAAA1": "Taking a history",
                                                                 "BBBBBBBBBB2": "7 Tips for bad news"}[vid])
    monkeypatch.setattr(matching, "localized_title", lambda vid, lang="ru": "")
    grid = [["", "", "Link", "", ""], ["", "", "https://youtu.be/AAAAAAAAAA1", "", ""],
            ["", "", "https://youtu.be/BBBBBBBBBB2", "", ""]]
    sheet = MockSheet(grid, key="k")
    try:
        from vidaudcont.gui.main_window import MainWindow, NameCheckDialog
        done = tmp_path / "готово"
        done.mkdir()
        for name, title in (("3.m4a", "Taking a history"), ("7.m4a", "7 Tips for bad news")):
            subprocess.run([resources.tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", "-c:a",
                            "aac", "-metadata", f"title={title}", str(done / name)], check=True)
        w = MainWindow()
        w.sheet_cfg = {"url": sheet.url, "key": "k", "lang": "ru"}
        src = tmp_path / "Taking a history.m4a"
        shutil.copy(done / "3.m4a", src)
        fid = w._add_item(str(src), {"status": "вырезано ✓", "row": 3, "cut": {"output": str(done / "3.m4a")},
                                     "result": {"duration": 1.0, "speech_ratio": 1.0, "timecodes": "good", "cuts": [],
                                                "segments": [], "hints": [], "transcript": ""}})
        dlg = NameCheckDialog(w, str(done))
        w._names_dialog = dlg
        dlg._check()
        wait(app, lambda: dlg.b_check.isEnabled(), timeout=60)
        assert "Нужно переименовать: 2" in dlg.report.toPlainText(), dlg.report.toPlainText()
        dlg._rename()
        assert sorted(os.listdir(done)) == ["2.m4a", "3.m4a"]
        assert w.items[fid]["cut"]["output"] == str(done / "2.m4a")      # the list follows the file
        w._names_dialog = None
        w.close()
    finally:
        sheet.close()


@pytest.mark.skipif(not os.path.isdir(resources.models_dir()), reason="models not downloaded")
def test_transcription_of_chosen_rows(tmp_path, monkeypatch):
    """«Расшифровка видео…»: chosen rows, files already at hand added from a folder (only transcribed), links for the
    videos still missing, and downloads into the transcription folder only transcribed; the text of a video standing
    in two rows goes to both rows of the transcripts sheet."""
    from vidaudcont import matching

    from .mock_sheet import MockSheet
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"error dialog: {a[2] if len(a) > 2 else a}"))
    names = {"AAAAAAAAAA1": "Talk one", "BBBBBBBBBB2": "Talk two", "CCCCCCCCCC3": "Talk three", "DDDDDDDDDD4": "Talk four"}
    monkeypatch.setattr(matching, "original_title", lambda vid: names[vid])
    monkeypatch.setattr(matching, "localized_title", lambda vid, lang="ru": "")
    grid = [["", "", "Link", "", ""]] + [["", "", f"https://youtu.be/{v}", "", ""] for v in
                                        ("AAAAAAAAAA1", "BBBBBBBBBB2", "AAAAAAAAAA1", "CCCCCCCCCC3", "DDDDDDDDDD4")]
    texts = [[], [], ["https://www.youtube.com/watch?v=BBBBBBBBBB2", "уже есть"]]   # row 3 has its text already
    sheet = MockSheet(grid, key="k", sheets={"Расшифровки": texts})
    try:
        from vidaudcont.gui.main_window import MainWindow, TranscriptionDialog
        w = MainWindow()
        w.sheet_cfg = {"url": sheet.url, "key": "k", "lang": "ru", "text_sheet": "Расшифровки"}
        w._after_fresh_rows(lambda: w._after_text_rows(lambda: None))
        wait(app, lambda: w.text_done_rows == {"BBBBBBBBBB2"} and w.net.pending == 0)
        have = tmp_path / "старые файлы"
        have.mkdir()
        shutil.copy(resources.asset("selftest.m4a"), have / "Talk three.m4a")      # row 5 is at hand
        dlg = TranscriptionDialog(w, list(w.sheet_rows.values()), w.text_done_rows, w._have_ids(), "Расшифровки",
                                  "2-5", str(tmp_path / "для расшифровки"), True)
        w._downloader_dialog = dlg
        assert [r for r, _ in dlg.links()] == [2, 5]          # 3 has a text, 4 repeats 2, 6 is not chosen
        dlg.spec.setText("2-x")
        assert "не понимаю" in dlg.summary.text() and dlg.links() == []
        dlg.spec.setText("2-5")
        dlg.window.add_text_files(str(have))                   # "Добавить папку с аудио…"
        wait(app, lambda: [r for r, _ in dlg.links()] == [2], timeout=60)
        assert "youtube.com/watch?v=AAAAAAAAAA1" in dlg.links_box.toPlainText()
        w._downloader_dialog = None
        w.text_spec, w.text_dir, w.text_watching = dlg.values()
        os.makedirs(w.text_dir)
        from vidaudcont.downloads import FolderWatcher
        w.text_watcher = FolderWatcher(w.text_dir)
        shutil.copy(resources.asset("selftest.m4a"), os.path.join(w.text_dir, "Talk one.m4a"))  # downloaded
        for _ in range(2):
            w._scan_watch()
            wait(app, lambda: not w._scanning, timeout=60)
        w._check_texts()
        items = {os.path.basename(it["path"]): it for it in w.items.values()}
        assert set(items) == {"Talk three.m4a", "Talk one.m4a"}
        assert all(it.get("text_only") and it["status"] == "только расшифровка" for it in items.values())
        wait(app, lambda: all(it.get("full_text") is not None for it in items.values()), timeout=300)
        assert all(it["status"] == "расшифровано" for it in items.values())
        assert all(it["result"] is None for it in items.values())            # nothing analysed
        w._flush_texts()
        wait(app, lambda: all(it.get("text_written") for it in items.values()) and w.net.pending == 0, timeout=60)
        by_id = {(line or [""])[0][-11:]: line for line in texts if line}
        assert set(by_id) == {"BBBBBBBBBB2", "AAAAAAAAAA1", "CCCCCCCCCC3"} | ({"Ссылка"} if "Ссылка" in by_id else set())
        assert "carrot" in by_id["AAAAAAAAAA1"][1].lower() and by_id["CCCCCCCCCC3"][1]   # one row per video, one
        assert "DDDDDDDDDD4" not in by_id                                                # after another; 6 not chosen
        w.close()
    finally:
        sheet.close()


def test_statuses_left_by_a_transcription_are_put_right(tmp_path):
    """Version 1.6.0 left "расшифровка 5%" over "вырезано ✓"/"готово": restoring the list repairs it, and a cut file
    is not cut again by «Вырезать все»."""
    import json
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    from vidaudcont.gui.main_window import MainWindow
    res = {"duration": 76.0, "speech_ratio": 0.8, "timecodes": "good", "segments": [], "hints": [], "cuts": [],
           "transcript": "", "accent": None, "topic": None}
    files = []
    for n in ("вырезан.m4a", "готов.m4a", "только текст.m4a"):
        shutil.copy(resources.asset("selftest.m4a"), tmp_path / n)
        files.append(str(tmp_path / n))
    w = MainWindow()
    session = w._session_file()
    w.close()
    json.dump([{"path": files[0], "status": "расшифровка 5%", "result": res, "text": None, "row": 7,
                "cut": {"output": str(tmp_path / "7.m4a"), "verification": {"lossless": True}, "sheet": "записано"}},
               {"path": files[1], "status": "расшифровка 5%", "result": res, "text": None, "cut": None, "row": 8},
               {"path": files[2], "status": "расшифровка 5%", "result": None, "text": None, "cut": None, "row": 9,
                "text_only": True, "full_text": "Hello."}], open(session, "w", encoding="utf-8"), ensure_ascii=False)
    w = MainWindow()
    by = {os.path.basename(it["path"]): it["status"] for it in w.items.values()}
    assert by == {"вырезан.m4a": "вырезано ✓, в таблице ✓", "готов.m4a": "готово", "только текст.m4a": "расшифровано"}
    w.close()


@pytest.mark.skipif(not os.path.isdir(resources.models_dir()), reason="models not downloaded")
def test_transcripts_go_one_after_another(tmp_path, monkeypatch):
    """With the new script, transcripts fill the sheet one after another (a video once, whatever its rows), empty
    rows can be gathered away, and texts are kept in files of their own (the saved list stays small)."""
    import json

    from .mock_sheet import MockSheet
    app = QApplication.instance() or QApplication([])
    fresh_app_state()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"error dialog: {a[2] if len(a) > 2 else a}"))
    grid = [["", "", "Link", "", ""]] + [["", "", f"https://youtu.be/{v}", "", ""]
                                        for v in ("AAAAAAAAAA1", "BBBBBBBBBB2", "AAAAAAAAAA1")]
    texts = [["Ссылка", "Расшифровка"], [], ["https://www.youtube.com/watch?v=CCCCCCCCCC3", "старая"], []]
    sheet = MockSheet(grid, key="k", sheets={"Расшифровки": texts})
    try:
        from vidaudcont.gui.main_window import MainWindow
        w = MainWindow()
        w.sheet_cfg = {"url": sheet.url, "key": "k", "text_sheet": "Расшифровки"}
        w._after_fresh_rows(lambda: None)
        wait(app, lambda: w.sheet_version == 3 and w.net.pending == 0)
        fids = []
        for name, row in (("один.m4a", 2), ("два.m4a", 3)):
            shutil.copy(resources.asset("selftest.m4a"), tmp_path / name)
            fids.append(w._add_item(str(tmp_path / name), {"row": row, "status": "готово"}))
        w._check_texts()
        wait(app, lambda: not w._text_pending, timeout=300)
        w._flush_texts()
        wait(app, lambda: all(w.items[f].get("text_written") for f in fids) and w.net.pending == 0, timeout=60)
        links = [(line or [""])[0] for line in texts]
        assert links[:3] == ["Ссылка", "", "https://www.youtube.com/watch?v=CCCCCCCCCC3"]
        assert links[3:5] == ["https://www.youtube.com/watch?v=AAAAAAAAAA1", "https://www.youtube.com/watch?v=BBBBBBBBBB2"]
        assert "carrot" in texts[3][1].lower() and len(texts) == 5         # row 4 of the main sheet: no second copy
        w.compact_texts()
        wait(app, lambda: w.net.pending == 0)
        assert [(line or [""])[0][-11:] for line in texts] == ["Ссылка", "CCCCCCCCCC3", "AAAAAAAAAA1", "BBBBBBBBBB2"]
        w._write_session()
        saved = json.load(open(w._session_file(), encoding="utf-8"))
        assert all(d.get("full_text") is None and d["has_text"] for d in saved)   # not in the list...
        w.close()
        w2 = MainWindow()                                                         # ...but back after a restart
        assert all("carrot" in it["full_text"].lower() for it in w2.items.values())
        w2.close()
    finally:
        sheet.close()
