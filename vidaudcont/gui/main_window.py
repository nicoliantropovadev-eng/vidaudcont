"""Main window: file list, analysis results with editable timecodes, separate lossless "Cut" button."""
import csv
import json
import os
import queue
import shutil
import threading
import traceback
from dataclasses import asdict, fields

from PySide6.QtCore import QSettings, QStandardPaths, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter,
                               QTableWidget, QTableWidgetItem, QToolBar, QVBoxLayout, QWidget)

from .. import __version__, cutter
from ..engine.analyzer import Models, Settings, analyze
from ..applog import log_event
from ..downloads import FolderWatcher, select_links, write_links
from ..matching import TitleCache, match_files
from ..sheet import SheetClient, SheetError, new_key, script_code
from ..timecodes import fmt_time, format_cuts, parse

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
    "threads": ("Потоков процессора для анализа (0 = половина)", 0, 64, 1),
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
                        self.models = Models(threads=payload["settings"].threads)
                    self.models.set_threads(payload["settings"].threads)
                    res = analyze(payload["path"], self.models, payload["settings"], progress=report,
                                  cancelled=self.cancel_flag.is_set)
                elif kind == "match":
                    res = self._match(payload, report)
                elif kind == "sheet":
                    SheetClient.from_config(payload["cfg"]).write(payload["updates"])
                    res = {"sheet": "записано", "updates": payload["updates"]}
                else:
                    res = self._cut(payload, report)
                self.done.emit(fid, kind, res)
            except InterruptedError:
                self.failed.emit(fid, kind, "остановлено")
            except Exception as e:  # shown to the user, details go to the log
                self.failed.emit(fid, kind, f"{e}\n\n{traceback.format_exc(limit=3)}")
            finally:
                self.pending -= 1


    def _match(self, payload, report):
        report(0.02, "читаю таблицу")
        rows = SheetClient.from_config(payload["cfg"]).rows()
        cache = TitleCache(payload["cache_path"])
        cache.fetch([r["id"] for r in rows], lang=payload["lang"], cancelled=self.cancel_flag.is_set,
                    progress=lambda f, t: report(0.05 + 0.9 * f, t))
        return {"rows": rows, "matches": match_files(payload["paths"], rows, cache.data, lang=payload["lang"])}

    def _cut(self, payload, report):
        if payload["cuts"]:
            res = cutter.cut(payload["path"], payload["cuts"], out_path=payload.get("out_path"), mode=payload["mode"],
                             progress=report, cancelled=self.cancel_flag.is_set)
        else:  # nothing to cut: the file is only copied under its new name (bit-for-bit)
            os.makedirs(os.path.dirname(payload["out_path"]), exist_ok=True)
            shutil.copy2(payload["path"], payload["out_path"])
            dur = cutter.probe(payload["out_path"])["duration"]
            res = {"output": payload["out_path"], "mode": "copy", "keep": [], "notes": ["вырезать было нечего — файл скопирован"],
                   "source_duration": dur, "expected_duration": dur, "output_duration": dur,
                   "verification": {"lossless": True, "copy": True}}
        if payload.get("sheet"):
            report(0.99, "запись в таблицу")
            try:
                SheetClient.from_config(payload["sheet"]["cfg"]).write([payload["sheet"]["update"]])
                res["sheet"], res["sheet_update"] = "записано", payload["sheet"]["update"]
            except Exception as e:  # the file is cut; only the table update failed
                res["sheet"] = f"ошибка: {e}"
        return res


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
                w.setDecimals(0 if isinstance(val, int) else (2 if step < 0.5 else 1))
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
            if isinstance(w, QCheckBox):
                setattr(st, name, w.isChecked())
            else:
                setattr(st, name, int(w.value()) if isinstance(getattr(st, name), int) else float(w.value()))
        return st, {"out_dir": self.out_dir.text().strip(), "suffix": self.suffix.text() or "_cut"}


