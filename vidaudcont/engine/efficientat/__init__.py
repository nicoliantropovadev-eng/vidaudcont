"""Vendored from https://github.com/fschmid56/EfficientAT (MIT licence, see LICENSE): MobileNet AudioSet tagger."""
import csv
import os

_here = os.path.dirname(os.path.abspath(__file__))


def load_labels():
    with open(os.path.join(_here, "class_labels_indices.csv"), newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    return [r[2] for r in rows[1:]]
