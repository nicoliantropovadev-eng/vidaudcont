"""End-to-end through the window: add file -> analyse (worker thread) -> edit timecodes -> cut -> verified."""
import os
import shutil
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
    for name in ("session.json", "titles.json"):
        if os.path.exists(os.path.join(d, name)):
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
    """Files named like the downloader names them -> rows found -> 'Cut all' -> "<row>.m4a" + column D filled."""
    from vidaudcont import matching

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
        w.cut_all()
        wait(app, lambda: all(it.get("cut") for it in w.items.values()), timeout=600)
        out = src_dir / "готово"
        assert sorted(os.listdir(out)) == ["7.m4a", "8.m4a", "9.m4a"]
        for it in w.items.values():
            assert it["cut"]["verification"]["lossless"], it["cut"]
        tc7 = next(it for it in w.items.values() if it["row"] == 7)["result"]["timecodes"]
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
    w._scan_watch()
    assert not w.items          # first look only records the size
    w._scan_watch()
    assert len(w.items) == 1
    it = next(iter(w.items.values()))
    wait(app, lambda: it["result"] is not None)
    assert it["result"]["timecodes"].startswith("start-0:0")
    w._scan_watch()
    assert len(w.items) == 1    # not added twice
    w.close()
