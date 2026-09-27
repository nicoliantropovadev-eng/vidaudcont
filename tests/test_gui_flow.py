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


def wait(app, cond, timeout=600):
    t = time.time()
    while not cond():
        app.processEvents()
        time.sleep(0.05)
        assert time.time() - t < timeout, "timeout"


@pytest.mark.skipif(not os.path.isdir(resources.models_dir()), reason="models not downloaded")
def test_analyse_and_cut_in_window(tmp_path, monkeypatch):
    QStandardPaths.setTestModeEnabled(True)  # keep the user's session/settings untouched
    app = QApplication.instance() or QApplication([])
    QSettings("VidAudCont", "VidAudCont").clear()
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
