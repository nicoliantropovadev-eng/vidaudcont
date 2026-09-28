"""Entry point: the window by default; --analyze / --cut / --selftest for the command line and CI."""
import argparse
import json
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
import traceback

# torch and CTranslate2 both ship Intel OpenMP on Windows; without this the second copy aborts the process
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

from . import __version__, cutter, resources  # noqa: E402
from .timecodes import parse  # noqa: E402


def _log_file():
    d = os.path.join(tempfile.gettempdir(), "VidAudCont")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "vidaudcont.log")


def run_gui(files=()):
    from PySide6.QtWidgets import QApplication

    from .gui.main_window import MainWindow
    from PySide6.QtCore import QLockFile, QStandardPaths, QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QMessageBox

    from . import applog
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setApplicationName("VidAudCont")
    app.setOrganizationName("VidAudCont")
    data_dir = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    os.makedirs(data_dir, exist_ok=True)
    lock = QLockFile(os.path.join(data_dir, "vidaudcont.lock"))  # two copies would overwrite each other's list
    lock.setStaleLockTime(0)  # only a lock of a program that is no longer running is stale
    if not lock.tryLock(200):
        QMessageBox.information(None, "VidAudCont", "Программа уже открыта — её окно на панели задач.")
        return 0
    log_file, crashed = applog.setup(data_dir, __version__)
    w = MainWindow()
    w.show()
    if crashed:
        box = QMessageBox(QMessageBox.Warning, "VidAudCont",
                          "В прошлый раз программа закрылась неожиданно.\n\nПричина записана в журнал:\n"
                          f"{log_file}\n\nПришлите этот файл разработчику — так ошибку можно будет исправить.", parent=w)
        show = box.addButton("Показать файл", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Ok)
        box.exec()
        if box.clickedButton() is show:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(log_file)))
    if files:
        w.add_paths(list(files))
    code = app.exec()
    applog.clean_exit()
    lock.unlock()
    return code


