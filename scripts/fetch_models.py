"""Downloads the model files into ./models (pinned revisions, SHA-256 checked)."""
import hashlib
import os
import sys
import urllib.request

HF = "https://huggingface.co/{repo}/resolve/{rev}/{name}"
ACCENT = ("Jzuluaga/accent-id-commonaccent_ecapa", "14bebf44b7e7a34204d0acc2c897935945fb5c51")
WHISPER = ("Systran/faster-whisper-base.en", "3d3d5dee26484f91867d81cb899cfcf72b96be6c")
FILES = [
    ("efficientat/mn10_as_mAP_471.pt",
     "https://github.com/fschmid56/EfficientAT/releases/download/v0.0.1/mn10_as_mAP_471.pt",
     "0bd7dc2443af498c289a2e739f02ebb515d6aa3fd3ab9db539c86123ae368a4e"),
    ("accent-ecapa/embedding_model.ckpt", HF.format(repo=ACCENT[0], rev=ACCENT[1], name="embedding_model.ckpt"),
     "7ffa5ac9c0ec21fd6677fa8a39b3b045f182236f5c312a8edca385dfa79e7e3c"),
    ("accent-ecapa/classifier.ckpt", HF.format(repo=ACCENT[0], rev=ACCENT[1], name="classifier.ckpt"),
     "146a2c6cb236e387b24972797ed9aebb3b54b09b33a072bee87eb3576bd88c01"),
    ("accent-ecapa/accent_encoder.txt", HF.format(repo=ACCENT[0], rev=ACCENT[1], name="accent_encoder.txt"),
     "a74ac219335687eba66ba1a389a6e48cd83897c7efd4692f5fd36c3e04f4ef6c"),
    ("whisper-base.en/model.bin", HF.format(repo=WHISPER[0], rev=WHISPER[1], name="model.bin"),
     "2a166925539a16005f14ff328359f9b9adb9dc4fb631bb3b227526862e93e2ef"),
    ("whisper-base.en/config.json", HF.format(repo=WHISPER[0], rev=WHISPER[1], name="config.json"),
     "f3bc3821e9fc76a27bae538e11ae5b677dcdd352b4600429ce7951d398569aeb"),
    ("whisper-base.en/tokenizer.json", HF.format(repo=WHISPER[0], rev=WHISPER[1], name="tokenizer.json"),
     "929c5252409436dce1b38a75d1abbcb5e132d170d8e324e4e04ed915fa2d22df"),
    ("whisper-base.en/vocabulary.txt", HF.format(repo=WHISPER[0], rev=WHISPER[1], name="vocabulary.txt"),
     "ff77588746d3a2595d32ab5b69ffd7b95ce2441ac57533cb66fc3eb575a115cf"),
]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(dest="models"):
    for rel, url, digest in FILES:
        path = os.path.join(dest, rel)
        if os.path.exists(path) and sha256(path) == digest:
            print("ok (cached)", rel)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        print("download", rel, flush=True)
        req = urllib.request.Request(url, headers={"User-Agent": "vidaudcont-build"})
        with urllib.request.urlopen(req, timeout=300) as r, open(path + ".part", "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        got = sha256(path + ".part")
        if got != digest:
            os.remove(path + ".part")
            sys.exit(f"checksum mismatch for {rel}: {got}")
        os.replace(path + ".part", path)
    print("models ready")


if __name__ == "__main__":
    main(*sys.argv[1:])
