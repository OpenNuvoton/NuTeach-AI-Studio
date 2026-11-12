"""Auto-exit: the Studio stops by itself once the last browser page is gone.

The decision is a pure function of (heartbeats, leaving beacons, running jobs, clock), so it
is driven here with a fake clock; the thread and the uvicorn hook are exercised through the
public API only where they are cheap.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from nuteach.auto_exit import (
    DEFAULT_GRACE_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    LEAVE_GRACE_SECONDS,
    AutoExitMonitor,
)
from nuteach.jobs import JobManager
from nuteach.project_store import ProjectStore


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _monitor(clock: Clock, *, busy: bool = False, grace: float = 120.0) -> AutoExitMonitor:
    return AutoExitMonitor(grace_seconds=grace, is_busy=lambda: busy, clock=clock)


def test_never_exits_before_the_first_page_arrives() -> None:
    clock = Clock()
    monitor = _monitor(clock)
    clock.now += 10_000
    assert monitor.should_exit() is False  # --check, tests, API-only use


def test_everyone_left_exits_after_the_leave_grace() -> None:
    clock = Clock()
    monitor = _monitor(clock)
    monitor.heartbeat("tab-a")
    monitor.heartbeat("tab-b")
    monitor.heartbeat("tab-a", leaving=True)
    clock.now += LEAVE_GRACE_SECONDS + 1
    assert monitor.should_exit() is False  # tab-b is still open
    monitor.heartbeat("tab-b", leaving=True)
    assert monitor.should_exit() is False  # a reload needs a moment to come back
    clock.now += LEAVE_GRACE_SECONDS - 1
    assert monitor.should_exit() is False
    clock.now += 2
    assert monitor.should_exit() is True


def test_a_reload_cancels_the_pending_exit() -> None:
    clock = Clock()
    monitor = _monitor(clock)
    monitor.heartbeat("old")
    monitor.heartbeat("old", leaving=True)
    clock.now += 2
    monitor.heartbeat("new")  # the reloaded page
    clock.now += LEAVE_GRACE_SECONDS + 5
    assert monitor.should_exit() is False


def test_silence_exits_after_the_grace_even_without_a_goodbye() -> None:
    clock = Clock()
    monitor = _monitor(clock, grace=120.0)
    monitor.heartbeat("tab")
    clock.now += 119
    assert monitor.should_exit() is False
    clock.now += 2
    assert monitor.should_exit() is True  # browser killed, no beacon ever came


def test_a_suspend_restarts_the_grace_instead_of_exiting_on_resume() -> None:
    clock = Clock()
    monitor = _monitor(clock, grace=120.0)
    monitor.heartbeat("tab")
    for _ in range(3):
        clock.now += 1
        assert monitor.observe_poll() is False  # ordinary one-second ticks
    # The PC sleeps for ten minutes: the page never left and could not send anything.
    clock.now += 600
    assert monitor.observe_poll() is True
    assert monitor.should_exit() is False
    # From the rebase on, the ordinary silence rule applies again.
    clock.now += 119
    assert monitor.should_exit() is False
    clock.now += 2
    assert monitor.should_exit() is True


def test_a_running_job_postpones_the_exit() -> None:
    clock = Clock()
    busy = {"value": True}
    monitor = AutoExitMonitor(grace_seconds=120.0, is_busy=lambda: busy["value"], clock=clock)
    monitor.heartbeat("tab")
    monitor.heartbeat("tab", leaving=True)
    clock.now += 600
    assert monitor.should_exit() is False  # training still running
    busy["value"] = False
    assert monitor.should_exit() is True  # ...and nobody came back afterwards


def test_disabled_monitor_never_exits_and_reports_it() -> None:
    clock = Clock()
    monitor = _monitor(clock, grace=0)
    monitor.heartbeat("tab")
    monitor.heartbeat("tab", leaving=True)
    clock.now += 10_000
    assert monitor.enabled is False and monitor.should_exit() is False
    assert monitor.status()["enabled"] is False


def test_status_reports_clients_and_interval() -> None:
    clock = Clock()
    monitor = _monitor(clock)
    status = monitor.heartbeat("tab")
    assert status["clients"] == 1 and status["enabled"] is True
    assert status["heartbeat_interval_seconds"] == HEARTBEAT_INTERVAL_SECONDS
    assert status["grace_seconds"] == 120.0
    assert DEFAULT_GRACE_SECONDS >= 90.0  # Chrome throttles hidden tabs to ~1 timer/minute


def test_shutdown_uses_the_launcher_hook_when_installed() -> None:
    calls: list[str] = []
    monitor = AutoExitMonitor(grace_seconds=1.0, request_shutdown=lambda: calls.append("hook"))
    monitor._shutdown()
    assert calls == ["hook"]


def test_jobs_any_active_tracks_queued_and_running() -> None:
    import threading

    jobs = JobManager(max_workers=1)
    assert jobs.any_active() is False
    release = threading.Event()

    def task(progress):
        release.wait(5)
        return {}

    job = jobs.submit("train", "p1", task)
    assert jobs.any_active() is True
    release.set()
    for _ in range(200):
        if jobs.get(job["id"])["state"] == "completed":
            break
        threading.Event().wait(0.02)
    assert jobs.any_active() is False


def test_heartbeat_endpoint_and_health(store: ProjectStore) -> None:
    from nuteach.app import create_app

    app = create_app(store=store, auto_exit_seconds=120.0)
    client = TestClient(app)
    health = client.get("/api/health").json()
    assert health["auto_exit"]["enabled"] is True and health["auto_exit"]["clients"] == 0
    beat = client.post("/api/heartbeat", json={"client_id": "tab-1"})
    assert beat.status_code == 200 and beat.json()["clients"] == 1
    bye = client.post("/api/heartbeat", json={"client_id": "tab-1", "leaving": True})
    assert bye.json()["clients"] == 0
    assert client.post("/api/heartbeat", json={"client_id": ""}).status_code in (400, 422)
    # Without the launcher hook a pending exit is logged and dropped, never a SIGINT into
    # the host process (this pytest run).
    app.state.auto_exit._shutdown()
    app.state.auto_exit.stop()


def test_env_var_disables_the_monitor(store: ProjectStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from nuteach.app import create_app

    monkeypatch.setenv("NUTEACH_AUTO_EXIT_SECONDS", "0")
    client = TestClient(create_app(store=store))
    assert client.get("/api/health").json()["auto_exit"]["enabled"] is False
    monkeypatch.setenv("NUTEACH_AUTO_EXIT_SECONDS", "not-a-number")
    assert create_app(store=store).state.auto_exit.grace_seconds == DEFAULT_GRACE_SECONDS
    assert "NUTEACH_AUTO_EXIT_SECONDS" in os.environ


def test_launcher_flags_parse() -> None:
    from scripts import start_local

    args = start_local._parse_args(["--keep-alive"])
    assert args.keep_alive is True and args.auto_exit_seconds is None
    args = start_local._parse_args(["--auto-exit-seconds", "45"])
    assert args.auto_exit_seconds == 45.0
