"""Adjustable clip length for the audio (KWS) kind: 1 / 1.5 / 2 / 3 / 4 / 5 seconds.

The ceiling is a measurement, not a guess: the KWS firmware's audio scratch buffers share the
1 MiB SRAM01 window with the Ethos-U tensor arena, and at 5 s the two together already use
~892 KB on every board (see config.AUDIO_CLIP_SECONDS_CHOICES). Everything below runs
without TensorFlow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nuteach.audio_frontend import AudioFrontendConfig, config_from_mapping
from nuteach.config import (
    AUDIO_CLIP_SECONDS_CHOICES,
    AUDIO_DEFAULTS,
    MCU_AUDIO_FRONTEND_LOCK,
    validate_audio_settings,
)
from nuteach.mcu import boards, contract, deploy_service, kws_codegen, vela
from nuteach.mcu.errors import DeployError
from nuteach.project_store import ProjectError, ProjectStore


def _frontend(clip_seconds: float, mel_bins: int = 40) -> dict:
    config = config_from_mapping({**AUDIO_DEFAULTS, "clip_seconds": clip_seconds, "mel_bins": mel_bins})
    return {
        "sample_rate": config.sample_rate,
        "clip_seconds": config.clip_seconds,
        "window_ms": config.window_ms,
        "hop_ms": config.hop_ms,
        "fft_size": config.fft_size,
        "mel_bins": config.mel_bins,
        "fmin": config.fmin,
        "fmax": config.fmax,
        "db_floor": config.db_floor,
        "frame_count": config.frame_count,
        "feature_shape": list(config.feature_shape),
    }


def _audio_contract(tmp_path: Path, clip_seconds: float, *, board: str = "NuGestureAI-M55M1") -> contract.DeployContract:
    frontend = _frontend(clip_seconds)
    return contract.DeployContract(
        kind="audio",
        application="kws",
        board=boards.board_for(board),
        project_id="p",
        project_name="n",
        labels=["Background Noise", "yes", "no"],
        models_dir=tmp_path,
        int8_path=tmp_path / "m.tflite",
        model_sha256="x",
        input=contract.TensorInfo("in", (1, frontend["frame_count"], 40, 1), "int8", 1 / 255, -128),
        output=contract.TensorInfo("out", (1, 3), "int8", 1 / 256, -128),
        image_size=None,
        audio_frontend=frontend,
        yamnet_frontend=None,
        encoder_depth=None,
        detection_threshold=0.6,
        class_thresholds=[0.6] * 3,
        hop_seconds=0.5,
        clip_seconds=clip_seconds,
        peak_hold_seconds=0.0,
        kws_runtime=dict(contract.DEFAULT_KWS_RUNTIME),
    )


def test_choices_are_the_measured_safe_set() -> None:
    assert AUDIO_CLIP_SECONDS_CHOICES == (1.0, 1.5, 2.0, 3.0, 4.0, 5.0)
    assert AUDIO_DEFAULTS["clip_seconds"] in AUDIO_CLIP_SECONDS_CHOICES
    # The lock still pins the spectrogram geometry; the window length is a choice instead.
    assert "clip_seconds" not in MCU_AUDIO_FRONTEND_LOCK
    assert set(MCU_AUDIO_FRONTEND_LOCK) == {
        "sample_rate", "window_ms", "hop_ms", "fft_size", "fmin", "fmax", "db_floor",
    }


@pytest.mark.parametrize("target", ["pc", "NuGestureAI-M55M1", "NuMaker-M55M1", "NuMaker-VoiceAI-M55M1"])
@pytest.mark.parametrize("clip_seconds", AUDIO_CLIP_SECONDS_CHOICES)
def test_validate_accepts_every_choice_on_every_target(target: str, clip_seconds: float) -> None:
    result = validate_audio_settings({"clip_seconds": clip_seconds, "deployment_target": target})
    assert result["clip_seconds"] == clip_seconds
    assert isinstance(result["clip_seconds"], float)


def test_validate_coerces_a_numeric_string() -> None:
    result = validate_audio_settings({"clip_seconds": "2", "deployment_target": "pc"})
    assert result["clip_seconds"] == 2.0 and isinstance(result["clip_seconds"], float)


@pytest.mark.parametrize("bad", [0.5, 6.0, 10.0, 2.25, "abc"])
def test_validate_rejects_lengths_outside_the_choices(bad: object) -> None:
    with pytest.raises(ValueError, match="取樣長度"):
        validate_audio_settings({"clip_seconds": bad, "deployment_target": "NuGestureAI-M55M1"})


def test_update_project_refuses_a_clip_change_once_samples_exist(store: ProjectStore, wav_bytes: bytes) -> None:
    project = store.create_project("audio", "Clips")
    pid = project["id"]
    class_id = project["classes"][0]["id"]
    # No samples yet: free to choose.
    assert store.update_project(pid, {"settings": {"clip_seconds": 2.0}})["settings"]["clip_seconds"] == 2.0
    # The 2 s fixture tone becomes exactly one 2 s clip.
    samples = store.add_audio_bytes(pid, class_id, wav_bytes, source_name="tone.wav")
    assert len(samples) == 1 and samples[0]["duration_seconds"] == 2.0
    with pytest.raises(ProjectError, match="取樣長度"):
        store.update_project(pid, {"settings": {"clip_seconds": 3.0}})
    # Re-sending the stored value (also as the string the form posts) is not a change.
    store.update_project(pid, {"settings": {"clip_seconds": 2.0}})
    store.update_project(pid, {"settings": {"clip_seconds": "2"}})
    assert store.get_project(pid)["settings"]["clip_seconds"] == 2.0
    # Other settings still save while the clip length is frozen.
    assert store.update_project(pid, {"settings": {"epochs": 7}})["settings"]["epochs"] == 7
    # Once the samples are gone the length is free again.
    store.delete_sample(pid, samples[0]["id"])
    assert store.update_project(pid, {"settings": {"clip_seconds": 3.0}})["settings"]["clip_seconds"] == 3.0


def test_known_sound_and_abnormal_sound_stay_at_one_second(store: ProjectStore) -> None:
    for kind in ("known_sound", "abnormal_sound"):
        project = store.create_project(kind, kind)
        with pytest.raises(ProjectError):
            store.update_project(project["id"], {"settings": {"clip_seconds": 2.0}})


def test_kws_tokens_follow_the_clip_length(tmp_path: Path) -> None:
    tokens = kws_codegen.kws_tokens(_audio_contract(tmp_path, 2.0), 4096)
    assert tokens["CLIP_SAMPLES"] == "32000"
    assert tokens["FRAME_COUNT"] == "198"
    assert tokens["INFERENCE_HOP"] == "4000"
    assert tokens["COOLDOWN_HOPS"] == "8"  # one full clip: 32000 / 4000
    assert tokens["SMOOTH_WINDOWS"] == "4"  # absolute (1 s of history), not clip-relative


def test_sram_buffer_bytes_match_the_firmware_layout() -> None:
    one = kws_codegen.kws_sram_buffer_bytes(AudioFrontendConfig())
    assert one == 84_832  # 2 x 32,000 ring/clip + 15,680 features + 2 x 2,048 fft + 1,056 power
    for seconds in (2.0, 3.0, 5.0):
        got = kws_codegen.kws_sram_buffer_bytes(AudioFrontendConfig(clip_seconds=seconds))
        assert got == one + round((seconds - 1.0) * 80_000)
    assert kws_codegen.kws_sram_buffer_bytes(AudioFrontendConfig(mel_bins=64)) == 84_832 + 98 * 24 * 4


def test_contract_accepts_choices_and_rejects_the_rest(tmp_path: Path) -> None:
    for clip_seconds in AUDIO_CLIP_SECONDS_CHOICES:
        contract.validate(_audio_contract(tmp_path, clip_seconds))
    with pytest.raises(DeployError, match="取樣長度"):
        contract.validate(_audio_contract(tmp_path, 6.0))
    # The model input must still match the frame count the front-end promises.
    wrong = _audio_contract(tmp_path, 2.0)
    wrong.audio_frontend["frame_count"] = 98
    with pytest.raises(DeployError, match="輸入形狀"):
        contract.validate(wrong)


def test_arena_budget_shrinks_with_the_audio_buffers(tmp_path: Path) -> None:
    board = boards.board_for("NuGestureAI-M55M1")
    assert deploy_service.arena_budget_bytes(_audio_contract(tmp_path, 1.0), board) == vela.DEFAULT_ARENA_BUDGET_BYTES
    five = deploy_service.arena_budget_bytes(_audio_contract(tmp_path, 5.0), board)
    assert five == board.sram01_bytes - 404_832 - deploy_service.AUDIO_SRAM01_RESERVE_BYTES
    # Measured with the bundled Vela: the 5 s Performance arena (485,376 B) still fits this
    # budget, so a 5 s build keeps the fast schedule on every board.
    assert 485_376 <= five
    # Non-audio kinds are untouched.
    image = _audio_contract(tmp_path, 1.0)
    object.__setattr__(image, "kind", "image")
    assert deploy_service.arena_budget_bytes(image, board) == vela.DEFAULT_ARENA_BUDGET_BYTES


def test_shrink_advice_for_audio_names_the_clip_length() -> None:
    advice = deploy_service._shrink_advice("audio", boards.board_for("NuMaker-VoiceAI-M55M1"))
    assert "取樣長度" in advice and "clip_seconds" in advice
