"""Background-noise mixing for Audio (KWS) training.

The mixing rule itself is a pure numpy function so it is pinned here without TensorFlow; the
end-to-end behaviour (copies added, honest skip without a usable background class, and the
report fields) runs a one-epoch training and is marked slow.
"""

from __future__ import annotations

import numpy as np
import pytest

from nuteach import audio_pipeline
from nuteach.config import (
    AUDIO_DEFAULTS,
    AUDIO_NOISE_MIX_COPIES,
    AUDIO_NOISE_MIX_SNR_DB,
    validate_audio_settings,
)

SR = 16000


def _tone(seconds: float = 1.0, hz: float = 440.0, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (amplitude * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def _snr_db(clean: np.ndarray, mixed: np.ndarray) -> float:
    added = mixed - clean
    return 10.0 * np.log10(float(np.mean(clean**2)) / max(1e-12, float(np.mean(added**2))))


def test_mix_background_hits_the_requested_snr() -> None:
    rng = np.random.default_rng(3)
    clip = _tone()
    noise = rng.normal(0.0, 0.05, SR * 4).astype(np.float32)
    for wanted in (5.0, 10.0, 20.0):
        mixed = audio_pipeline.mix_background(clip, noise, wanted, np.random.default_rng(1))
        assert mixed.shape == clip.shape and mixed.dtype == np.float32
        assert abs(_snr_db(clip, mixed) - wanted) < 0.5, wanted


def test_mix_background_is_deterministic_per_seed_and_varies_across_seeds() -> None:
    clip = _tone()
    noise = np.random.default_rng(3).normal(0.0, 0.05, SR * 4).astype(np.float32)
    first = audio_pipeline.mix_background(clip, noise, 10.0, np.random.default_rng(7))
    again = audio_pipeline.mix_background(clip, noise, 10.0, np.random.default_rng(7))
    other = audio_pipeline.mix_background(clip, noise, 10.0, np.random.default_rng(8))
    assert np.array_equal(first, again)
    assert not np.array_equal(first, other)  # a different slice of the noise pool


def test_mix_background_never_exceeds_full_scale() -> None:
    clip = _tone(amplitude=0.9)
    noise = np.random.default_rng(3).normal(0.0, 0.5, SR * 2).astype(np.float32)
    mixed = audio_pipeline.mix_background(clip, noise, 0.0, np.random.default_rng(1))
    assert float(np.max(np.abs(mixed))) <= 0.999 + 1e-6


def test_mix_background_with_nothing_to_mix_returns_the_clip() -> None:
    clip = _tone()
    rng = np.random.default_rng(1)
    assert np.array_equal(audio_pipeline.mix_background(clip, np.zeros(0), 10.0, rng), clip)
    # Digital silence as noise (what a board that is not a microphone records) must not
    # produce NaN or blow up: zero times any scale is still zero.
    silent = audio_pipeline.mix_background(clip, np.zeros(SR), 10.0, rng)
    assert np.all(np.isfinite(silent)) and np.allclose(silent, clip)


def test_mix_background_tiles_a_short_noise_pool() -> None:
    clip = _tone()
    short_noise = np.random.default_rng(3).normal(0.0, 0.05, SR // 5).astype(np.float32)
    mixed = audio_pipeline.mix_background(clip, short_noise, 10.0, np.random.default_rng(1))
    assert mixed.shape == clip.shape and abs(_snr_db(clip, mixed) - 10.0) < 0.5


def test_noise_mix_defaults_and_constants() -> None:
    assert AUDIO_DEFAULTS["noise_mix"] is True
    assert AUDIO_NOISE_MIX_COPIES >= 1
    low, high = AUDIO_NOISE_MIX_SNR_DB
    # The board was measured at roughly 11-19 dB SNR; the range must bracket that and stay
    # positive (at 0 dB or below the keyword itself is buried).
    assert 0.0 < low < 11.0 and 19.0 < high <= 30.0
    settings = validate_audio_settings({"noise_mix": "false"})
    assert settings["noise_mix"] is False
    assert validate_audio_settings({})["noise_mix"] is True


def _wav(signal: np.ndarray) -> bytes:
    from nuteach.audio_frontend import encode_wav_bytes

    return encode_wav_bytes(signal.astype(np.float32), SR)


def _seed_audio_project(store, *, silent_background: bool) -> str:
    rng = np.random.default_rng(11)
    project = store.create_project("audio", "Noise mix")
    raw = store.get_raw_project(project["id"])
    for class_index, class_item in enumerate(raw["classes"]):
        for take in range(3):
            if class_index == 0:  # "Background Noise"
                signal = (
                    np.zeros(SR, dtype=np.float32)
                    if silent_background
                    else rng.normal(0.0, 0.01, SR).astype(np.float32)
                )
            else:
                signal = _tone(hz=300.0 + 200.0 * class_index) + rng.normal(0.0, 0.002, SR)
            store.add_audio_bytes(
                project["id"],
                class_item["id"],
                _wav(signal),
                source_name=f"take{take}.wav",
                recording_session_id=f"{class_item['id']}-s{take}",
            )
    return project["id"]


@pytest.mark.slow
def test_train_adds_noisy_copies_only_for_training_keywords(store) -> None:
    project_id = _seed_audio_project(store, silent_background=False)
    result = audio_pipeline.train_audio_project(
        store, project_id, {"epochs": 1, "minimum_samples_per_class": 2}
    )
    settings = result["report"]["settings"]
    assert settings["noise_mix_requested"] is True
    assert settings["noise_mix_applied"] is True
    assert settings["noise_mix_copies"] == AUDIO_NOISE_MIX_COPIES
    assert settings["noise_mix_snr_db"] == list(AUDIO_NOISE_MIX_SNR_DB)
    # Every training keyword clip gets exactly AUDIO_NOISE_MIX_COPIES copies; background
    # clips get none, and validation clips are never touched.
    dataset = result["report"]["dataset"]
    from nuteach.audio_pipeline import split_audio_clips

    # Same inputs as train_audio_project() used (default split, session-disjoint), so this
    # reproduces exactly the train/validation partition it trained on.
    class_count = len(store.get_raw_project(project_id)["classes"])
    split, _ = split_audio_clips(
        store.audio_clip_refs(project_id), 0.2, True, class_count=class_count
    )
    train_background = sum(1 for label in split.train_labels if label == 0)
    train_keywords = len(split.train_paths) - train_background
    assert dataset["train_samples"] == len(split.train_paths)
    assert settings["noise_mix_added"] == AUDIO_NOISE_MIX_COPIES * train_keywords
    assert settings["noise_mix_sources"] == train_background > 0


@pytest.mark.slow
def test_train_skips_mixing_when_background_is_silent_or_disabled(store) -> None:
    silent = _seed_audio_project(store, silent_background=True)
    result = audio_pipeline.train_audio_project(
        store, silent, {"epochs": 1, "minimum_samples_per_class": 2}
    )
    settings = result["report"]["settings"]
    assert settings["noise_mix_requested"] is True
    assert settings["noise_mix_applied"] is False
    assert settings["noise_mix_added"] == 0 and settings["noise_mix_sources"] == 0

    normal = _seed_audio_project(store, silent_background=False)
    result = audio_pipeline.train_audio_project(
        store, normal, {"epochs": 1, "minimum_samples_per_class": 2, "noise_mix": False}
    )
    settings = result["report"]["settings"]
    assert settings["noise_mix_requested"] is False
    assert settings["noise_mix_applied"] is False and settings["noise_mix_added"] == 0
