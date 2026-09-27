"""End-to-end through the window: add file -> analyse (worker thread) -> edit timecodes -> cut -> verified."""
import os
import shutil
import threading
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QStandardPaths  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from vidaudcont import resources  # noqa: E402


def fresh_app_state():
    """Qt test mode keeps its own settings folder between runs: start every test from an empty one."""
    QStandardPaths.setTestModeEnabled(True)
    QSettings("VidAudCont", "VidAudCont").clear()
    d = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    for name in os.listdir(d) if os.path.isdir(d) else []:
        if name.startswith(("session.json", "titles.json")):  # with the spare and put-aside copies
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
        assert sheet.cell(7, 5) == ""                        # British accent: no note
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
