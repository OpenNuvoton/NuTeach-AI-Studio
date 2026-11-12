r"""Windows path limits that break the Studio, and advice a student can act on.

Stdlib only: scripts\install_local.py imports this with the base Python before .venv exists.
"""

from __future__ import annotations

import errno
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from .mcu.errors import long_path_reason

# Longest path Windows accepts while long-path support is off (MAX_PATH is 260 with the NUL).
MAX_PATH_CHARS = 259
# Deepest file pip writes under .venv, relative to the Studio root: a TensorFlow 2.21 header
# measured 187 characters on 2026-10-29; the margin is for the next TensorFlow.
VENV_DEEPEST_RELATIVE_CHARS = 190
# The Studio's own deepest runtime paths stay far below this, so an error naming a path this
# long is almost certainly the limit rather than a genuinely missing file.
LONG_PATH_HINT_CHARS = 240
SHORT_PATH_EXAMPLE = r"C:\NuTeach AI Studio"
_ERROR_FILENAME_EXCED_RANGE = 206
_QUOTED_PATH = re.compile(r"""['"]([A-Za-z]:[\\/][^'"\r\n]*)['"]""")
# Unquoted paths end at whitespace, so a path with spaces is measured short -- never long.
_BARE_PATH = re.compile(r"""(?<![\w'"])[A-Za-z]:[\\/][^\s'"<>|?*]*""")


def long_paths_enabled() -> bool:
    """Windows' LongPathsEnabled switch: python.exe honours it, gcc and Explorer's unzip do not."""
    if sys.platform != "win32":
        return True
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
        ) as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def non_ascii_reason(root: Path | str) -> str | None:
    text = str(root)
    if text.isascii():
        return None
    return (
        f"Studio 資料夾的路徑含有中文或其他非英文字元：{text}\n"
        "TensorFlow 在 Windows 上打不開這種路徑，「匯出模型」與「部署到開發板」都會失敗"
        "（常見原因：放在「桌面」「下載」或中文名稱的資料夾裡，或 Windows 使用者名稱是中文）。"
        f"請把整個 Studio 資料夾搬到只用英文字母、數字與空白的短路徑，例如 {SHORT_PATH_EXAMPLE}。"
    )


def install_problems(root: Path | str, *, long_paths: bool | None = None) -> list[str]:
    """Why 01_INSTALL.bat must stop now instead of failing ten minutes into the install."""
    problems = [reason for reason in (non_ascii_reason(root),) if reason]
    if long_paths is None:
        long_paths = long_paths_enabled()
    deepest = len(str(root)) + 1 + VENV_DEEPEST_RELATIVE_CHARS
    if not long_paths and deepest > MAX_PATH_CHARS:
        problems.append(
            f"Studio 資料夾路徑太長（{len(str(root))} 字元）：{root}\n"
            f"安裝 TensorFlow 時最深的檔案路徑會有 {deepest} 字元，超過 Windows 的上限 "
            f"{MAX_PATH_CHARS} 字元，安裝一定會失敗。請把整個資料夾搬到較短的路徑"
            f"（例如 {SHORT_PATH_EXAMPLE}）再執行 01_INSTALL.bat。"
        )
    return problems


def mcu_path_warning(root: Path | str) -> str | None:
    reason = long_path_reason(Path(root) / "mcu_toolkit")
    if reason is None:
        return None
    return reason + "。訓練與匯出不受影響，但「部署到開發板」會被擋下，建議現在就搬。"


def missing_toolkit_files(root: Path | str) -> list[str]:
    """Files mcu_toolkit\\manifest.json lists that are not on disk.

    Windows' built-in "Extract All" skips every file whose path would pass 260 characters, and
    mcu_toolkit holds the deepest files in the ZIP -- so a gap here means a too-deep extraction.
    """
    toolkit = Path(root) / "mcu_toolkit"
    try:
        files = json.loads((toolkit / "manifest.json").read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [name for name in files if not (toolkit / name).is_file()]


def startup_warnings(root: Path | str) -> list[str]:
    return [warning for warning in (non_ascii_reason(root), mcu_path_warning(root)) if warning]


def use_ascii_temp_dir(fallback: Path) -> Path | None:
    """Point TEMP/TMP at `fallback` while the temp folder holds non-ASCII characters.

    A Chinese Windows user name puts %TEMP% on such a path, and tf_keras's `model.save()` stages
    through tempfile into a directory TensorFlow then cannot create (measured 2026-10-29): every
    training would fail at the save step. Child processes (conversion worker, gcc, Vela) inherit
    the corrected environment.
    """
    if tempfile.gettempdir().isascii() or not str(fallback).isascii():
        return None
    fallback.mkdir(parents=True, exist_ok=True)
    for name in ("TMPDIR", "TEMP", "TMP"):
        os.environ[name] = str(fallback)
    tempfile.tempdir = None
    return fallback


def path_error_hint(error: BaseException | str) -> str | None:
    """Advice to append when an error looks like the path-length limit or a non-ASCII path."""
    texts: list[str] = []
    too_long = False
    seen: set[int] = set()
    current = error if isinstance(error, BaseException) else None
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        texts.append(str(current))
        if isinstance(current, OSError):
            too_long = too_long or (
                getattr(current, "winerror", None) == _ERROR_FILENAME_EXCED_RANGE
                or current.errno == errno.ENAMETOOLONG
            )
            texts.extend(str(name) for name in (current.filename, current.filename2) if name)
        current = current.__cause__ or current.__context__
    if isinstance(error, str):
        texts.append(error)
    paths: list[str] = []
    for text in texts:
        # str(OSError) shows the path as a repr, every backslash doubled.
        text = text.replace("\\\\", "\\")
        paths += _QUOTED_PATH.findall(text) + _BARE_PATH.findall(text)
    longest = max(paths, key=len, default="")
    if too_long or len(longest) >= LONG_PATH_HINT_CHARS:
        measured = f"出錯的路徑有 {len(longest)} 字元，" if longest else ""
        return (
            f"（這很可能是 Windows 的路徑長度上限造成的：{measured}超過約 260 字元就會失敗。"
            f"請把整個 Studio 資料夾搬到較短的路徑，例如 {SHORT_PATH_EXAMPLE}，再重新啟動。）"
        )
    unreadable = next((path for path in paths if not path.isascii()), None)
    if unreadable:
        return (
            f"（出錯的路徑含有中文或其他非英文字元：{unreadable}。TensorFlow 在 Windows 上打不開"
            f"這種路徑，請把整個 Studio 資料夾搬到只用英文字母、數字與空白的短路徑，例如 {SHORT_PATH_EXAMPLE}，"
            "再重新啟動。）"
        )
    return None


def describe_error(error: BaseException) -> str:
    """`str(error)`, with the path advice after its first line when it looks like a path problem."""
    message = str(error)
    hint = path_error_hint(error)
    if not hint:
        return message
    # Export errors carry the worker's log tail after the first line; keep the advice in view.
    first, newline, rest = message.partition("\n")
    return f"{first}{hint}{newline}{rest}"
