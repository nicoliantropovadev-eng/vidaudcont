import pytest


@pytest.fixture(autouse=True)
def no_youtube_lengths(monkeypatch):
    """No test asks YouTube for a video's length (tests that need lengths set their own)."""
    from vidaudcont import matching
    monkeypatch.setattr(matching, "video_length", lambda video_id: None)
