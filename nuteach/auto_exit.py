"""Stop the Studio by itself once the last browser page has gone away.

A student closes the tab and expects the console window to go with it; instead the server
keeps running until someone finds the window and presses Ctrl+C, and the next double-click on
02_START.bat then fails with 「Port 8765 已被其他程式使用」. So every page announces itself:
``POST /api/heartbeat`` every few seconds while open, and once more with ``leaving=true`` from
``pagehide`` (via ``navigator.sendBeacon``) as it closes. This monitor decides when nobody is
left and asks uvicorn to shut down.

Two independent signals, because either one alone misfires:

* **Everyone said goodbye** -- exit ``LEAVE_GRACE_SECONDS`` after the last page's ``leaving``
  beacon. A reload also fires ``pagehide``, so the grace is long enough for the new page to send
  its first heartbeat and cancel the exit.
* **Silence** -- exit when no heartbeat at all has arrived for ``grace_seconds`` (a browser that
  crashed or a tab Windows discarded never gets to say goodbye). Chrome throttles timers in
  hidden tabs down to about once a minute, so this grace must stay well above 60 s.
* **Sleep is not silence** -- ``time.monotonic()`` keeps counting through standby on Windows,
  so a poll gap longer than ``SUSPEND_GAP_SECONDS`` means the machine was suspended. Every
  stamp is then rebased and the grace starts over, instead of the monitor exiting on its
  first tick after resume while the tab is still open and has not yet had a chance to speak.

Never while a job is queued or running: a training or firmware build that was started on
purpose finishes first, and the exit happens afterwards if nobody came back. Never before the
first heartbeat either: ``--no-browser`` API use, ``--check`` and the tests start no page, so
they must not be killed by a monitor that was waiting for one.

Stdlib only, so ``create_app()`` can build it without pulling anything else in.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger("nuteach.auto_exit")

# Default silence grace, in seconds; 0 disables the monitor. Read from
# NUTEACH_AUTO_EXIT_SECONDS by create_app(); scripts/start_local.py --keep-alive sets 0.
DEFAULT_GRACE_SECONDS = 120.0
# Seconds after the last page's `leaving` beacon before exiting (a reload needs a few).
LEAVE_GRACE_SECONDS = 8.0
# Heartbeat period the page is told to use (returned by /api/heartbeat and /api/health).
HEARTBEAT_INTERVAL_SECONDS = 15.0
# How often the background thread re-evaluates.
POLL_SECONDS = 1.0
# A gap between two polls longer than this cannot come from the loop itself (it sleeps one
# second): the machine was suspended, and every page went quiet through no fault of its own.
SUSPEND_GAP_SECONDS = 5.0


class AutoExitMonitor:
    """Tracks open pages by heartbeat and requests shutdown when none is left."""

    def __init__(
        self,
        *,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        is_busy: Callable[[], bool] = lambda: False,
        request_shutdown: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        leave_grace_seconds: float = LEAVE_GRACE_SECONDS,
    ) -> None:
        self.grace_seconds = max(0.0, float(grace_seconds))
        self.leave_grace_seconds = max(0.0, float(leave_grace_seconds))
        self._is_busy = is_busy
        self._request_shutdown = request_shutdown
        self._clock = clock
        self._lock = threading.Lock()
        self._clients: dict[str, float] = {}
        self._last_seen: float | None = None
        self._all_left_at: float | None = None
        self._last_poll: float | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False
        self.exit_requested = False

    @property
    def enabled(self) -> bool:
        return self.grace_seconds > 0.0

    # ----------------------------------------------------------------- page signals
    def heartbeat(self, client_id: str, *, leaving: bool = False) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            if leaving:
                self._clients.pop(client_id, None)
                if not self._clients and self._last_seen is not None:
                    self._all_left_at = now
            else:
                self._clients[client_id] = now
                self._last_seen = now
                self._all_left_at = None
            if self.enabled:
                self._ensure_thread_locked()
            return self._status_locked(now)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_locked(self._clock())

    def _status_locked(self, now: float) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "grace_seconds": self.grace_seconds,
            "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
            "clients": len(self._clients),
            "seconds_since_last_heartbeat": (
                None if self._last_seen is None else round(now - self._last_seen, 1)
            ),
            "exit_requested": self.exit_requested,
        }

    # ------------------------------------------------------------------ decision
    def should_exit(self, now: float | None = None) -> bool:
        """The pure decision, so tests can drive it with a fake clock."""
        if not self.enabled:
            return False
        if now is None:
            now = self._clock()
        with self._lock:
            if self._last_seen is None:
                return False  # no page ever opened: API-only / --check / tests
            if self._is_busy():
                return False  # a training or firmware build is running: finish it first
            if (
                not self._clients
                and self._all_left_at is not None
                and now - self._all_left_at >= self.leave_grace_seconds
            ):
                return True
            stale = [cid for cid, seen in self._clients.items() if now - seen >= self.grace_seconds]
            for cid in stale:
                self._clients.pop(cid, None)
            return not self._clients and now - self._last_seen >= self.grace_seconds

    def observe_poll(self, now: float | None = None) -> bool:
        """Record one poll tick; True when a suspend was detected and every stamp was rebased.

        The thread calls this before should_exit(); public so the fake-clock tests can drive it.
        """
        if now is None:
            now = self._clock()
        with self._lock:
            previous = self._last_poll
            self._last_poll = now
            if previous is None or now - previous < SUSPEND_GAP_SECONDS:
                return False
            if self._last_seen is not None:
                self._last_seen = now
            for client_id in self._clients:
                self._clients[client_id] = now
            if self._all_left_at is not None:
                self._all_left_at = now
            LOGGER.info("auto-exit: %.0f 秒沒有輪詢（電腦可能睡眠過），重新計時。", now - previous)
            return True

    # ----------------------------------------------------------------- lifecycle
    def _ensure_thread_locked(self) -> None:
        if self._thread is not None or self._stopped:
            return
        self._thread = threading.Thread(target=self._run, name="nuteach-auto-exit", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stopped:
            time.sleep(POLL_SECONDS)
            if self.exit_requested:
                return
            now = self._clock()
            if self.observe_poll(now):
                continue
            if self.should_exit(now):
                self.exit_requested = True
                LOGGER.info("瀏覽器頁面已關閉，NuTeach AI Studio 自動結束。")
                self._shutdown()
                return

    def stop(self) -> None:
        self._stopped = True

    def _shutdown(self) -> None:
        """Call the launcher's hook. Deliberately no SIGINT fallback: this monitor also lives
        inside pytest and any process that merely imported the app, and a stray Ctrl+C there
        would kill the host, not a server."""
        if self._request_shutdown is None:
            LOGGER.warning("auto-exit: no shutdown hook installed, nothing to stop")
            return
        try:
            self._request_shutdown()
        except Exception:
            LOGGER.exception("auto-exit: the shutdown hook failed")
