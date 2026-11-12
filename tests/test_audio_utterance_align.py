"""One press = one word: utterance detection and centring for keyword classes (audio kind).

A take recorded from the moment the button is pressed contains the student's reaction time,
the word, and trailing room sound. The server must find the word by its energy and centre it
in one clip -- never slice on a fixed grid, never keep a silent take as a "keyword", and never
touch the Background class, whose silence is the content. Runs without TensorFlow.
"""

from __future__ import annotations

import io
import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from nuteach.audio_frontend import (
    UTTERANCE_MIN_PEAK_DBFS,
    center_utterance,
    encode_wav_bytes,
    find_utterance,
    read_wav_bytes,
)
from nuteach.project_store import AUDIO_ALIGN_MODES, ProjectError, ProjectStore

RATE = 16000


def _take(word_start: float, word_length: float, total: float, *, level: float = 0.3, noise: float = 0.002) -> np.ndarray:
    """Room noise with one 440 Hz 'word' (with a short plosive gap) at `word_start`."""
    rng = np.random.default_rng(7)
    signal = (rng.standard_normal(int(total * RATE)) * noise).astype(np.float32)
    start = int(word_start * RATE)
    length = int(word_length * RATE)
    t = np.arange(length) / RATE
    word = level * np.sin(2 * math.pi * 440.0 * t).astype(np.float32)
    # A 60 ms silent gap one third of the way in, like the stop in "nu-vo-ton".
    gap = slice(length // 3, length // 3 + int(0.06 * RATE))
    word[gap] = 0.0
    signal[start : start + length] += word
    return signal


def _energy_centroid(clip: np.ndarray) -> float:
    energy = clip.astype(np.float64) ** 2
    return float((np.arange(clip.size) * energy).sum() / max(energy.sum(), 1e-12) / RATE)


def test_find_utterance_locates_the_word_and_bridges_the_gap() -> None:
    span = find_utterance(_take(1.2, 0.45, 2.0), RATE)
    assert span is not None
    assert abs(span.start / RATE - 1.2) <= 0.02
    assert abs(span.end / RATE - 1.65) <= 0.02  # one span, the 60 ms gap did not split it
    assert span.peak_dbfs > -20 and span.noise_floor_dbfs < -40


def test_find_utterance_refuses_silence_and_noise_only() -> None:
    assert find_utterance(np.zeros(RATE, dtype=np.float32), RATE) is None
    rng = np.random.default_rng(1)
    quiet = (rng.standard_normal(RATE * 2) * 0.001).astype(np.float32)  # ~ -60 dBFS
    assert find_utterance(quiet, RATE) is None
    # A loud but flat take (no peak above the floor) is not a word either.
    steady = (rng.standard_normal(RATE * 2) * 0.1).astype(np.float32)
    assert find_utterance(steady, RATE) is None
    assert UTTERANCE_MIN_PEAK_DBFS == -45.0


def test_center_utterance_puts_the_word_in_the_middle_using_the_take_itself() -> None:
    take = _take(1.2, 0.45, 2.0)
    span = find_utterance(take, RATE)
    assert span is not None
    clip, start = center_utterance(take, span, RATE)
    assert clip.shape == (RATE,)
    assert abs(_energy_centroid(clip) - 0.5) <= 0.05
    # The clip is a verbatim window of the take (room sound kept, no zero padding).
    assert np.array_equal(clip, take[start : start + RATE])
    # A word near the end cannot be centred without leaving the take: the window slides back.
    late = _take(1.7, 0.25, 2.0)
    late_span = find_utterance(late, RATE)
    assert late_span is not None
    clip, start = center_utterance(late, late_span, RATE)
    assert start == RATE and clip.shape == (RATE,)


def test_center_utterance_pads_a_take_shorter_than_the_clip() -> None:
    take = _take(0.1, 0.3, 0.6)
    span = find_utterance(take, RATE)
    assert span is not None
    clip, start = center_utterance(take, span, RATE)
    assert clip.shape == (RATE,) and start < 0
    assert np.array_equal(clip[-start : -start + take.size], take)


def test_store_utterance_mode_keeps_exactly_one_centred_clip(store: ProjectStore) -> None:
    project = store.create_project("audio", "Words")
    pid = project["id"]
    class_id = project["classes"][1]["id"]  # [0] is "Background Noise" by name
    take = encode_wav_bytes(_take(1.2, 0.45, 2.0), RATE)
    samples = store.add_audio_bytes(pid, class_id, take, source_name="press.wav", align="utterance")
    assert len(samples) == 1
    sample = samples[0]
    assert sample["alignment"] == "utterance"
    assert sample["duration_seconds"] == 1.0
    assert 0 < sample["utterance_start_sample"] < sample["utterance_end_sample"] <= RATE
    assert sample["utterance_truncated"] is False
    assert sample["utterance_peak_dbfs"] > sample["noise_floor_dbfs"] + 10
    _, stored = read_wav_bytes((store.project_dir(pid) / sample["relative_path"]).read_bytes(), RATE)
    assert abs(_energy_centroid(stored) - 0.5) <= 0.05
    # Grid mode is untouched and says so in its metadata.
    grid = store.add_audio_bytes(pid, class_id, take, source_name="take.wav")
    assert len(grid) == 2 and all(item["alignment"] == "grid" for item in grid)


def test_store_utterance_mode_refuses_silence_and_the_background_class(store: ProjectStore) -> None:
    project = store.create_project("audio", "Words")
    pid = project["id"]
    # The default first class is named "Background Noise"; the name hint alone must refuse
    # utterance mode there, exactly as training and the firmware treat it as background.
    keyword_id, background_id = project["classes"][1]["id"], project["classes"][0]["id"]
    silent = encode_wav_bytes(np.zeros(RATE * 2, dtype=np.float32), RATE)
    with pytest.raises(ProjectError, match="沒有聽到聲音"):
        store.add_audio_bytes(pid, keyword_id, silent, align="utterance")
    # Loud but flat (a hiss, no word above the floor) must not be reported as "nothing heard".
    hiss = np.random.default_rng(5).normal(0.0, 0.1, RATE * 2).astype(np.float32)
    with pytest.raises(ProjectError, match="找不到清楚的一個字"):
        store.add_audio_bytes(pid, keyword_id, encode_wav_bytes(hiss, RATE), align="utterance")
    assert store.get_project(pid)["total_samples"] == 0
    with pytest.raises(ProjectError, match="背景類別"):
        store.add_audio_bytes(pid, background_id, encode_wav_bytes(_take(0.5, 0.4, 2.0), RATE), align="utterance")
    store.update_project(pid, {"settings": {"background_class_id": background_id}})
    with pytest.raises(ProjectError, match="背景類別"):
        store.add_audio_bytes(pid, background_id, encode_wav_bytes(_take(0.5, 0.4, 2.0), RATE), align="utterance")
    with pytest.raises(ProjectError, match="align"):
        store.add_audio_bytes(pid, keyword_id, silent, align="magic")
    assert AUDIO_ALIGN_MODES == ("grid", "utterance")


def test_upload_endpoint_passes_align_through(store: ProjectStore) -> None:
    from nuteach.app import create_app

    client = TestClient(create_app(store=store))
    project = client.post("/api/projects", json={"kind": "audio", "name": "Words"}).json()
    pid, class_id = project["id"], project["classes"][1]["id"]  # [0] is the background class
    wav = encode_wav_bytes(_take(1.2, 0.45, 2.0), RATE)
    ok = client.post(
        f"/api/projects/{pid}/classes/{class_id}/audio",
        files=[("files", ("press.wav", io.BytesIO(wav), "audio/wav"))],
        data={"align": "utterance"},
    )
    assert ok.status_code == 200 and ok.json()["added"] == 1 and ok.json()["errors"] == []
    silent = client.post(
        f"/api/projects/{pid}/classes/{class_id}/audio",
        files=[("files", ("quiet.wav", io.BytesIO(encode_wav_bytes(np.zeros(RATE * 2, dtype=np.float32), RATE)), "audio/wav"))],
        data={"align": "utterance"},
    )
    assert silent.status_code == 200 and silent.json()["added"] == 0
    assert "沒有聽到聲音" in silent.json()["errors"][0]
    bad = client.post(
        f"/api/projects/{pid}/classes/{class_id}/audio",
        files=[("files", ("press.wav", io.BytesIO(wav), "audio/wav"))],
        data={"align": "sideways"},
    )
    assert bad.status_code == 400
