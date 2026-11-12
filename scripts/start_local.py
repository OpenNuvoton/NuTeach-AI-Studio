from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import traceback
import webbrowser
from pathlib import Path
from time import time_ns

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start NuTeach AI Studio.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        help="Keep running after the last browser page closes (default: exit by itself).",
    )
    parser.add_argument(
        "--auto-exit-seconds",
        type=float,
        default=None,
        help="Exit this many seconds after the last page heartbeat (0 = never).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Import and create the FastAPI app, then exit without starting a server.",
    )
    return parser.parse_args(argv)


def _port_is_in_use(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _create_application():
    from nuteach.app import create_app

    return create_app()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    from nuteach.config import LOG_ROOT, PROJECTS_ROOT
    from nuteach.logging_utils import setup_session_logging
    from nuteach.runtime_config import apply_runtime_environment, public_runtime_info

    session_log = setup_session_logging(LOG_ROOT)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    runtime_config = apply_runtime_environment()
    runtime_public = public_runtime_info(runtime_config)

    from nuteach.path_limits import startup_warnings

    # The black window is where students look first; stderr is also teed into LATEST.log.
    for warning in startup_warnings(PROJECT_ROOT):
        print(f"[WARN] {warning}", file=sys.stderr, flush=True)

    # Port checks must run *before* the app is created: create_app() sweeps
    # workspace/tmp/mcu/<hex> work dirs and pending firmware download copies
    # (nuteach.mcu.housekeeping.sweep_temp_root()) on the assumption that this is the only
    # instance starting up. A student double-clicking 02_START.bat while another instance is
    # mid-deploy must be told "port already in use" and exit before ever touching that sweep --
    # not after it has already deleted the live instance's in-progress work. --check keeps
    # building the app with no port involved at all: that is the whole point of --check.
    if not args.check:
        if not 1 <= args.port <= 65535:
            print(f"[ERROR] Port 必須介於 1 到 65535，目前是 {args.port}。", file=sys.stderr)
            return 2

        if _port_is_in_use(args.host, args.port):
            print(f"[ERROR] Port {args.port} 已被其他程式使用。", file=sys.stderr)
            print(f"請執行：netstat -ano | findstr :{args.port}", file=sys.stderr)
            return 3

    # Decided before the app exists: create_app() reads it from the environment, and the
    # tests / --check path must keep building the app with the monitor idle.
    if args.keep_alive:
        os.environ["NUTEACH_AUTO_EXIT_SECONDS"] = "0"
    elif args.auto_exit_seconds is not None:
        os.environ["NUTEACH_AUTO_EXIT_SECONDS"] = str(max(0.0, args.auto_exit_seconds))

    try:
        application = _create_application()
    except Exception:
        print("[ERROR] 無法載入 NuTeach AI Studio FastAPI 應用程式。", file=sys.stderr)
        print(f"Project root : {PROJECT_ROOT}", file=sys.stderr)
        print(f"Python       : {sys.executable}", file=sys.stderr)
        print(f"Log          : {session_log.latest_path}", file=sys.stderr)
        traceback.print_exc()
        return 1

    if args.check:
        print("[PASS] NuTeach AI Studio application import/create check")
        print(f"Project root : {PROJECT_ROOT}")
        print(f"Python       : {sys.executable}")
        print(f"Runtime      : {runtime_public.get('backend_label')}")
        print(f"Log          : {session_log.latest_path}")
        return 0

    import uvicorn

    url_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    # A unique URL prevents Chrome from restoring an old NuTeach AI Studio page from
    # its back/forward cache without requesting index.html or app.js again.
    url = f"http://{url_host}:{args.port}/?session={time_ns()}"

    if not args.no_browser:
        timer = threading.Timer(1.3, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()

    print("=" * 78, flush=True)
    print(" NuTeach AI Studio v2.1.1", flush=True)
    print(" Local URL :", url, flush=True)
    print(" Workspace :", PROJECTS_ROOT, flush=True)
    print(" Runtime   :", runtime_public.get("backend_label"), flush=True)
    print(" Log       :", session_log.latest_path, flush=True)
    auto_exit = getattr(application.state, "auto_exit", None)
    if auto_exit is not None and auto_exit.enabled:
        print(
            " Stop      : press Ctrl+C（關閉所有 Studio 網頁後約 8 秒自動結束；"
            "--keep-alive 可停用）",
            flush=True,
        )
    else:
        print(" Stop      : press Ctrl+C", flush=True)
    print("=" * 78, flush=True)

    try:
        # uvicorn.Server rather than uvicorn.run(): the auto-exit monitor needs a handle it can
        # flip (`should_exit`) from its own thread; uvicorn then drains and returns normally, so
        # 02_START.bat sees exit code 0 and closes its window.
        server = uvicorn.Server(
            uvicorn.Config(
                application,
                host=args.host,
                port=args.port,
                reload=False,
                access_log=True,
            )
        )

        def request_shutdown() -> None:
            server.should_exit = True

        application.state.request_shutdown = request_shutdown
        server.run()
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("[ERROR] 本機服務執行失敗。", file=sys.stderr)
        traceback.print_exc()
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
