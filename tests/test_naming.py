"""Row numbers in file names against the sheet: files recognised by the link or title inside them."""
import os
import subprocess

from vidaudcont import cutter, naming, resources

ROWS = [{"row": 80, "id": "AAAAAAAAAA1"}, {"row": 81, "id": "BBBBBBBBBB2"}, {"row": 82, "id": "CCCCCCCCCC3"},
        {"row": 90, "id": "AAAAAAAAAA1"}]                        # row 90 repeats the video of row 80
TITLES = {"AAAAAAAAAA1": {"ok": True, "orig": "Taking a history", "ru": "Сбор анамнеза"},
          "BBBBBBBBBB2": {"ok": True, "orig": "7 Tips for breaking bad news", "ru": "7 советов"},
          "CCCCCCCCCC3": {"ok": True, "orig": "Abdominal examination", "ru": "Осмотр живота"}}


def make(path, **tags):
    meta = [x for k, v in tags.items() for x in ("-metadata", f"{k}={v}")]
    subprocess.run([resources.tool("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=2",
                    "-c:a", "aac"] + meta + [str(path)], check=True)


def test_names_are_checked_by_what_is_inside_the_files(tmp_path):
    make(tmp_path / "81.m4a", title="Taking a history")                        # really row 80
    make(tmp_path / "80.m4a", title="7 Tips for breaking bad news")            # really row 81: a swap
    make(tmp_path / "82.m4a", comment="https://www.youtube.com/watch?v=CCCCCCCCCC3", title="whatever")  # right
    make(tmp_path / "7.m4a", title="Abdominal examination")                    # the "7 Tips" mistake: row 82
    make(tmp_path / "90. Сбор анамнеза.m4a")                                   # the name tells: first row, 80
    make(tmp_path / "95.m4a", title="Совсем другое видео")                    # not in the sheet
    make(tmp_path / "без номера.m4a", title="Taking a history")               # no number: not touched
    plan = {x["name"]: x for x in naming.check_folder(str(tmp_path), ROWS, TITLES)}
    assert set(plan) == {"81.m4a", "80.m4a", "82.m4a", "7.m4a", "90. Сбор анамнеза.m4a", "95.m4a"}
    assert (plan["81.m4a"]["row"], plan["81.m4a"]["new"]) == (80, "80.m4a")
    assert (plan["80.m4a"]["row"], plan["80.m4a"]["new"]) == (81, "81.m4a")
    assert plan["82.m4a"]["new"] is None and "ссылка" in plan["82.m4a"]["how"]
    assert plan["7.m4a"]["new"] == "82 (2).m4a"                                # 82.m4a is taken by the right file
    assert plan["90. Сбор анамнеза.m4a"]["new"] == "80. Сбор анамнеза.m4a"
    assert plan["95.m4a"]["row"] is None and plan["95.m4a"]["new"] is None
    done = naming.rename(str(tmp_path), plan.values())
    assert len(done) == 4
    names = sorted(os.listdir(tmp_path))
    assert names == sorted(["80.m4a", "81.m4a", "82.m4a", "82 (2).m4a", "80. Сбор анамнеза.m4a", "95.m4a",
                            "без номера.m4a"])
    assert naming.file_tags(str(tmp_path / "80.m4a"))["title"] == "Taking a history"
    assert naming.file_tags(str(tmp_path / "81.m4a"))["title"].startswith("7 Tips")


def test_cut_files_carry_the_video_link(tmp_path):
    src = tmp_path / "Taking a history.m4a"
    make(src, title="Taking a history")
    rep = cutter.cut(str(src), [(0.5, 1.0)], out_path=str(tmp_path / "80.m4a"),
                     tags={"comment": "https://www.youtube.com/watch?v=AAAAAAAAAA1"})
    assert rep["verification"]["lossless"]
    tags = naming.file_tags(str(tmp_path / "80.m4a"))
    assert tags["comment"].endswith("AAAAAAAAAA1") and tags["title"] == "Taking a history"
    make(tmp_path / "81.m4a", comment="https://www.youtube.com/watch?v=AAAAAAAAAA1")
    assert naming.identify(str(tmp_path / "81.m4a"), ROWS, TITLES)[0] == "AAAAAAAAAA1"
