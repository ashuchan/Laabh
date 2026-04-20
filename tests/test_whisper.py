"""Tests for whisper pipeline components."""
from __future__ import annotations

import pytest

from src.whisper_pipeline.stream_recorder import StreamRecorder


def test_cleanup_returns_zero_when_no_dir(tmp_path, monkeypatch):
    """cleanup_old_chunks returns 0 when audio directory doesn't exist."""
    import src.whisper_pipeline.stream_recorder as sr
    monkeypatch.setattr(sr, "AUDIO_BASE", tmp_path / "nonexistent")
    count = StreamRecorder.cleanup_old_chunks(days=7)
    assert count == 0


def test_cleanup_deletes_old_files(tmp_path, monkeypatch):
    """cleanup_old_chunks removes files older than `days`."""
    import time
    import src.whisper_pipeline.stream_recorder as sr
    monkeypatch.setattr(sr, "AUDIO_BASE", tmp_path)

    # Create a fake old chunk
    old_file = tmp_path / "chunk_old.webm"
    old_file.touch()
    # Set mtime to 10 days ago
    old_mtime = time.time() - 10 * 86400
    import os
    os.utime(old_file, (old_mtime, old_mtime))

    # Create a recent chunk
    recent_file = tmp_path / "chunk_recent.webm"
    recent_file.touch()

    count = StreamRecorder.cleanup_old_chunks(days=7)
    assert count == 1
    assert not old_file.exists()
    assert recent_file.exists()


def test_vod_downloader_extract_video_id():
    from src.whisper_pipeline.vod_downloader import VODDownloader
    dl = VODDownloader()
    assert dl._extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert dl._extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert dl._extract_video_id("https://youtube.com/live/abc123") == "abc123"
    assert dl._extract_video_id("https://example.com") is None
