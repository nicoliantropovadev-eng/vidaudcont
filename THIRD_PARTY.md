# Third-party components

| Component | Use | Licence |
|---|---|---|
| [FFmpeg](https://ffmpeg.org) (Windows: gyan.dev essentials build; macOS: ffmpeg.martin-riedl.de build) | decoding, lossless cutting | GPL v3 builds (source: ffmpeg.org) |
| [Silero VAD](https://github.com/snakers4/silero-vad) | speech detection | MIT |
| [EfficientAT](https://github.com/fschmid56/EfficientAT) (vendored in `vidaudcont/engine/efficientat`, weights `mn10_as`) | music / sound event tagging (AudioSet) | MIT |
| [CommonAccent ECAPA](https://huggingface.co/Jzuluaga/accent-id-commonaccent_ecapa) + [SpeechBrain](https://speechbrain.github.io) | English accent identification | MIT / Apache 2.0 |
| [SpeechBrain x-vector, VoxCeleb](https://huggingface.co/speechbrain/spkrec-xvect-voxceleb) | telling voices apart: conversation or lecture | Apache 2.0 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [Whisper base.en](https://huggingface.co/Systran/faster-whisper-base.en) | transcript excerpts for the topic hint | MIT |
| [PyTorch](https://pytorch.org), torchaudio | model runtime | BSD |
| [Qt for Python (PySide6)](https://www.qt.io/qt-for-python) | user interface | LGPL v3 |
| `assets/selftest.m4a` | self-test clip | contains excerpts of OpenSLR 83 (CC BY-SA 4.0) and Kevin MacLeod music (CC BY 4.0) |
