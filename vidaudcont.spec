# PyInstaller spec: one folder app (Windows: VidAudCont\VidAudCont.exe, macOS: VidAudCont.app)
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

sys.path.insert(0, os.path.abspath("."))
from vidaudcont import __version__  # noqa: E402

datas = [("models", "models"), ("assets", "assets"),
         ("vidaudcont/engine/efficientat/class_labels_indices.csv", "vidaudcont/engine/efficientat"),
         ("vidaudcont/engine/efficientat/LICENSE", "vidaudcont/engine/efficientat")]
datas += collect_data_files("silero_vad")
datas += collect_data_files("faster_whisper")
datas += collect_data_files("speechbrain", include_py_files=True)  # lazily imported modules must exist on disk
binaries = [(os.path.join("bin", f), "bin") for f in os.listdir("bin")]
binaries += collect_dynamic_libs("ctranslate2")
# only the speechbrain parts the accent model is built from (the rest needs optional packages)
hiddenimports = ["speechbrain.lobes.features", "speechbrain.lobes.models.ECAPA_TDNN",
                 "speechbrain.processing.features", "speechbrain.nnet.CNN", "speechbrain.nnet.linear",
                 "speechbrain.nnet.normalization", "speechbrain.nnet.pooling"]

a = Analysis(["run_vidaudcont.py"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=["tkinter", "matplotlib", "IPython", "jupyter", "notebook", "pytest", "transformers",
                       "torch.utils.tensorboard", "tensorboard", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
                       "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia"],
             # speechbrain lists its own package folders at import time -> ship it as plain .py files
             module_collection_mode={"speechbrain": "py"},
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="VidAudCont", console=False, upx=False,
          disable_windowed_traceback=False, argv_emulation=False,
          manifest=open("windows.manifest", encoding="utf-8").read() if sys.platform == "win32" else None)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="VidAudCont")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="VidAudCont.app", bundle_identifier="app.vidaudcont",
                 info_plist={"CFBundleShortVersionString": __version__, "CFBundleVersion": __version__,
                             "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "12.0"})
