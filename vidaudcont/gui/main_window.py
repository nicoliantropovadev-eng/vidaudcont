"""Main window: file list, analysis results with editable timecodes, separate lossless "Cut" button."""
import csv
import json
import os
import queue
import threading
import traceback
from dataclasses import asdict, fields

from PySide6.QtCore import QSettings, QStandardPaths, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QToolBar, QVBoxLayout, QWidget)

from .. import __version__, cutter
from ..engine.analyzer import Models, Settings, analyze
from ..timecodes import fmt_time, parse

MEDIA_EXT = {".m4a", ".mp3", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aiff", ".aif", ".alac",
             ".ape", ".wv", ".amr", ".ac3", ".eac3", ".mka", ".caf", ".m4b", ".mp2", ".mp4", ".m4v", ".mov", ".mkv",
             ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp", ".3g2", ".ogv",
             ".vob", ".asf", ".dv", ".mxf", ".f4v", ".rm", ".rmvb"}

SETTING_LABELS = {
    "max_gap": ("Вырезать паузы длиннее, с", 1, 60, 0.5),
    "edge_min": ("Вырезать начало/конец без речи от, с", 0, 30, 0.5),
    "music_min": ("Вырезать музыку от, с", 0.5, 30, 0.5),
    "music_thr": ("Чувствительность к музыке (0.1–0.9, меньше = строже)", 0.1, 0.9, 0.05),
    "min_keep": ("Вырезать кусок речи между вырезами короче, с", 0, 60, 1),
    "quiet_enabled": ("Вырезать слишком тихую речь", None, None, None),
    "quiet_abs": ("… тихая речь: громкость ниже, дБ", -60, -10, 1),
    "quiet_rel": ("… и тише остальной записи на, дБ", -30, 0, 1),
    "accent": ("Определять акцент", None, None, None),
    "topic": ("Проверять тему (медицинская ли)", None, None, None),
}


def app_data_dir():
    d = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    os.makedirs(d, exist_ok=True)
    return d