def selftest(out_json=None, gui=False):
    """Everything the packaged app needs, on a known clip. Exit code 0 = all good."""
    from .engine.analyzer import Models, analyze
    res = {"version": __version__, "platform": platform.platform(), "python": sys.version.split()[0], "checks": []}
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        res["checks"].append({"name": name, "ok": bool(cond), "detail": str(detail)[:500]})
        print(("OK   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)

    t0 = time.time()
    tmp = tempfile.mkdtemp(prefix="vidaudcont-selftest-")
    try:
        check("ffmpeg", os.path.exists(resources.tool("ffmpeg")), resources.tool("ffmpeg"))
        check("ffprobe", os.path.exists(resources.tool("ffprobe")), resources.tool("ffprobe"))
        src = os.path.join(tmp, "selftest клип.m4a")  # non-ASCII path on purpose
        shutil.copy(resources.asset("selftest.m4a"), src)

        t = time.time()
        r = analyze(src, Models())
        res["analysis_seconds"] = round(time.time() - t, 1)
        res["timecodes"] = r["timecodes"]
        cuts = r["cuts"]

        def cover(a, b):
            return sum(max(0.0, min(b, c["end"]) - max(a, c["start"])) for c in cuts) / (b - a)

        for a, b in [(0, 5), (30, 40), (55, 60), (72, 76)]:
            check(f"найден вырез {a}-{b} с", cover(a, b) >= 0.8, f"покрыто {cover(a, b):.0%}")
        for a, b in [(5, 30), (40, 52), (60, 72)]:
            check(f"речь {a}-{b} с сохранена", cover(a, b) <= 0.2, f"вырезано {cover(a, b):.0%}")
        check("акцент британский", r["accent"] and r["accent"]["top"] == "британский", r["accent"] and r["accent"]["groups"])
        check("расшифровка для темы", bool(r["transcript"].strip()), r["transcript"][:80])
        d = r.get("dialogue") or {}
        check("проверка «разговор или нет»", "separation" in d, d)
        from .engine.analyzer import load_audio, speech_segments, transcribe_full
        m = Models()
        full = transcribe_full(m, load_audio(src, 16000), r["segments"])
        check("полная расшифровка", "carrot" in full.lower() and len(full) > 200, full[:120])

        from dataclasses import asdict

        from .engine.analyzer import Settings
        from .engine.pool import AnalysisPool
        got, failed, finished = {}, {}, threading.Event()

        def keep(store):
            def f(fid, kind, value):
                store[fid] = value
                if len(got) + len(failed) == 2:
                    finished.set()
            return f
        pool = AnalysisPool(lambda *a: None, keep(got), keep(failed))
        pool.configure(2, workers=2)
        for fid in (1, 2):
            pool.add(fid, src, asdict(Settings()))
        finished.wait(900)
        pool.stop()
        check("два файла одновременно, в отдельных процессах",
              len(got) == 2 and all(g["timecodes"] == r["timecodes"] for g in got.values()),
              failed or [g["timecodes"] for g in got.values()])

        rep = cutter.cut(src, [(c["start"], c["end"]) for c in cuts])
        check("вырезание m4a без потерь", rep["verification"]["lossless"], rep["verification"])
        check("длительность после вырезания", abs(rep["output_duration"] - rep["expected_duration"]) < 0.2,
              f"{rep['output_duration']:.2f} vs {rep['expected_duration']:.2f}")

        ff = resources.tool("ffmpeg")
        flac = os.path.join(tmp, "lossless.flac")
        resources.run([ff, "-v", "error", "-y", "-i", src, "-c:a", "flac", flac], check=True)
        rep = cutter.cut(flac, [(0, 5), (30, 40)])
        check("вырезание flac до отсчёта", rep["verification"]["lossless"], rep["verification"])

        vid = os.path.join(tmp, "video.mp4")
        resources.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=duration=30:size=320x240:rate=25",
                       "-i", src, "-map", "0:v", "-map", "1:a", "-c:v", "mpeg4", "-q:v", "5", "-g", "25", "-bf", "2",
                       "-c:a", "copy", "-t", "30", vid], check=True)
        rep = cutter.cut(vid, [(0, 3), (12, 19.5)], mode="video")
        check("вырезание видео без перекодирования", rep["verification"]["lossless"], rep["verification"])
        rep = cutter.cut(vid, [(0, 3), (12, 19.5)], mode="audio")
        check("звук из видео без потерь", rep["verification"]["lossless"] and rep["output"].endswith(".m4a"),
              rep["verification"])
        check("разбор таймкодов", parse("start-0:13, 3:10-3:17, 6:35-end", 400) == [(0, 13), (190, 197), (395, 400)])
        from .matching import match_files
        from .sheet import script_code
        check("код скрипта для Google Таблицы", "const KEY = 'test-key';" in script_code("test-key"))
        m = match_files([os.path.join(tmp, "Как работать с немотивированным пациентом.m4a")],
                        [{"row": 5, "id": "4Y3EGPjhiXE"}],
                        {"4Y3EGPjhiXE": {"ok": True, "ru": "Как работать с немотивированным пациентом?"}})
        check("поиск строки по названию файла", list(m.values())[0][0] == 5, m)

        if gui:
            from PySide6.QtWidgets import QApplication

            from .gui.main_window import MainWindow
            app = QApplication.instance() or QApplication(sys.argv[:1])
            w = MainWindow()
            w.add_paths([src])
            app.processEvents()
            check("окно программы открывается", w.table.rowCount() >= 1)
            w.close()
    except Exception:
        check("без исключений", False, traceback.format_exc())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    res["ok"] = ok
    res["seconds"] = round(time.time() - t0, 1)
    if out_json:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    print("SELFTEST", "PASSED" if ok else "FAILED", f"in {res['seconds']} s", flush=True)
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="VidAudCont", description="Таймкоды пауз/музыки и вырезание без потерь")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--analyze", action="store_true", help="напечатать таймкоды для файлов и выйти")
    ap.add_argument("--cut", metavar="TIMECODES", help='вырезать из файла, например "start-0:13, 6:35-end"')
    ap.add_argument("--audio-only", action="store_true", help="при вырезании из видео сохранить только звук")
    ap.add_argument("--json", metavar="FILE", help="записать результат в JSON")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--selftest-gui", action="store_true", help="самопроверка вместе с окном программы")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--transcribe-clips", nargs=2, metavar=("CLIPS_NPZ", "OUT_JSON"), help=argparse.SUPPRESS)
    ap.add_argument("--threads", type=int, default=0, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    if a.transcribe_clips:  # helper process: Whisper only, torch is never imported here
        import numpy as np

        from .engine.analyzer import load_whisper, transcribe
        src, dst = a.transcribe_clips
        with np.load(src) as z:
            clips = [z[k] for k in sorted(z.files, key=lambda k: int(k.split("_")[1]))]
        model = load_whisper(resources.models_dir(), a.threads or max(1, (os.cpu_count() or 2) - 1))
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(transcribe(model, clips), f, ensure_ascii=False)
        return 0

    if a.selftest or a.selftest_gui:
        return selftest(a.json, gui=a.selftest_gui)
    if a.analyze:
        from .engine.analyzer import Models, analyze, suitability
        models, out = Models(), []
        for f in a.files:
            r = analyze(f, models)
            why = suitability(r)
            print(f"{os.path.basename(f)}\t{r['timecodes']}\t{'подходит' if not why else ' + '.join(why or [])}",
                  flush=True)
            out.append(r)
        if a.json:
            with open(a.json, "w", encoding="utf-8") as fh:
                json.dump(out, fh, ensure_ascii=False, indent=1)
        return 0
    if a.cut is not None:
        if len(a.files) != 1:
            ap.error("--cut: нужен ровно один файл")
        info = cutter.probe(a.files[0])
        rep = cutter.cut(a.files[0], parse(a.cut, info["duration"]), mode="audio" if a.audio_only else "auto")
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep["verification"]["lossless"] else 2
    return run_gui(a.files)


if __name__ == "__main__":
    sys.exit(main())
