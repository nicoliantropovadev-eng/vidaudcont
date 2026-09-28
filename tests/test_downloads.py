import os
import shutil
import subprocess

from vidaudcont import resources
from vidaudcont.downloads import FolderWatcher, select_links, write_links

ROWS = [{"row": 5, "id": "4Y3EGPjhiXE", "cuts": "start-0:13"},
        {"row": 80, "id": "iuAMFrjiKXg", "cuts": ""},
        {"row": 81, "id": "Wb7VTTNMhyM", "cuts": ""},
        {"row": 82, "id": "iuAMFrjiKXg", "cuts": ""},    # same video twice -> one link
        {"row": 83, "id": "0OzB_bqOAps", "cuts": ""}]


def test_select_links():
    got = select_links(ROWS, 1, 1000)
    assert got == [(80, "https://www.youtube.com/watch?v=iuAMFrjiKXg"), (81, "https://www.youtube.com/watch?v=Wb7VTTNMhyM"),
                   (83, "https://www.youtube.com/watch?v=0OzB_bqOAps")]
    assert [r for r, _ in select_links(ROWS, 1, 1000, skip_filled=False)] == [5, 80, 81, 83]
    assert [r for r, _ in select_links(ROWS, 81, 82)] == [81]  # 82 repeats row 80, even outside the range
    assert [r for r, _ in select_links(ROWS, 1, 1000, skip_rows={81})] == [80, 83]
    marked = ROWS + [{"row": 84, "id": "ZZZZZZZZZZZ", "cuts": "", "note": "НЕ РАЗГОВОР"}]
    assert 84 not in [r for r, _ in select_links(marked, 1, 1000)]      # reason already in column E


def test_repeats_and_red_rows_are_left_out():
    from vidaudcont.downloads import repeats
    from vidaudcont.sheet import is_red
    rows = [{"row": 5, "id": "AAAAAAAAAAA", "cuts": "start-0:13", "note": ""},          # done
            {"row": 80, "id": "AAAAAAAAAAA", "cuts": "", "note": ""},                    # the same video again
            {"row": 81, "id": "BBBBBBBBBBB", "cuts": "", "note": "", "bg": ["#f4cccc"]},  # light red fill
            {"row": 82, "id": "CCCCCCCCCCC", "cuts": "", "note": "", "fg": "#ff0000"},    # red link text
            {"row": 83, "id": "DDDDDDDDDDD", "cuts": "", "note": "", "bg": ["#fff2cc"]},  # yellow: not skipped
            {"row": 84, "id": "DDDDDDDDDDD", "cuts": "", "note": ""}]
    stats = {}
    assert select_links(rows, 1, 1000, stats=stats) == [(83, "https://www.youtube.com/watch?v=DDDDDDDDDDD")]
    assert stats == {"red": 2, "repeat": 2, "filled": 1, "have": 0}
    assert repeats(rows) == {80: 5, 84: 83}
    for c in ("#ff0000", "#ea4335", "#cc0000", "#660000", "#e06666", "#ea9999", "#f4cccc", "#dd7e6b"):
        assert is_red(c), c
    for c in ("#ffffff", "#000000", "#ff9900", "#fff2cc", "#d9ead3", "#cfe2f3", "#ead1dc", "#ff00ff", "", None):
        assert not is_red(c), c


def test_write_links(tmp_path):
    p = tmp_path / "ссылки.txt"
    write_links(str(p), select_links(ROWS, 1, 1000))
    assert p.read_bytes().decode("utf-8").split() == [
        "https://www.youtube.com/watch?v=iuAMFrjiKXg", "https://www.youtube.com/watch?v=Wb7VTTNMhyM",
        "https://www.youtube.com/watch?v=0OzB_bqOAps"]


def test_watcher_waits_until_the_download_is_complete(tmp_path):
    w = FolderWatcher(str(tmp_path))
    part = tmp_path / "Клинический разговор.m4a"
    part.write_bytes(open(resources.asset("selftest.m4a"), "rb").read()[:20000])  # half-written file
    assert w.scan() == []            # first look
    assert w.scan() == []            # stable size, but not a readable file yet
    part.unlink()
    shutil.copy(resources.asset("selftest.m4a"), part)  # the finished download
    assert w.scan() == []            # changed since the last look
    assert w.scan() == [str(part)]   # stable and complete
    assert w.scan() == []            # handed over only once
    (tmp_path / "notes.txt").write_text("x")
    sub = tmp_path / "готово"
    sub.mkdir()
    shutil.copy(resources.asset("selftest.m4a"), sub / "80.m4a")
    assert w.scan() == [] and w.scan() == []  # other files and sub-folders are ignored


def test_known_files_are_skipped(tmp_path):
    ff = resources.tool("ffmpeg")
    f = tmp_path / "a.m4a"
    subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "sine=duration=3", "-c:a", "aac", str(f)], check=True)
    w = FolderWatcher(str(tmp_path))
    w.scan(known={str(f)})
    assert w.scan(known={str(f)}) == []
    assert os.path.exists(f)


def test_known_files_are_not_opened_again_whatever_the_spelling(tmp_path, monkeypatch):
    from vidaudcont import downloads
    opened = []
    monkeypatch.setattr(downloads.cutter, "probe", lambda p: opened.append(p) or {"duration": 1.0})
    for n in ("a.m4a", "b.m4a"):
        (tmp_path / n).write_bytes(b"x" * 10)
    w = FolderWatcher(str(tmp_path) + "/./")          # the folder as a dialog may spell it
    known = {os.path.abspath(tmp_path / "a.m4a")}     # the file list spells it differently
    w.scan(known)
    assert w.scan(known) == [os.path.abspath(tmp_path / "b.m4a")]
    assert opened == [os.path.abspath(tmp_path / "b.m4a")]