class Worker(QThread):
    """Runs analyses and cuts one after another, away from the UI thread."""
    progress = Signal(int, float, str)
    done = Signal(int, str, object)
    failed = Signal(int, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.jobs = queue.Queue()
        self.cancel_flag = threading.Event()
        self.models = None
        self.pending = 0

    def add(self, fid, kind, payload):
        self.pending += 1
        self.jobs.put((fid, kind, payload))

    def cancel_all(self):
        dropped = []
        try:
            while True:
                dropped.append(self.jobs.get_nowait())
                self.pending -= 1
        except queue.Empty:
            pass
        self.cancel_flag.set()
        return dropped

    def stop(self):
        self.cancel_all()
        self.jobs.put((0, "quit", None))
        self.wait(5000)

    def run(self):
        while True:
            fid, kind, payload = self.jobs.get()
            if kind == "quit":
                return
            self.cancel_flag.clear()
            report = lambda f, t: self.progress.emit(fid, float(f), t)  # noqa: E731
            try:
                if kind == "analyze":
                    if self.models is None:
                        report(0.0, "загрузка моделей (первый раз дольше)")
                        self.models = Models()
                    res = analyze(payload["path"], self.models, payload["settings"], progress=report,
                                  cancelled=self.cancel_flag.is_set)
                else:
                    res = cutter.cut(payload["path"], payload["cuts"], out_path=payload.get("out_path"),
                                     mode=payload["mode"], progress=report, cancelled=self.cancel_flag.is_set)
                self.done.emit(fid, kind, res)
            except InterruptedError:
                self.failed.emit(fid, kind, "остановлено")
            except Exception as e:  # shown to the user, details go to the log
                self.failed.emit(fid, kind, f"{e}\n\n{traceback.format_exc(limit=3)}")
            finally:
                self.pending -= 1


class Timeline(QWidget):
    """Speech (blue), cuts (red) and hint pauses (amber) along the file."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(46)
        self.setMouseTracking(True)
        self.dur, self.segs, self.cuts, self.hints = 0.0, [], [], []

    def set_data(self, dur, segs, cuts, hints):
        self.dur, self.segs, self.cuts, self.hints = dur, segs, cuts, hints
        self.update()

    def _x(self, t):
        return 4 + (self.width() - 8) * (t / self.dur if self.dur else 0)

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(4, 8, w - 8, h - 22, QColor("#eceff1"))
        if self.dur > 0:
            for s, e in self.segs:
                p.fillRect(int(self._x(s)), 14, max(1, int(self._x(e) - self._x(s))), h - 34, QColor("#5c8dd6"))
            for s, e in self.hints:
                p.fillRect(int(self._x(s)), 8, max(1, int(self._x(e) - self._x(s))), h - 22, QColor(255, 179, 0, 140))
            for s, e in self.cuts:
                p.fillRect(int(self._x(s)), 8, max(2, int(self._x(e) - self._x(s))), h - 22, QColor(220, 50, 47, 170))
            p.setPen(QPen(QColor("#607d8b")))
            step = 60 if self.dur <= 900 else (300 if self.dur <= 3600 else 600)
            t = 0
            while t <= self.dur:
                x = int(self._x(t))
                p.drawLine(x, h - 14, x, h - 10)
                p.drawText(x + 2, h - 1, fmt_time(t))
                t += step
        p.end()

    def mouseMoveEvent(self, ev):
        if self.dur > 0:
            t = max(0.0, min(self.dur, (ev.position().x() - 4) / max(1, self.width() - 8) * self.dur))
            self.setToolTip(fmt_time(t))


class SettingsDialog(QDialog):
    def __init__(self, settings, extra, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        form = QFormLayout(self)
        self.widgets = {}
        for f in fields(Settings):
            if f.name not in SETTING_LABELS:
                continue
            label, lo, hi, step = SETTING_LABELS[f.name]
            val = getattr(settings, f.name)
            if isinstance(val, bool):
                w = QCheckBox()
                w.setChecked(val)
            else:
                w = QDoubleSpinBox()
                w.setRange(lo, hi)
                w.setSingleStep(step)
                w.setDecimals(2 if step < 0.5 else 1)
                w.setValue(val)
            self.widgets[f.name] = w
            form.addRow(label, w)
        self.out_dir = QLineEdit(extra.get("out_dir", ""))
        self.out_dir.setPlaceholderText("рядом с исходным файлом")
        browse = QPushButton("…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.out_dir)
        row.addWidget(browse)
        form.addRow("Куда сохранять вырезанное", row)
        self.suffix = QLineEdit(extra.get("suffix", "_cut"))
        form.addRow("Добавка к имени файла", self.suffix)
        reset = QPushButton("Вернуть значения по умолчанию")
        reset.clicked.connect(self._reset)
        form.addRow(reset)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Папка для результатов", self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def _reset(self):
        default = Settings()
        for name, w in self.widgets.items():
            v = getattr(default, name)
            w.setChecked(v) if isinstance(w, QCheckBox) else w.setValue(v)

    def values(self):
        st = Settings()
        for name, w in self.widgets.items():
            setattr(st, name, w.isChecked() if isinstance(w, QCheckBox) else float(w.value()))
        return st, {"out_dir": self.out_dir.text().strip(), "suffix": self.suffix.text() or "_cut"}


COLS = ["Файл", "Длит.", "Статус", "Таймкоды для вырезки", "Акцент", "Тема", "Вырезано"]
COL_WIDTH = [230, 58, 110, 270, 90, 95, 90]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"VidAudCont {__version__} — вырезка пауз, музыки и лишнего")
        self.resize(1320, 820)
        self.setAcceptDrops(True)
        self.items = {}   # fid -> dict(path, status, result, text, cut)
        self.next_id = 1
        self.current = None
        self.qs = QSettings("VidAudCont", "VidAudCont")
        self.settings = Settings(**{k: v for k, v in json.loads(self.qs.value("settings", "{}")).items()
                                    if k in Settings.__dataclass_fields__})
        self.extra = json.loads(self.qs.value("extra", "{}")) or {"out_dir": "", "suffix": "_cut"}

        self.worker = Worker(self)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_done)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

        self._build_ui()
        self._restore_session()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        tb = QToolBar("Главное")
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(text, slot, tip=""):
            a = QAction(text, self)
            a.triggered.connect(slot)
            a.setToolTip(tip or text)
            tb.addAction(a)
            return a

        act("Добавить файлы…", self.add_files_dialog)
        act("Добавить папку…", self.add_folder_dialog)
        act("Убрать из списка", self.remove_selected)
        tb.addSeparator()
        act("Анализировать все", self.analyze_all, "Найти таймкоды для всех файлов без результата")
        act("Остановить", self.stop_all)
        tb.addSeparator()
        act("Экспорт в CSV…", self.export_csv, "Таблица с таймкодами всех файлов (открывается в Excel/Google Таблицах)")
        act("Настройки…", self.open_settings)

        split = QSplitter(Qt.Horizontal)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        for c, wdt in enumerate(COL_WIDTH):
            hh.setSectionResizeMode(c, QHeaderView.Interactive)
            self.table.setColumnWidth(c, wdt)
        hh.setStretchLastSection(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.itemSelectionChanged.connect(self.on_select)
        split.addWidget(self.table)

        right = QWidget()
        rl = QVBoxLayout(right)
        self.title = QLabel("Перетащите сюда аудио или видео файлы, или нажмите «Добавить файлы…»")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size: 15px; font-weight: 600;")
        rl.addWidget(self.title)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        rl.addWidget(self.info)
        self.timeline = Timeline()
        rl.addWidget(self.timeline)
        legend = QLabel('<span style="color:#5c8dd6">■</span> речь &nbsp; <span style="color:#dc322f">■</span> вырезается'
                        ' &nbsp; <span style="color:#ffb300">■</span> паузы 5–7 с (не вырезаются)')
        rl.addWidget(legend)

        box = QGroupBox("Таймкоды для вырезки (можно править вручную)")
        bl = QVBoxLayout(box)
        self.tc_edit = QPlainTextEdit()
        self.tc_edit.setMaximumHeight(90)
        self.tc_edit.setPlaceholderText("например: start-0:13, 3:10-3:17, 6:35-end   (good — вырезать нечего)")
        self.tc_edit.textChanged.connect(self.on_tc_changed)
        bl.addWidget(self.tc_edit)
        row = QHBoxLayout()
        self.tc_status = QLabel("")
        row.addWidget(self.tc_status, 1)
        b_copy = QPushButton("Копировать")
        b_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.tc_edit.toPlainText().strip()))
        row.addWidget(b_copy)
        b_reset = QPushButton("Вернуть найденные")
        b_reset.clicked.connect(self.reset_timecodes)
        row.addWidget(b_reset)
        bl.addLayout(row)
        rl.addWidget(box)

        self.reasons = QTableWidget(0, 4)
        self.reasons.setHorizontalHeaderLabels(["Начало", "Конец", "Длит.", "Что там"])
        self.reasons.verticalHeader().setVisible(False)
        self.reasons.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.reasons.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.reasons.setMaximumHeight(170)
        rl.addWidget(self.reasons)

        self.checks = QLabel("")
        self.checks.setWordWrap(True)
        self.checks.setTextFormat(Qt.RichText)
        rl.addWidget(self.checks)
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("Фрагменты расшифровки (для проверки темы)")
        self.transcript.setMaximumHeight(120)
        rl.addWidget(self.transcript)

        act_row = QHBoxLayout()
        self.b_analyze = QPushButton("Анализировать")
        self.b_analyze.clicked.connect(self.analyze_current)
        act_row.addWidget(self.b_analyze)
        self.mode = QComboBox()
        self.mode.addItem("Видео и звук (границы по ключевым кадрам)", "video")
        self.mode.addItem("Только звук (точно)", "audio")
        self.mode.setToolTip("Для видео: без перекодирования видео можно резать только по ключевым кадрам.\n"
                             "«Только звук» сохраняет звуковую дорожку с точными границами.")
        act_row.addWidget(self.mode)
        act_row.addStretch(1)
        self.b_cut = QPushButton("Вырезать ✂")
        self.b_cut.setStyleSheet("font-weight: 600; padding: 6px 18px;")
        self.b_cut.setToolTip("Создать новый файл без выделенных кусков. Без перекодирования — качество не меняется.")
        self.b_cut.clicked.connect(self.cut_current)
        act_row.addWidget(self.b_cut)
        rl.addLayout(act_row)
        self.cut_info = QLabel("")
        self.cut_info.setWordWrap(True)
        self.cut_info.setTextFormat(Qt.RichText)
        self.cut_info.setOpenExternalLinks(False)
        self.cut_info.linkActivated.connect(lambda url: QDesktopServices.openUrl(QUrl.fromLocalFile(url)))
        rl.addWidget(self.cut_info)
        rl.addStretch(1)
        split.addWidget(right)
        split.setSizes([760, 640])
        self.setCentralWidget(split)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setVisible(False)
        self.status_text = QLabel("")
        self.statusBar().addWidget(self.status_text, 1)
        self.statusBar().addPermanentWidget(self.progress)
        self._show_details(None)

    # ------------------------------------------------------------------ files
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        self.add_paths(paths)

    def add_files_dialog(self):
        pattern = " ".join("*" + e for e in sorted(MEDIA_EXT))
        files, _ = QFileDialog.getOpenFileNames(self, "Аудио и видео файлы", self.qs.value("last_dir", ""),
                                                f"Аудио и видео ({pattern});;Все файлы (*)")
        if files:
            self.qs.setValue("last_dir", os.path.dirname(files[0]))
            self.add_paths(files)

    def add_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Папка с файлами", self.qs.value("last_dir", ""))
        if d:
            self.qs.setValue("last_dir", d)
            self.add_paths([d])

    def add_paths(self, paths):
        files = []
        for p in paths:
            if os.path.isdir(p):
                for root, _, names in os.walk(p):
                    files += [os.path.join(root, n) for n in sorted(names) if os.path.splitext(n)[1].lower() in MEDIA_EXT]
            elif os.path.isfile(p):
                files.append(p)
        known = {it["path"] for it in self.items.values()}
        for f in files:
            f = os.path.abspath(f)
            if f in known or os.path.basename(f).startswith(".") or "_cut" in os.path.basename(f):
                continue
            self._add_item(f)
        self._save_session()

    def _add_item(self, path, state=None):
        fid = self.next_id
        self.next_id += 1
        it = {"path": path, "status": "ожидает анализа", "result": None, "text": None, "cut": None}
        if state:
            it.update({k: state.get(k) for k in ("status", "result", "text", "cut")})
        self.items[fid] = it
        r = self.table.rowCount()
        self.table.insertRow(r)
        name = QTableWidgetItem(os.path.basename(path))
        name.setData(Qt.UserRole, fid)
        name.setToolTip(path)
        self.table.setItem(r, 0, name)
        for c in range(1, len(COLS)):
            self.table.setItem(r, c, QTableWidgetItem(""))
        self._refresh_row(fid)
        if self.table.rowCount() == 1:
            self.table.selectRow(0)
        return fid

    def _row_of(self, fid):
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.UserRole) == fid:
                return r
        return -1

    def _refresh_row(self, fid):
        r, it = self._row_of(fid), self.items[fid]
        if r < 0:
            return
        res = it["result"] or {}
        self.table.item(r, 1).setText(fmt_time(res["duration"]) if res else "")
        self.table.item(r, 2).setText(it["status"])
        tc = it["text"] if it["text"] is not None else res.get("timecodes", "")
        self.table.item(r, 3).setText(tc)
        self.table.item(r, 3).setToolTip(tc)
        acc = (res.get("accent") or {}).get("groups") or {}
        top = next(iter(acc.items()), None)
        acc_item = self.table.item(r, 4)
        short = {"британский": "брит.", "американский": "амер.", "ирландский": "ирл.", "австралийский": "австрал.",
                 "индийский": "инд.", "другой": "другой"}
        acc_item.setText(f"{short.get(top[0], top[0])} {top[1]:.0%}" if top else "")
        acc_item.setToolTip(", ".join(f"{k} {v:.0%}" for k, v in acc.items()))
        acc_item.setForeground(QColor("#2e7d32") if top and top[0] == "британский" else QColor("#c62828"))
        topic = res.get("topic")
        t_item = self.table.item(r, 5)
        t_item.setText("" if not topic else ("медицинская" if topic.get("medical") else "проверьте"))
        t_item.setForeground(QColor("#2e7d32") if topic and topic.get("medical") else QColor("#ef6c00"))
        cut = it.get("cut")
        self.table.item(r, 6).setText(("✓ " if (cut.get("verification") or {}).get("lossless") else "") +
                                      os.path.basename(cut["output"]) if cut else "")

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        for r in rows:
            fid = self.table.item(r, 0).data(Qt.UserRole)
            self.items.pop(fid, None)
            self.table.removeRow(r)
        self._save_session()
        self.on_select()

    def selected_fid(self):
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    # ------------------------------------------------------------------ details
    def on_select(self):
        self._show_details(self.selected_fid())

    def _show_details(self, fid):
        self.current = fid
        it = self.items.get(fid)
        enabled = it is not None
        for w in (self.b_analyze, self.b_cut, self.tc_edit, self.mode):
            w.setEnabled(enabled)
        if not it:
            self.info.setText("")
            self.timeline.set_data(0, [], [], [])
            self.tc_edit.blockSignals(True)
            self.tc_edit.setPlainText("")
            self.tc_edit.blockSignals(False)
            self.reasons.setRowCount(0)
            self.checks.setText("")
            self.transcript.setPlainText("")
            self.cut_info.setText("")
            return
        res = it["result"]
        self.title.setText(os.path.basename(it["path"]))
        self.tc_edit.blockSignals(True)
        self.tc_edit.setPlainText(it["text"] if it["text"] is not None else (res or {}).get("timecodes", ""))
        self.tc_edit.blockSignals(False)
        self.on_tc_changed(store=False)
        if not res:
            self.info.setText(f"{it['path']}\nСтатус: {it['status']}")
            self.timeline.set_data(0, [], [], [])
            self.reasons.setRowCount(0)
            self.checks.setText("")
            self.transcript.setPlainText("")
        else:
            self.info.setText(f"{it['path']}\nДлительность {fmt_time(res['duration'])}, речь {res['speech_ratio']:.0%}")
            self.reasons.setRowCount(0)
            for c in res["cuts"]:
                r = self.reasons.rowCount()
                self.reasons.insertRow(r)
                for col, val in enumerate([fmt_time(c["start"]), fmt_time(c["end"], floor=True),
                                           f"{c['end'] - c['start']:.0f} с", ", ".join(c["reasons"])]):
                    self.reasons.setItem(r, col, QTableWidgetItem(val))
            self.checks.setText(self._checks_html(res))
            self.transcript.setPlainText(res.get("transcript") or "")
        self._update_timeline()
        self._show_cut_info(it)
        if "has_video" not in it:
            try:
                it["has_video"] = bool(cutter.probe(it["path"])["video"])
            except Exception:
                it["has_video"] = False
        self.mode.setVisible(it["has_video"])

    def _checks_html(self, res):
        out = []
        acc = res.get("accent")
        if acc:
            groups = ", ".join(f"{k} {v:.0%}" for k, v in acc["groups"].items())
            good = acc["top"] == "британский"
            out.append(f"<b>Акцент:</b> <span style='color:{'#2e7d32' if good else '#c62828'}'>{groups}</span>"
                       + ("" if good else " — <b>не британский</b>"))
            if acc.get("non_british") and good:
                out.append("&nbsp;&nbsp;не британский на участках: " + ", ".join(
                    f"{o['group']} {fmt_time(o['start'])}–{fmt_time(o['end'])}" for o in acc["non_british"]))
        top = res.get("topic")
        if top:
            if top["medical"]:
                out.append(f"<b>Тема:</b> <span style='color:#2e7d32'>медицинская</span> ({', '.join(top['top_terms'][:5])})")
            else:
                out.append("<b>Тема:</b> <span style='color:#ef6c00'>проверьте — мало медицинских слов</span>"
                           " (психотерапию программа узнаёт плохо; см. расшифровку ниже)")
        ph = res.get("phase") or {}
        if ph.get("phase_inverted_s"):
            rng = ", ".join(f"{fmt_time(a)}–{fmt_time(b)}" for a, b in ph["phase_inverted_ranges"][:6])
            out.append(f"<b>Внимание:</b> каналы в противофазе ({rng}) — при переводе в моно голос там пропадёт")
        if res.get("hints"):
            out.append("<b>Паузы 5–7 с</b> (не вырезаются): " + ", ".join(
                f"{fmt_time(h['start'])}–{fmt_time(h['end'])}" for h in res["hints"]))
        return "<br>".join(out)

    def _update_timeline(self):
        it = self.items.get(self.current)
        if not it or not it["result"]:
            return
        res = it["result"]
        try:
            cuts = parse(self.tc_edit.toPlainText(), res["duration"])
        except ValueError:
            cuts = [(c["start"], c["end"]) for c in res["cuts"]]
        self.timeline.set_data(res["duration"], res.get("segments", []), cuts,
                               [(h["start"], h["end"]) for h in res.get("hints", [])])

    def on_tc_changed(self, store=True):
        it = self.items.get(self.current)
        if not it:
            return
        text = self.tc_edit.toPlainText()
        dur = (it["result"] or {}).get("duration")
        try:
            cuts = parse(text, dur)
            total = sum(e - s for s, e in cuts if e != float("inf"))
            self.tc_status.setText(f"вырезов: {len(cuts)}, всего {total:.0f} с" if cuts else "вырезать нечего")
            self.tc_status.setStyleSheet("color: #455a64")
        except ValueError as e:
            self.tc_status.setText(str(e))
            self.tc_status.setStyleSheet("color: #c62828")
        if store:
            it["text"] = text
            self._refresh_row(self.current)
            self._update_timeline()
            self._save_session()

    def reset_timecodes(self):
        it = self.items.get(self.current)
        if it and it["result"]:
            it["text"] = None
            self._show_details(self.current)
            self._refresh_row(self.current)

    def _show_cut_info(self, it):
        cut = it.get("cut")
        if not cut:
            self.cut_info.setText("")
            return
        v = cut.get("verification") or {}
        folder = os.path.dirname(cut["output"])
        lines = [f"Сохранено: <a href='{folder}'>{os.path.basename(cut['output'])}</a> "
                 f"({fmt_time(cut['output_duration'])} из {fmt_time(cut['source_duration'])})"]
        if v.get("lossless"):
            if "identical_samples" in v.get("audio", {}):
                lines.append("<span style='color:#2e7d32'>✓ Без потери качества: звук совпадает с исходным до последнего отсчёта.</span>")
            else:
                a = v.get("audio", {})
                txt = f"все {a.get('packets', 0)} аудиопакетов"
                if "video" in v:
                    txt += f" и {v['video'].get('packets', 0)} видеокадров"
                lines.append(f"<span style='color:#2e7d32'>✓ Без перекодирования: {txt} скопированы бит-в-бит из исходника.</span>")
        elif v:
            lines.append("<span style='color:#c62828'>Проверка качества не прошла — не используйте этот файл и сообщите разработчику.</span>")
        for n in cut.get("notes", []):
            lines.append(f"<span style='color:#ef6c00'>{n}</span>")
        self.cut_info.setText("<br>".join(lines))

    # ------------------------------------------------------------------ actions
    def analyze_current(self):
        if self.current:
            self._queue_analysis(self.current)

    def analyze_all(self):
        n = 0
        for fid, it in self.items.items():
            if it["result"] is None and not it["status"].startswith(("в очереди", "анализ")):
                self._queue_analysis(fid)
                n += 1
        if not n:
            self.status_text.setText("Все файлы уже проанализированы (для повторного анализа — кнопка «Анализировать»).")

    def _queue_analysis(self, fid):
        it = self.items[fid]
        it["status"] = "в очереди"
        self._refresh_row(fid)
        self.worker.add(fid, "analyze", {"path": it["path"], "settings": Settings(**asdict(self.settings))})
        self.progress.setVisible(True)

    def cut_current(self):
        it = self.items.get(self.current)
        if not it:
            return
        dur = (it["result"] or {}).get("duration")
        if dur is None:
            try:
                dur = cutter.probe(it["path"])["duration"]
            except Exception as e:
                QMessageBox.warning(self, "Ошибка", str(e))
                return
        try:
            cuts = parse(self.tc_edit.toPlainText(), dur)
        except ValueError as e:
            QMessageBox.warning(self, "Таймкоды", str(e))
            return
        if not cuts:
            QMessageBox.information(self, "Вырезать", "Вырезать нечего — в поле нет таймкодов (или «good»).")
            return
        mode = self.mode.currentData()
        try:
            info = cutter.probe(it["path"])
            mode = mode if info["video"] else "audio"
            ext = (cutter.AUDIO_CONTAINER.get(info["audio"][0]["codec_name"], ".mka")
                   if mode == "audio" and info["video"] and info["audio"] else os.path.splitext(it["path"])[1].lower())
        except Exception as e:
            QMessageBox.warning(self, "Ошибка", str(e))
            return
        out = cutter.default_output(it["path"], ext, suffix=self.extra.get("suffix", "_cut"),
                                    out_dir=self.extra.get("out_dir") or None)
        it["status"] = "в очереди на вырезание"
        self._refresh_row(self.current)
        self.worker.add(self.current, "cut", {"path": it["path"], "cuts": cuts, "mode": mode, "out_path": out})
        self.progress.setVisible(True)

    def stop_all(self):
        for fid, kind, _ in self.worker.cancel_all():
            if fid in self.items:
                self.items[fid]["status"] = "остановлено"
                self._refresh_row(fid)

    # ------------------------------------------------------------------ worker signals
    def on_progress(self, fid, frac, text):
        it = self.items.get(fid)
        if not it:
            return
        it["status"] = f"{text} {frac:.0%}"
        self._refresh_row(fid)
        self.progress.setVisible(True)
        self.progress.setValue(int(frac * 100))
        left = self.worker.pending
        self.status_text.setText(f"{os.path.basename(it['path'])}: {text}" + (f"   (в очереди ещё {left - 1})" if left > 1 else ""))

    def on_done(self, fid, kind, res):
        it = self.items.get(fid)
        if it is not None:
            if kind == "analyze":
                it["result"], it["text"], it["cut"] = res, None, None
                it["status"] = "готово"
            else:
                it["cut"] = res
                ok = (res.get("verification") or {}).get("lossless")
                it["status"] = "вырезано ✓" if ok else "вырезано (проверка не прошла!)"
            self._refresh_row(fid)
            if fid == self.current:
                self._show_details(fid)
            self._save_session()
        self._maybe_idle()

    def on_failed(self, fid, kind, msg):
        it = self.items.get(fid)
        if it is not None:
            it["status"] = "остановлено" if msg == "остановлено" else "ошибка"
            self._refresh_row(fid)
            if msg != "остановлено":
                self.status_text.setText(f"{os.path.basename(it['path'])}: ошибка")
                QMessageBox.warning(self, "Ошибка", f"{os.path.basename(it['path'])}\n\n{msg}")
        self._maybe_idle()

    def _maybe_idle(self):
        if self.worker.pending <= 0:
            self.progress.setVisible(False)
            self.status_text.setText("Готово")

    # ------------------------------------------------------------------ settings, export, session
    def open_settings(self):
        dlg = SettingsDialog(self.settings, self.extra, self)
        if dlg.exec():
            self.settings, self.extra = dlg.values()
            self.qs.setValue("settings", json.dumps(asdict(self.settings)))
            self.qs.setValue("extra", json.dumps(self.extra))
            self.status_text.setText("Настройки сохранены. Новые пороги применятся при следующем анализе.")

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт", "vidaudcont.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Файл", "Длительность", "Таймкоды для вырезки", "Акцент", "Тема", "Вырезанный файл", "Путь"])
            for r in range(self.table.rowCount()):
                it = self.items[self.table.item(r, 0).data(Qt.UserRole)]
                res = it["result"] or {}
                acc = (res.get("accent") or {}).get("groups") or {}
                top = next(iter(acc.items()), None)
                w.writerow([os.path.basename(it["path"]), fmt_time(res["duration"]) if res else "",
                            it["text"] if it["text"] is not None else res.get("timecodes", ""),
                            f"{top[0]} {top[1]:.0%}" if top else "",
                            "" if not res.get("topic") else ("медицинская" if res["topic"]["medical"] else "проверить"),
                            it["cut"]["output"] if it.get("cut") else "", it["path"]])
        self.status_text.setText(f"Сохранено: {path}")

    def _session_file(self):
        return os.path.join(app_data_dir(), "session.json")

    def _save_session(self):
        data = []
        for r in range(self.table.rowCount()):
            it = self.items[self.table.item(r, 0).data(Qt.UserRole)]
            st = it["status"] if it["status"] in ("готово", "вырезано ✓") or it["result"] else "ожидает анализа"
            data.append({"path": it["path"], "status": st, "result": it["result"], "text": it["text"], "cut": it["cut"]})
        try:
            with open(self._session_file(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError:
            pass

    def _restore_session(self):
        try:
            with open(self._session_file(), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for d in data:
            if os.path.exists(d.get("path", "")):
                if d.get("result") is None:
                    d["status"] = "ожидает анализа"
                self._add_item(d["path"], d)

    def closeEvent(self, ev):
        self._save_session()
        self.worker.stop()
        super().closeEvent(ev)
