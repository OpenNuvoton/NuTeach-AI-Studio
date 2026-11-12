from __future__ import annotations

import errno
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scripts import install_local
from nuteach.app import create_app
from nuteach.jobs import JobManager
from nuteach.model_runtime import ModelRuntime
from nuteach.path_limits import (
    MAX_PATH_CHARS,
    VENV_DEEPEST_RELATIVE_CHARS,
    describe_error,
    install_problems,
    missing_toolkit_files,
    path_error_hint,
    startup_warnings,
    use_ascii_temp_dir,
)
from nuteach.project_store import ProjectStore

ROOT = Path(__file__).resolve().parents[1]
LONGEST_INSTALLABLE_ROOT = MAX_PATH_CHARS - 1 - VENV_DEEPEST_RELATIVE_CHARS


def _root(length: int) -> str:
    return "C:\\" + "x" * (length - 3)


def test_install_stops_on_a_folder_tensorflow_cannot_open() -> None:
    """Measured: on Windows, SavedModel export and the TFLite Interpreter fail on such a path."""
    problems = install_problems(r"C:\Users\你的名字\OneDrive\桌面\NuTeach AI Studio", long_paths=True)
    assert len(problems) == 1
    assert "非英文" in problems[0] and "C:\\NuTeach AI Studio" in problems[0]


def test_install_stops_where_tensorflow_would_pass_max_path() -> None:
    assert install_problems(_root(LONGEST_INSTALLABLE_ROOT), long_paths=False) == []
    problems = install_problems(_root(LONGEST_INSTALLABLE_ROOT + 1), long_paths=False)
    assert len(problems) == 1 and "太長" in problems[0]
    # With Windows' long-path support switched on, pip and TensorFlow cope with any depth.
    assert install_problems(_root(LONGEST_INSTALLABLE_ROOT + 1), long_paths=True) == []


def test_start_warns_where_firmware_builds_would_be_refused() -> None:
    assert startup_warnings(_root(78)) == []
    warnings = startup_warnings(_root(79))
    assert len(warnings) == 1 and "部署到開發板" in warnings[0]


def test_a_long_missing_path_gets_the_path_length_advice() -> None:
    path = "C:\\" + "d\\" * 120 + "clip.wav"
    hint = path_error_hint(FileNotFoundError(errno.ENOENT, "No such file or directory", path))
    assert hint is not None
    assert f"{len(path)} 字元" in hint and "C:\\NuTeach AI Studio" in hint


def test_filename_too_long_is_advised_whatever_the_path_shown() -> None:
    error = OSError(None, "The filename or extension is too long", None, 206)
    assert "路徑長度上限" in (path_error_hint(error) or "")


def test_a_tensorflow_error_on_a_chinese_path_gets_the_non_ascii_advice() -> None:
    message = (
        r"FailedPreconditionError: C:\Users\你的名字\TM\workspace\projects\p\nuteach_convert_1"
        " is not a directory"
    )
    hint = path_error_hint(RuntimeError(message))
    assert hint is not None and "非英文" in hint and "你的名字" in hint


def test_the_cause_of_a_wrapped_error_is_inspected() -> None:
    try:
        try:
            raise FileNotFoundError(errno.ENOENT, "No such file", "C:\\" + "y" * 250)
        except OSError as inner:
            raise RuntimeError("Conversion worker failed") from inner
    except RuntimeError as outer:
        described = describe_error(outer)
    assert described.startswith("Conversion worker failed")
    assert "路徑長度上限" in described


def test_ordinary_errors_get_no_advice() -> None:
    short = FileNotFoundError(errno.ENOENT, "No such file or directory", r"C:\TM\a.wav")
    assert path_error_hint(short) is None
    assert path_error_hint(ValueError("每個類別至少要 5 個樣本")) is None
    assert describe_error(ValueError("x")) == "x"


def test_the_advice_stays_ahead_of_a_worker_log_tail() -> None:
    error = RuntimeError(
        "Conversion worker failed: C:\\" + "w" * 245 + "\n\nWorker log tail:\nTraceback ..."
    )
    described = describe_error(error)
    assert "路徑長度上限" in described.splitlines()[0]
    assert described.endswith("Traceback ...")


def test_missing_toolkit_files_names_what_the_extraction_skipped(tmp_path: Path) -> None:
    toolkit = tmp_path / "mcu_toolkit"
    (toolkit / "a").mkdir(parents=True)
    (toolkit / "a" / "kept.h").write_text("x", encoding="utf-8")
    manifest = {"files": {"a/kept.h": "0", "a/b/deep_test.cc": "0"}}
    (toolkit / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert missing_toolkit_files(tmp_path) == ["a/b/deep_test.cc"]
    assert missing_toolkit_files(tmp_path / "nowhere") == []


def test_the_shipped_toolkit_has_every_file_its_manifest_lists() -> None:
    assert missing_toolkit_files(ROOT) == []


def test_a_chinese_temp_folder_is_swapped_for_the_studio_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Measured: tf_keras's model.save() stages through %TEMP%, which TensorFlow cannot open."""
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tempfile, "tempdir", r"C:\Users\你的名字\AppData\Local\Temp")
    fallback = tmp_path / "workspace" / "tmp"
    assert use_ascii_temp_dir(fallback) == fallback
    assert tempfile.gettempdir() == str(fallback)
    assert os.environ["TEMP"] == os.environ["TMP"] == str(fallback)


def test_an_english_temp_folder_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", r"C:\Users\yourname\AppData\Local\Temp")
    assert use_ascii_temp_dir(tmp_path / "unused") is None
    assert not (tmp_path / "unused").exists()


def test_a_failed_job_tells_the_student_about_the_path_limit() -> None:
    jobs = JobManager(max_workers=1)
    long_path = "C:\\" + "z" * 250 + ".wav"

    def task(progress: Any) -> None:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", long_path)

    job = jobs.submit("train", None, task)
    jobs._executor.shutdown(wait=True)
    record = jobs.get(job["id"])
    assert record["state"] == "failed"
    assert record["error"].startswith("FileNotFoundError: ")
    assert "路徑長度上限" in record["error"] and "路徑長度上限" in record["message"]


def test_a_file_system_error_in_a_request_reaches_the_page_with_advice(
    store: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = store.create_project("image", "Long path")

    def broken(project_id: str) -> int:
        raise PermissionError(errno.EACCES, "Permission denied", "C:\\" + "q" * 250)

    monkeypatch.setattr(store, "prune_missing_samples", broken)
    app = create_app(store=store, jobs=JobManager(max_workers=1), runtime=ModelRuntime())
    with TestClient(app) as client:
        response = client.get(f"/api/projects/{project['id']}")
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail.startswith("PermissionError: ") and "路徑長度上限" in detail


def test_installer_checks_the_path_before_creating_the_venv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        install_local,
        "setup_log",
        lambda: (tmp_path / "s.log", tmp_path / "l.log", io.StringIO(), io.StringIO()),
    )
    monkeypatch.setattr(install_local, "install_problems", lambda root: ["路徑有問題"])
    monkeypatch.setattr(install_local, "ensure_venv", lambda: pytest.fail("created .venv"))
    monkeypatch.chdir(tmp_path)
    assert install_local.main() == 1
    assert "路徑有問題" in capsys.readouterr().err
