"""JSON metadata is replaced while someone else may have the file open.

Windows refuses to replace a file that another handle has open (a reader, a virus scanner) and
refuses one of two replaces that hit the same file at the same moment. Left alone, that turned a
finished TensorFlow Lite conversion into "Conversion worker failed: PermissionError": the worker's
heartbeat thread and its progress callback both rewrite progress.json, and the parent reads it.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

from nuteach import conversion_worker as worker
from nuteach import utils


def _denied(path: object) -> PermissionError:
    return PermissionError(13, "Access is denied", str(path))


# --------------------------------------------------------------------------- atomic_write_json
def test_atomic_write_json_waits_for_the_file_to_be_let_go(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "project.json"
    utils.atomic_write_json(target, {"n": 1})
    replace = os.replace
    refusals = {"left": 3}

    def busy_at_first(source: str, destination: Path) -> None:
        if refusals["left"]:
            refusals["left"] -= 1
            raise _denied(destination)
        replace(source, destination)

    monkeypatch.setattr(utils.os, "replace", busy_at_first)
    monkeypatch.setattr(utils, "_REPLACE_RETRY_DELAYS", (0, 0, 0, 0))

    utils.atomic_write_json(target, {"n": 2})

    assert refusals["left"] == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {"n": 2}
    assert [entry.name for entry in tmp_path.iterdir()] == ["project.json"]


def test_atomic_write_json_gives_up_in_the_end_and_keeps_the_old_file(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "project.json"
    utils.atomic_write_json(target, {"n": 1})
    attempts = []

    def always_busy(source: str, destination: Path) -> None:
        attempts.append(source)
        raise _denied(destination)

    monkeypatch.setattr(utils.os, "replace", always_busy)
    monkeypatch.setattr(utils, "_REPLACE_RETRY_DELAYS", (0, 0))

    with pytest.raises(PermissionError):
        utils.atomic_write_json(target, {"n": 2})

    assert len(attempts) == 3  # every delay, then one last try
    # Still the complete old file, and no temporary file left next to it.
    assert json.loads(target.read_text(encoding="utf-8")) == {"n": 1}
    assert [entry.name for entry in tmp_path.iterdir()] == ["project.json"]


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to replace an open file")
def test_atomic_write_json_survives_a_reader_that_has_the_file_open(tmp_path: Path) -> None:
    target = tmp_path / "progress.json"
    utils.atomic_write_json(target, {"n": 0})
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    opened = threading.Event()

    def reader() -> None:
        with target.open("r", encoding="utf-8"):
            opened.set()
            time.sleep(0.15)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert opened.wait(5)
        with pytest.raises(PermissionError):  # what Windows says while the reader holds it
            os.replace(other, target)
        utils.atomic_write_json(target, {"n": 1})
    finally:
        thread.join()

    assert json.loads(target.read_text(encoding="utf-8")) == {"n": 1}


# --------------------------------------------------------------------------- the worker's heartbeat
def test_progress_heartbeat_never_raises_when_its_file_cannot_be_written(
    tmp_path: Path, monkeypatch
) -> None:
    def locked(path: Path, payload: object) -> None:
        raise _denied(path)

    monkeypatch.setattr(worker, "atomic_write_json", locked)
    heartbeat = worker.ProgressHeartbeat(tmp_path / "progress.json")

    heartbeat.start()
    heartbeat.update(0.5, "half way")
    heartbeat.finish("completed", "done")
    heartbeat.close()

    assert heartbeat.state == "completed"
    assert heartbeat.value == 1.0


def test_progress_heartbeat_writes_one_at_a_time(tmp_path: Path, monkeypatch) -> None:
    counter = threading.Lock()
    writers = {"now": 0, "most": 0}

    def slow_write(path: Path, payload: object) -> None:
        with counter:
            writers["now"] += 1
            writers["most"] = max(writers["most"], writers["now"])
        time.sleep(0.005)
        with counter:
            writers["now"] -= 1

    monkeypatch.setattr(worker, "atomic_write_json", slow_write)
    heartbeat = worker.ProgressHeartbeat(tmp_path / "progress.json")

    def progress_calls() -> None:
        for step in range(10):
            heartbeat.update(step / 10, "converting")

    threads = [threading.Thread(target=progress_calls) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert writers["most"] == 1


def test_a_finished_conversion_is_not_failed_by_its_progress_file(
    tmp_path: Path, monkeypatch
) -> None:
    """The exit code and result.json are what the parent trusts; progress.json is only a display."""

    representative = tmp_path / "representative.npz"
    np.savez_compressed(representative, samples=np.zeros((2, 4), dtype=np.float32))
    keras_path = tmp_path / "model.keras"
    keras_path.write_bytes(b"stands in for a trained model")
    result_path = tmp_path / "result.json"
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(
        json.dumps({
            "keras_path": str(keras_path),
            "representative_path": str(representative),
            "output_dir": str(tmp_path / "models"),
            "prefix": "demo",
            "selected": ["int8"],
            "progress_path": str(tmp_path / "progress.json"),
            "result_path": str(result_path),
        }),
        encoding="utf-8",
    )
    keras = types.SimpleNamespace(models=types.SimpleNamespace(load_model=lambda *_, **__: object()))
    monkeypatch.setitem(sys.modules, "tensorflow", types.SimpleNamespace(keras=keras))

    def converted(model, representatives, output_dir, prefix, selected, *, progress, **_):
        progress(0.5, "converting")
        return {"int8": "demo_int8.tflite"}, {"models": {}}

    monkeypatch.setattr(worker, "convert_selected", converted)
    write = worker.atomic_write_json

    def progress_file_is_locked(path: Path, payload: object) -> None:
        if Path(path).name == "progress.json":
            raise _denied(path)
        write(path, payload)

    monkeypatch.setattr(worker, "atomic_write_json", progress_file_is_locked)

    assert worker.main([str(spec_path)]) == 0

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["artifacts"] == {"int8": "demo_int8.tflite"}