class SheetDialog(QDialog):
    """Connection to the Google Sheet (Apps Script web app) and what to write there."""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Google Таблица")
        self.setMinimumWidth(640)
        self.cfg = dict(cfg)
        if not self.cfg.get("key"):
            self.cfg["key"] = new_key()
        form = QFormLayout(self)
        steps = QLabel(
            "<b>Подключение (один раз):</b><br>"
            "1. Нажмите «Скопировать код для Google».<br>"
            "2. В таблице: Расширения → Apps Script. Удалите всё в редакторе, вставьте код, нажмите «Сохранить».<br>"
            "3. «Начать развёртывание» → «Новое развёртывание» → шестерёнка → «Веб-приложение»; "
            "запуск от имени: «Я», доступ: «Все» → «Начать развёртывание», разрешите доступ.<br>"
            "4. Скопируйте «URL веб-приложения» в поле ниже и нажмите «Проверить связь».")
        steps.setWordWrap(True)
        form.addRow(steps)
        b_code = QPushButton("Скопировать код для Google")
        b_code.clicked.connect(self._copy_code)
        form.addRow(b_code)
        self.url = QLineEdit(self.cfg.get("url", ""))
        self.url.setPlaceholderText("https://script.google.com/macros/s/…/exec")
        form.addRow("URL веб-приложения", self.url)
        self.sheet = QLineEdit(self.cfg.get("sheet", ""))
        self.sheet.setPlaceholderText("первый лист")
        form.addRow("Лист", self.sheet)
        cols = QHBoxLayout()
        self.link_col = QLineEdit(self.cfg.get("link_col", "C"))
        self.cuts_col = QLineEdit(self.cfg.get("cuts_col", "D"))
        self.note_col = QLineEdit(self.cfg.get("note_col", "E"))
        for label, w in (("ссылки", self.link_col), ("таймкоды", self.cuts_col), ("пометки", self.note_col)):
            w.setMaximumWidth(40)
            cols.addWidget(QLabel(label))
            cols.addWidget(w)
        cols.addStretch(1)
        form.addRow("Колонки", cols)
        self.write_note = QCheckBox("Писать в колонку пометок «АМЕРИКАНСКИЙ АКЦЕНТ» и т.п., если там пусто")
        self.write_note.setChecked(self.cfg.get("write_note", True))
        form.addRow(self.write_note)
        self.overwrite = QCheckBox("Перезаписывать таймкоды, которые уже есть в таблице")
        self.overwrite.setChecked(self.cfg.get("overwrite", False))
        form.addRow(self.overwrite)
        self.lang = QLineEdit(self.cfg.get("lang", "ru"))
        self.lang.setMaximumWidth(40)
        self.lang.setToolTip("На каком языке YouTube показывал названия, когда вы скачивали файлы (ru, en, …)")
        form.addRow("Язык названий в именах файлов", self.lang)
        row = QHBoxLayout()
        b_ping = QPushButton("Проверить связь")
        b_ping.clicked.connect(self._ping)
        row.addWidget(b_ping)
        self.ping_result = QLabel("")
        self.ping_result.setWordWrap(True)
        row.addWidget(self.ping_result, 1)
        form.addRow(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _copy_code(self):
        QGuiApplication.clipboard().setText(script_code(self.cfg["key"]))
        self.ping_result.setText("Код скопирован — вставьте его в Apps Script.")

    def _ping(self):
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            d = SheetClient.from_config(self.values()).ping()
            self.ping_result.setText(f"<span style='color:#2e7d32'>✓ Связь есть: «{d['spreadsheet']}», "
                                     f"лист «{d['sheet']}», строк: {d['last_row']}</span>")
        except (SheetError, ValueError) as e:
            self.ping_result.setText(f"<span style='color:#c62828'>{e}</span>")
        finally:
            QGuiApplication.restoreOverrideCursor()

    def values(self):
        return dict(self.cfg, url=self.url.text().strip(), sheet=self.sheet.text().strip(),
                    link_col=self.link_col.text().strip() or "C", cuts_col=self.cuts_col.text().strip() or "D",
                    note_col=self.note_col.text().strip() or "E", write_note=self.write_note.isChecked(),
                    overwrite=self.overwrite.isChecked(), lang=self.lang.text().strip() or "ru")


class DownloaderDialog(QDialog):
    """Links for 4K Video Downloader+ and the folder the program watches for finished downloads."""

    def __init__(self, rows, have_rows, watch_dir, watching, auto_analyze, parent=None):
        super().__init__(parent)
        self.setWindowTitle("4K Video Downloader+")
        self.setMinimumWidth(660)
        self.rows, self.have_rows = rows, set(have_rows)
        form = QFormLayout(self)
        first = min(r["row"] for r in rows) if rows else 1
        last = max(r["row"] for r in rows) if rows else 1
        rng = QHBoxLayout()
        self.first, self.last = QSpinBox(), QSpinBox()
        for w, v in ((self.first, first), (self.last, last)):
            w.setRange(1, 100000)
            w.setValue(v)
            w.valueChanged.connect(self._update)
        rng.addWidget(QLabel("с"))
        rng.addWidget(self.first)
        rng.addWidget(QLabel("по"))
        rng.addWidget(self.last)
        rng.addStretch(1)
        form.addRow("Строки таблицы", rng)
        self.skip_filled = QCheckBox("пропустить строки, где уже есть таймкоды (колонка D)")
        self.skip_filled.setChecked(True)
        self.skip_have = QCheckBox("пропустить видео, файлы которых уже есть в программе")
        self.skip_have.setChecked(True)
        for w in (self.skip_filled, self.skip_have):
            w.toggled.connect(self._update)
            form.addRow(w)
        self.count = QLabel("")
        form.addRow("Ссылок", self.count)
        folder = QHBoxLayout()
        self.watch_dir = QLineEdit(watch_dir)
        self.watch_dir.setPlaceholderText("папка, куда 4K Video Downloader+ сохраняет файлы")
        browse = QPushButton("…")
        browse.clicked.connect(self._browse)
        folder.addWidget(self.watch_dir)
        folder.addWidget(browse)
        form.addRow("Папка загрузок", folder)
        self.watching = QCheckBox("следить за этой папкой: новые файлы сами добавляются в программу")
        self.watching.setChecked(watching)
        form.addRow(self.watching)
        self.auto_analyze = QCheckBox("и сразу анализировать их")
        self.auto_analyze.setChecked(auto_analyze)
        form.addRow(self.auto_analyze)
        buttons = QHBoxLayout()
        b_copy = QPushButton("Скопировать ссылки")
        b_copy.clicked.connect(self._copy)
        b_save = QPushButton("Сохранить список в файл…")
        b_save.clicked.connect(self._save)
        buttons.addWidget(b_copy)
        buttons.addWidget(b_save)
        buttons.addStretch(1)
        form.addRow(buttons)
        # its File → Import only takes its own export files (*.json *.csv *.4kv), but Paste Link takes many links at once
        self.help = QLabel(
            "<b>В 4K Video Downloader+:</b> включите «Умный режим» (значок лампочки): <i>Аудио</i>, формат <i>M4A</i>, "
            "папка — та же, что выше. Нажмите здесь «Скопировать ссылки», а там — «Вставить ссылку»: добавятся "
            "все ссылки сразу. Если добавились не все, копируйте частями, например по 50 строк. "
            "Готовые файлы программа подхватит сама.")
        self.help.setWordWrap(True)
        form.addRow(self.help)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self._update()

    def links(self):
        return select_links(self.rows, self.first.value(), self.last.value(), self.skip_filled.isChecked(),
                            self.have_rows if self.skip_have.isChecked() else ())

    def _update(self):
        n = len(self.links())
        self.count.setText(f"{n}" + ("  (ничего не выбрано — проверьте диапазон и галочки)" if not n else ""))

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Папка загрузок 4K Video Downloader+", self.watch_dir.text())
        if d:
            self.watch_dir.setText(d)

    def _copy(self):
        QGuiApplication.clipboard().setText("\n".join(link for _, link in self.links()))
        self.count.setText(self.count.text().split("  ")[0] + "  — скопированы")

    def _save(self):
        links = self.links()
        if not links:
            return
        start = os.path.join(self.watch_dir.text() or os.path.expanduser("~"),
                             f"ссылки_строки_{links[0][0]}-{links[-1][0]}.txt")
        path, _ = QFileDialog.getSaveFileName(self, "Файл со ссылками", start, "Текст (*.txt)")
        if path:
            write_links(path, links)
            self.count.setText(f"{len(links)}  — сохранено в {os.path.basename(path)}")

    def values(self):
        return self.watch_dir.text().strip(), self.watching.isChecked(), self.auto_analyze.isChecked()


ACCENT_NOTE = {"американский": "АМЕРИКАНСКИЙ АКЦЕНТ", "ирландский": "ИРЛАНДСКИЙ АКЦЕНТ",
               "австралийский": "АВСТРАЛИЙСКИЙ АКЦЕНТ", "индийский": "ИНДИЙСКИЙ АКЦЕНТ", "другой": "НЕ БРИТАНСКИЙ АКЦЕНТ"}

COLS = ["Файл", "Строка", "Длит.", "Статус", "Таймкоды для вырезки", "Акцент", "Тема", "Вырезано"]
COL_WIDTH = [220, 56, 56, 110, 250, 90, 95, 90]
C_FILE, C_ROW, C_DUR, C_STATUS, C_TC, C_ACC, C_TOPIC, C_CUT = range(8)


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
        self.sheet_cfg = json.loads(self.qs.value("sheet", "{}") or "{}")
        self.sheet_rows = {}  # row -> {id, link, cuts, note} as last read from the table
        self._loading = False
        self._match_queued = False
        self.watch_dir = self.qs.value("watch_dir", "") or ""
        self.watching = self.qs.value("watching", "false") == "true"
        self.auto_analyze = self.qs.value("auto_analyze", "true") == "true"
        self.watcher = FolderWatcher(self.watch_dir)
        self.watch_timer = QTimer(self)
        self.watch_timer.setInterval(4000)
        self.watch_timer.timeout.connect(self._scan_watch)
        if self.watching and self.watch_dir:
            self.watch_timer.start()

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
        act("Вырезать все ✂", self.cut_all, "Вырезать все проанализированные файлы, назвать их номером строки "
                                             "и записать таймкоды в таблицу")
        act("Остановить", self.stop_all)
        tb.addSeparator()
        act("Таблица…", self.open_sheet, "Подключение к Google Таблице")
        act("Найти строки", self.find_rows, "Определить, в какой строке таблицы ссылка на каждый файл")
        act("4K Video Downloader+…", self.open_downloader, "Ссылки из таблицы для загрузки и папка, за которой "
                                                           "следит программа")
        act("Экспорт в CSV…", self.export_csv, "Таблица с таймкодами всех файлов (открывается в Excel/Google Таблицах)")
        act("Настройки…", self.open_settings)

        split = QSplitter(Qt.Horizontal)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self.on_item_changed)
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
        self.b_to_sheet = QPushButton("В таблицу")
        self.b_to_sheet.setToolTip("Записать эти таймкоды в Google Таблицу, в строку этого файла")
        self.b_to_sheet.clicked.connect(self.write_current_to_sheet)
        row.addWidget(self.b_to_sheet)
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
                for root, dirs, names in os.walk(p):
                    dirs[:] = [d for d in dirs if d != "готово" and not d.startswith(".")]  # our own results
                    files += [os.path.join(root, n) for n in sorted(names) if os.path.splitext(n)[1].lower() in MEDIA_EXT]
            elif os.path.isfile(p):
                files.append(p)
        known = {it["path"] for it in self.items.values()}
        outputs = {os.path.abspath(it["cut"]["output"]) for it in self.items.values() if it.get("cut")}
        added = 0
        for f in files:
            f = os.path.abspath(f)
            if f in known or f in outputs or os.path.basename(f).startswith(".") or "_cut" in os.path.basename(f):
                continue
            self._add_item(f)
            added += 1
        self._save_session()
        if added and self.sheet_cfg.get("url"):
            self.find_rows(quiet=True)

    def _add_item(self, path, state=None):
        fid = self.next_id
        self.next_id += 1
        it = {"path": path, "status": "ожидает анализа", "result": None, "text": None, "cut": None,
              "row": None, "row_how": ""}
        if state:
            it.update({k: state.get(k) for k in ("status", "result", "text", "cut", "row", "row_how") if k in state})
        self.items[fid] = it
        self._loading = True
        r = self.table.rowCount()
        self.table.insertRow(r)
        name = QTableWidgetItem(os.path.basename(path))
        name.setData(Qt.UserRole, fid)
        name.setToolTip(path)
        name.setFlags(name.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(r, C_FILE, name)
        for c in range(1, len(COLS)):
            cell = QTableWidgetItem("")
            if c != C_ROW:
                cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(r, c, cell)
        self._loading = False
        self._refresh_row(fid)
        if self.table.rowCount() == 1:
            self.table.selectRow(0)
        return fid

    def _row_of(self, fid):
        for r in range(self.table.rowCount()):
            if self.table.item(r, C_FILE).data(Qt.UserRole) == fid:
                return r
        return -1

    def _refresh_row(self, fid):
        r, it = self._row_of(fid), self.items[fid]
        if r < 0:
            return
        self._loading = True
        res = it["result"] or {}
        row_item = self.table.item(r, C_ROW)
        row_item.setText(str(it["row"]) if it.get("row") else "")
        info = self.sheet_rows.get(it.get("row")) if it.get("row") else None
        row_item.setToolTip((f"Найдено: {it.get('row_how')}\n" if it.get("row_how") else "") +
                            (info["link"] if info else "Двойной щелчок — указать номер строки вручную"))
        self.table.item(r, C_DUR).setText(fmt_time(res["duration"]) if res else "")
        self.table.item(r, C_STATUS).setText(it["status"])
        tc = it["text"] if it["text"] is not None else res.get("timecodes", "")
        self.table.item(r, C_TC).setText(tc)
        self.table.item(r, C_TC).setToolTip(tc)
        acc = (res.get("accent") or {}).get("groups") or {}
        top = next(iter(acc.items()), None)
        acc_item = self.table.item(r, C_ACC)
        short = {"британский": "брит.", "американский": "амер.", "ирландский": "ирл.", "австралийский": "австрал.",
                 "индийский": "инд.", "другой": "другой"}
        acc_item.setText(f"{short.get(top[0], top[0])} {top[1]:.0%}" if top else "")
        acc_item.setToolTip(", ".join(f"{k} {v:.0%}" for k, v in acc.items()))
        acc_item.setForeground(QColor("#2e7d32") if top and top[0] == "британский" else QColor("#c62828"))
        topic = res.get("topic")
        t_item = self.table.item(r, C_TOPIC)
        t_item.setText("" if not topic else ("медицинская" if topic.get("medical") else "проверьте"))
        t_item.setForeground(QColor("#2e7d32") if topic and topic.get("medical") else QColor("#ef6c00"))
        cut = it.get("cut")
        cut_item = self.table.item(r, C_CUT)
        cut_item.setText(("✓ " if (cut.get("verification") or {}).get("lossless") else "") +
                         os.path.basename(cut["output"]) if cut else "")
        cut_item.setToolTip(f"в таблице: {cut['sheet']}" if cut and cut.get("sheet") else "")
        self._loading = False

    def on_item_changed(self, item):
        if self._loading or item.column() != C_ROW:
            return
        fid = self.table.item(item.row(), C_FILE).data(Qt.UserRole)
        it = self.items.get(fid)
        if not it:
            return
        txt = item.text().strip()
        if not txt:
            it["row"], it["row_how"] = None, ""
        elif txt.isdigit() and int(txt) > 0:
            it["row"], it["row_how"] = int(txt), "вручную"
        else:
            QMessageBox.warning(self, "Строка", "Номер строки — это целое число, например 80.")
        self._refresh_row(fid)
        self._save_session()

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        for r in rows:
            fid = self.table.item(r, C_FILE).data(Qt.UserRole)
            self.items.pop(fid, None)
            self.table.removeRow(r)
        self._save_session()
        self.on_select()

    def selected_fid(self):
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), C_FILE).data(Qt.UserRole) if rows else None

    # ------------------------------------------------------------------ details
    def on_select(self):
        self._show_details(self.selected_fid())

    def _show_details(self, fid):
        self.current = fid
        it = self.items.get(fid)
        enabled = it is not None
        for w in (self.b_analyze, self.b_cut, self.tc_edit, self.mode, self.b_to_sheet):
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
        if not cuts and not it.get("row"):
            QMessageBox.information(self, "Вырезать", "Вырезать нечего — в поле нет таймкодов (или «good»).")
            return
        mode = self.mode.currentData() if it.get("has_video") else None
        if it.get("row") and not self._refresh_sheet_rows():
            return
        self._queue_cut(self.current, cuts, format_cuts(cuts, dur), mode)

    # ------------------------------------------------------------------ Google Sheet
    def open_sheet(self):
        dlg = SheetDialog(self.sheet_cfg, self)
        if dlg.exec():
            self.sheet_cfg = dlg.values()
            self.qs.setValue("sheet", json.dumps(self.sheet_cfg))
            if self.sheet_cfg.get("url") and self.items:
                self.find_rows()

    def find_rows(self, quiet=False):
        if not self.sheet_cfg.get("url"):
            if not quiet:
                QMessageBox.information(self, "Таблица", "Сначала подключите таблицу: кнопка «Таблица…».")
            return
        if self._match_queued:
            return
        self._match_queued = True
        paths = [it["path"] for it in self.items.values() if it.get("row_how") != "вручную"]
        self.worker.add(0, "match", {"cfg": self.sheet_cfg, "paths": paths, "lang": self.sheet_cfg.get("lang", "ru"),
                                     "cache_path": os.path.join(app_data_dir(), "titles.json")})
        self.progress.setVisible(True)
        self.status_text.setText("Ищу строки таблицы для файлов…")

    def _apply_matches(self, res):
        self.sheet_rows = {r["row"]: r for r in res["rows"]}
        by_path = {it["path"]: fid for fid, it in self.items.items()}
        for path in res["matches"]:
            if path in by_path:
                self.items[by_path[path]]["_match_tried"] = True
        found = 0
        for path, (row, how) in res["matches"].items():
            fid = by_path.get(path)
            if fid is None or self.items[fid].get("row_how") == "вручную":
                continue
            self.items[fid]["row"], self.items[fid]["row_how"] = row, how
            found += row is not None
            self._refresh_row(fid)
        missing = [os.path.basename(p) for p, (row, _) in res["matches"].items() if row is None]
        self._save_session()
        msg = f"Строки найдены для {found} из {len(res['matches'])} файлов."
        if missing:
            msg += f" Не найдены: {len(missing)} — укажите номер вручную (двойной щелчок в колонке «Строка»)."
        self.status_text.setText(msg)

    def _pending_match_paths(self):
        return [it["path"] for it in self.items.values() if it.get("row") is None and it.get("row_how") != "вручную"
                and not it.get("_match_tried")]

    def open_downloader(self):
        rows = []
        if self.sheet_cfg.get("url"):
            if not self._refresh_sheet_rows():
                return
            rows = list(self.sheet_rows.values())
        elif QMessageBox.question(self, "4K Video Downloader+", "Таблица не подключена — список ссылок не составить. "
                                  "Настроить только папку загрузок?") != QMessageBox.Yes:
            return
        have = {it["row"] for it in self.items.values() if it.get("row")}
        dlg = DownloaderDialog(rows, have, self.watch_dir, self.watching, self.auto_analyze, self)
        if dlg.exec():
            self.watch_dir, self.watching, self.auto_analyze = dlg.values()
            self.qs.setValue("watch_dir", self.watch_dir)
            self.qs.setValue("watching", "true" if self.watching else "false")
            self.qs.setValue("auto_analyze", "true" if self.auto_analyze else "false")
            if self.watcher.folder != self.watch_dir:
                self.watcher = FolderWatcher(self.watch_dir)
            if self.watching and self.watch_dir:
                self.watch_timer.start()
                self.status_text.setText(f"Слежу за папкой: {self.watch_dir}")
                self._scan_watch()
            else:
                self.watch_timer.stop()

    def _scan_watch(self):
        known = {it["path"] for it in self.items.values()}
        known |= {os.path.abspath(it["cut"]["output"]) for it in self.items.values() if it.get("cut")}
        new = self.watcher.scan(known)
        if not new:
            return
        before = set(self.items)
        self.add_paths(new)
        added = [fid for fid in self.items if fid not in before]
        log_event(f"из папки загрузок добавлено: {len(added)}")
        if self.auto_analyze:
            for fid in added:
                self._queue_analysis(fid)
        self.status_text.setText(f"Из папки загрузок добавлено файлов: {len(added)}"
                                 + (" — анализ запущен" if self.auto_analyze and added else ""))

    def _cuts_for(self, it):
        """(cuts, canonical timecode string) for a file, from the edited field or the analysis."""
        dur = it["result"]["duration"]
        text = it["text"] if it["text"] is not None else it["result"]["timecodes"]
        cuts = parse(text, dur)
        return cuts, format_cuts(cuts, dur)

    def _sheet_update(self, it, tc):
        """What to write for this file (respecting existing cells), or None."""
        if not (self.sheet_cfg.get("url") and it.get("row")):
            return None
        upd = {"row": it["row"]}
        old = self.sheet_rows.get(it["row"], {})
        if self.sheet_cfg.get("overwrite") or not (old.get("cuts") or "").strip() or old.get("cuts", "").strip() == tc:
            upd["cuts"] = tc
        acc = (it["result"] or {}).get("accent") or {}
        if self.sheet_cfg.get("write_note", True) and acc and acc.get("top") != "британский" \
                and not (old.get("note") or "").strip():
            upd["note"] = ACCENT_NOTE.get(acc["top"], "НЕ БРИТАНСКИЙ АКЦЕНТ")
        return upd if len(upd) > 1 else None

    def _row_output(self, it, ext):
        folder = self.extra.get("out_dir") or os.path.join(os.path.dirname(it["path"]), "готово")
        name = f"{it['row']}{ext}"
        out = os.path.join(folder, name)
        if os.path.exists(out) and os.path.abspath(out) != os.path.abspath((it.get("cut") or {}).get("output", "")):
            out = cutter.default_output(out, ext, suffix="", out_dir=folder)
        return out

    def _remember_written(self, upd):
        row = self.sheet_rows.setdefault(upd["row"], {"row": upd["row"], "link": "", "cuts": "", "note": ""})
        for k in ("cuts", "note"):
            if k in upd:
                row[k] = upd[k]

    def cut_all(self):
        todo, no_row, bad = [], [], []
        for r in range(self.table.rowCount()):
            fid = self.table.item(r, C_FILE).data(Qt.UserRole)
            it = self.items[fid]
            if not it["result"] or (it.get("cut") and it["status"].startswith("вырезано")):
                continue
            try:
                cuts, tc = self._cuts_for(it)
            except ValueError as e:
                bad.append(f"{os.path.basename(it['path'])}: {e}")
                continue
            if not it.get("row"):
                no_row.append(os.path.basename(it["path"]))
            todo.append((fid, cuts, tc))
        if not todo:
            QMessageBox.information(self, "Вырезать все", "Нет проанализированных файлов, которые ещё не вырезаны.")
            return
        with_row = sum(1 for fid, _, _ in todo if self.items[fid].get("row"))
        text = f"Будет обработано файлов: {len(todo)}.\n\n"
        if with_row:
            text += (f"{with_row} получат имя по номеру строки (например «{self.items[todo[0][0]].get('row') or 80}.m4a»)"
                     f" в папке «{self.extra.get('out_dir') or 'готово'}»")
            text += " и таймкоды будут записаны в таблицу.\n" if self.sheet_cfg.get("url") else ".\n"
        if no_row:
            text += f"\nБез номера строки ({len(no_row)}) — сохранятся как «имя_cut», в таблицу не попадут:\n  " + \
                    "\n  ".join(no_row[:8]) + ("\n  …" if len(no_row) > 8 else "") + "\n"
        if bad:
            text += "\nПропущены из-за ошибок в таймкодах:\n  " + "\n  ".join(bad[:5]) + "\n"
        text += "\nИсходные файлы не изменяются. Продолжить?"
        if QMessageBox.question(self, "Вырезать все", text) != QMessageBox.Yes:
            return
        if not self._refresh_sheet_rows():
            return
        for fid, cuts, tc in todo:
            self._queue_cut(fid, cuts, tc)

    def _refresh_sheet_rows(self):
        """Read the table again right before writing, so filled cells are never overwritten by accident."""
        if not self.sheet_cfg.get("url"):
            return True
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.sheet_rows = {r["row"]: r for r in SheetClient.from_config(self.sheet_cfg).rows()}
            return True
        except (SheetError, ValueError) as e:
            QMessageBox.warning(self, "Таблица", f"Не удалось прочитать таблицу: {e}\n\nФайлы можно вырезать и без "
                                                 "таблицы — отключите её в «Таблица…» (очистите адрес).")
            return False
        finally:
            QGuiApplication.restoreOverrideCursor()

    def _queue_cut(self, fid, cuts, tc, mode=None):
        it = self.items[fid]
        ext = os.path.splitext(it["path"])[1].lower()
        if mode == "audio" and it.get("has_video"):
            info = cutter.probe(it["path"])
            if info["audio"]:
                ext = cutter.AUDIO_CONTAINER.get(info["audio"][0]["codec_name"], ".mka")
        mode = mode or "auto"
        if it.get("row"):
            out = self._row_output(it, ext)
        elif cuts:
            out = cutter.default_output(it["path"], ext, suffix=self.extra.get("suffix", "_cut"),
                                        out_dir=self.extra.get("out_dir") or None)
        else:
            return  # nothing to cut and no row to name it after
        upd = self._sheet_update(it, tc)
        it["status"] = "в очереди на вырезание"
        self._refresh_row(fid)
        self.worker.add(fid, "cut", {"path": it["path"], "cuts": cuts, "mode": mode, "out_path": out,
                                     "sheet": {"cfg": self.sheet_cfg, "update": upd} if upd else None})
        self.progress.setVisible(True)

    def write_current_to_sheet(self):
        it = self.items.get(self.current)
        if not it:
            return
        if not self.sheet_cfg.get("url"):
            QMessageBox.information(self, "Таблица", "Сначала подключите таблицу: кнопка «Таблица…».")
            return
        if not it.get("row"):
            QMessageBox.information(self, "Таблица", "Для этого файла не найдена строка таблицы. Укажите её в колонке "
                                                     "«Строка» (двойной щелчок).")
            return
        dur = (it["result"] or {}).get("duration") or cutter.probe(it["path"])["duration"]
        try:
            tc = format_cuts(parse(self.tc_edit.toPlainText(), dur), dur)
        except ValueError as e:
            QMessageBox.warning(self, "Таймкоды", str(e))
            return
        if not self._refresh_sheet_rows():
            return
        old = (self.sheet_rows.get(it["row"]) or {}).get("cuts", "").strip()
        if old and old != tc and QMessageBox.question(
                self, "Таблица", f"В строке {it['row']} уже записано:\n{old}\n\nЗаменить на:\n{tc}?") != QMessageBox.Yes:
            return
        self.worker.add(self.current, "sheet", {"cfg": self.sheet_cfg, "updates": [{"row": it["row"], "cuts": tc}]})
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
            self.progress.setVisible(True)
            self.progress.setValue(int(frac * 100))
            self.status_text.setText(text)
            return
        it["status"] = f"{text} {frac:.0%}"
        self._refresh_row(fid)
        self.progress.setVisible(True)
        self.progress.setValue(int(frac * 100))
        left = self.worker.pending
        self.status_text.setText(f"{os.path.basename(it['path'])}: {text}" + (f"   (в очереди ещё {left - 1})" if left > 1 else ""))

    def on_done(self, fid, kind, res):
        if kind == "match":
            self._match_queued = False
            self._apply_matches(res)
            if any(it.get("row") is None and it.get("row_how") != "вручную" for it in self.items.values()) \
                    and self._pending_match_paths():
                self.find_rows(quiet=True)  # files that arrived while the search was running
            self._maybe_idle(keep_text=True)
            return
        it = self.items.get(fid)
        if kind == "sheet":
            for u in res.get("updates", []):
                self._remember_written(u)
                self.status_text.setText(f"Строка {u['row']}: таймкоды записаны в таблицу.")
            self._maybe_idle(keep_text=True)
            return
        if it is not None:
            if kind == "analyze":
                it["result"], it["text"], it["cut"] = res, None, None
                it["status"] = "готово"
                log_event(f"анализ: {os.path.basename(it['path'])} -> {res['timecodes']}")
            else:
                it["cut"] = res
                ok = (res.get("verification") or {}).get("lossless")
                log_event(f"вырезано: {os.path.basename(it['path'])} -> {res['output']} "
                          f"(без потерь: {ok}, таблица: {res.get('sheet', '-')})")
                it["status"] = "вырезано ✓" if ok else "вырезано (проверка не прошла!)"
                if res.get("sheet") == "записано":
                    it["status"] += ", в таблице ✓"
                    self._remember_written(res["sheet_update"])
                elif res.get("sheet"):
                    it["status"] += ", таблица: ошибка"
                    self.status_text.setText(f"Строка {it.get('row')}: {res['sheet']}")
            self._refresh_row(fid)
            if fid == self.current:
                self._show_details(fid)
            self._save_session()
        self._maybe_idle()

    def on_failed(self, fid, kind, msg):
        if kind == "match":
            self._match_queued = False
        log_event(f"ошибка ({kind}): {msg}")
        it = self.items.get(fid)
        if it is None and msg != "остановлено":
            QMessageBox.warning(self, "Таблица" if kind in ("match", "sheet") else "Ошибка", msg.split("\n\n")[0])
        if it is not None:
            it["status"] = "остановлено" if msg == "остановлено" else "ошибка"
            self._refresh_row(fid)
            if fid == self.current:
                self._show_details(fid)
            if msg != "остановлено":
                self.status_text.setText(f"{os.path.basename(it['path'])}: ошибка")
                QMessageBox.warning(self, "Ошибка", f"{os.path.basename(it['path'])}\n\n{msg}")
        self._maybe_idle()

    def _maybe_idle(self, keep_text=False):
        if self.worker.pending <= 0:
            self.progress.setVisible(False)
            if not keep_text:
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
            w.writerow(["Файл", "Строка", "Длительность", "Таймкоды для вырезки", "Акцент", "Тема", "Вырезанный файл",
                        "Путь"])
            for r in range(self.table.rowCount()):
                it = self.items[self.table.item(r, C_FILE).data(Qt.UserRole)]
                res = it["result"] or {}
                acc = (res.get("accent") or {}).get("groups") or {}
                top = next(iter(acc.items()), None)
                w.writerow([os.path.basename(it["path"]), it.get("row") or "", fmt_time(res["duration"]) if res else "",
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
            it = self.items[self.table.item(r, C_FILE).data(Qt.UserRole)]
            st = it["status"] if it["status"].startswith(("готово", "вырезано")) or it["result"] else "ожидает анализа"
            if it["result"] and not st.startswith(("готово", "вырезано")):
                st = "готово"
            data.append({"path": it["path"], "status": st, "result": it["result"], "text": it["text"], "cut": it["cut"],
                         "row": it.get("row"), "row_how": it.get("row_how", "")})
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
