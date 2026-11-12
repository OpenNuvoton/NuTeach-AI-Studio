"""Student tool: download and install the Arm GNU Toolchain into the Studio's runtime folder.

The Studio builds M55M1 firmware with `arm-none-eabi-gcc` but does not bundle it (GPL-3.0
with the GCC runtime library exception; students install it themselves). This script fetches
the pinned Arm-hosted release, checks its SHA-256 (the digest Arm publishes is recorded below;
a download that differs is compared with the `.sha256asc` file Arm serves), and extracts it so
`nuteach/mcu/toolchain.py`'s local-install probe (`LOCAL_TOOLCHAIN_ROOT`) finds it without any
PATH or environment-variable setup.

Usage:
  .venv\\Scripts\\python.exe scripts\\download_arm_toolchain.py
  .venv\\Scripts\\python.exe scripts\\download_arm_toolchain.py --dest D:\\tools\\arm-gnu-toolchain
  .venv\\Scripts\\python.exe scripts\\download_arm_toolchain.py --keep-zip

`--dest` is the toolchain folder itself (it ends up holding bin\\, lib\\ ...), not its parent:
it has to be new, empty, or an earlier install of this toolchain, because it is replaced as a
whole. By itself the Studio looks in runtime\\arm-gnu-toolchain\\ and in the Arm installer's
folder under Program Files; a toolchain anywhere else has to be named with the NUTEACH_ARM_GCC_BIN
environment variable or PATH.

No network (a school that blocks developer.arm.com): put the official zip, not unpacked, next to
the destination (runtime\\ by default) and run the script; a copy whose SHA-256 matches the
pinned release is used as it is and is never deleted.

Only runs the network when a student invokes it directly; 01_INSTALL.bat does not call this.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import http.client
import os
import re
import secrets
import shutil
import stat
import sys
import time
import urllib.request
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from nuteach import __version__
from nuteach.archive import safe_extract_zip
from nuteach.mcu.errors import long_path_reason
from nuteach.mcu.paths import LOCAL_TOOLCHAIN_ROOT, MCU_TOOLKIT_ROOT
from nuteach.mcu.toolchain import probe_toolchain

TOOLCHAIN_VERSION = "14.2.rel1"
_ARCHIVE_STEM = f"arm-gnu-toolchain-{TOOLCHAIN_VERSION}-mingw-w64-i686-arm-none-eabi"
ARCHIVE_NAME = _ARCHIVE_STEM + ".zip"
BASE_URL = f"https://developer.arm.com/-/media/Files/downloads/gnu/{TOOLCHAIN_VERSION}/binrel/"
# SHA-256 of that archive as Arm publishes it. A download with this digest needs no second
# request, and a zip the student already has is used without any network at all; only a
# download that differs is compared with the `.sha256asc` Arm serves today.
PINNED_SHA256 = "6facb152ce431ba9a4517e939ea46f057380f8f1e56b62e8712b3f3b87d994e1"
# Size of the pinned archive, and of what it unpacks to (8,332 files, rounded up for the space
# NTFS needs around them). Known up front so a disk that is too full is refused before the
# download instead of at the end of the extraction.
_ARCHIVE_BYTES = 305_393_359
_INSTALLED_BYTES = 1_260_000_000
_CHUNK_SIZE = 1024 * 1024
# Per read, not for the whole download: a slow connection keeps delivering. Windows does not
# answer Ctrl+C while a read is waiting, so this is also the longest a stalled download keeps
# the key waiting.
_DOWNLOAD_TIMEOUT = 30
_USER_AGENT = f"NuTeach-AI-Studio/{__version__}"
_GCC_RELATIVE = Path("bin") / "arm-none-eabi-gcc.exe"
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")
# Everything Arm's zip and Arm's installer put at the top level of an install; the installer's
# manifest is named after its release ("13.3.rel1-mingw-w64-i686-arm-none-eabi-manifest.txt").
# desktop.ini is Explorer's own note about the folder, nobody's work.
_INSTALL_TOP_LEVEL = frozenset({
    "arm-none-eabi", "bin", "include", "lib", "libexec", "share",
    ".version", "manifest.txt", "uninstall.exe", "desktop.ini",
})
_INSTALLER_MANIFEST = "-arm-none-eabi-manifest.txt"
# Windows refuses a path of 260 characters or more unless long paths are switched on for the
# whole PC, and the mingw-built gcc cannot open such a path even then.
_MAX_PATH_CHARS = 259
_FORBIDDEN_NAME_CHARS = '<>:"|?*'
# Longest member name of the pinned archive (a libstdc++ header). Known up front so a hopeless
# destination is refused before the 305 MB download; the archive itself is measured again once
# it is on disk.
_PINNED_LONGEST_MEMBER_CHARS = 117
# Siblings of the destination used while installing: ".tc-new-<8 hex>" holds the extracted tree
# until it is complete, ".tc-old-<8 hex>" the previous install until the new one is in place. The
# first six hex digits are a hash of the destination's name, so a run touches only what belongs
# to its own destination when two toolchains live side by side in one folder (two names sharing
# all six digits: one pair in sixteen million). Both names are shorter than
# "arm-gnu-toolchain": staging never needs a longer path than the default destination does. The
# file ".tc-lock" is held open for as long as a run lasts; it is one per folder, because the
# downloaded zip is shared by every destination in that folder.
_STAGING_NEW = ".tc-new-"
_STAGING_OLD = ".tc-old-"
_LOCK_NAME = ".tc-lock"
_STAGING_NAME_CHARS = len(_STAGING_NEW) + 8
# Windows refuses to rename a folder while any file inside it is open. Right after extracting
# thousands of .exe and .a files that is usually the virus scanner, and it lets go within a
# second or two; a build that is running does not, and is reported after the last attempt.
_RENAME_ATTEMPTS = 10
_RENAME_RETRY_SECONDS = 0.5
# Windows words its own errors in the PC's language; the C runtime's are English.
_ERRNO_WORDS = {
    errno.EACCES: "存取被拒",
    errno.ENOENT: "找不到檔案或資料夾",
    errno.EEXIST: "已經有同名的檔案或資料夾",
    errno.ENOSPC: "磁碟空間不足",
    errno.EINVAL: "名稱不合法",
}


class _Refused(Exception):
    """The install cannot start; the message says why, in words for the student."""


class _Failed(RuntimeError):
    """The install did not finish; the message is already worded for the student."""


def archive_url() -> str:
    return BASE_URL + ARCHIVE_NAME


def checksum_url() -> str:
    return archive_url() + ".sha256asc"


def parse_sha256asc(text: str, archive_name: str) -> str:
    """Parse Arm's `.sha256asc`: one `sha256sum` line for the archive (others are ignored).

    Arm publishes the binary-mode form, `<sha256> *<filename>`; the text-mode form,
    `<sha256>  <filename>`, is accepted as well. A line only counts when its first field really
    is a SHA-256, so a stray "File: <filename>" line cannot be mistaken for the checksum.
    """

    for raw_line in text.splitlines():
        parts = raw_line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0], parts[1].strip().removeprefix("*")
        if name == archive_name and _SHA256_HEX.fullmatch(digest):
            return digest
    raise ValueError(f"checksum for {archive_name!r} not found in .sha256asc contents")


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _why(exc: OSError) -> str:
    """The reason for an OSError, in the student's language where that is known."""

    if getattr(exc, "winerror", None) is None and exc.errno in _ERRNO_WORDS:
        return _ERRNO_WORDS[exc.errno]
    return (exc.strerror or str(exc)).rstrip("。. ")


def _explain(exc: Exception, work_dir: Path) -> str:
    """Any failure in words for the student: never a bare "[Errno 13] Permission denied: ..."."""

    if isinstance(exc, _Failed):
        return str(exc)  # worded where it was raised
    if isinstance(exc, OSError):
        if exc.errno == errno.ENOSPC:
            return (
                f"{work_dir} 所在的磁碟滿了。解壓後的工具鏈約 1.2 GB；請先清出空間，"
                "或用 --dest 指定另一個磁碟上的位置"
            )
        return (
            f"讀寫 {exc.filename or work_dir} 時被 Windows 拒絕（{_why(exc)}）。它可能還在複製中，"
            "或正被其他程式使用（防毒軟體、另一個視窗）；請稍後再執行一次本腳本，"
            "仍然不行就把這一行訊息交給老師"
        )
    return f"發生沒有預期到的錯誤（{type(exc).__name__}：{exc}）；請把這一行訊息交給老師"


def _command(dest: str) -> str:
    """The whole command line for another destination, to copy as it is."""

    return f'.venv\\Scripts\\python.exe scripts\\download_arm_toolchain.py --dest "{dest}"'


def _offline_hint(work_dir: Path) -> str:
    return (
        f"這台電腦連不上 developer.arm.com 時，可以在別的網路下載 {archive_url()} ，"
        f"把這個 zip 直接放到 {work_dir}（不要解壓縮、不要改檔名），再執行一次本腳本，就會直接使用它"
    )


def _download(url: str, destination: Path, *, label: str, accept=None) -> None:
    """Stream `url` to `destination`, printing progress; writes via a `.part` temp file.

    `accept(part)` judges the finished body before it takes `destination`'s place and raises to
    turn it down: whatever `destination` held before stays untouched then.

    Only what the network does wrong leaves as an OSError or HTTPException. What goes wrong on
    this PC -- another program holds the file, the disk is full -- is a RuntimeError worded for
    the student, so it is never reported as "cannot reach developer.arm.com".
    """

    temp = destination.with_name(destination.name + ".part")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp.unlink(missing_ok=True)
        output = temp.open("wb")
    except OSError as exc:
        raise _Failed(
            f"無法建立下載檔 {temp}（{_why(exc)}）；它可能正被其他程式使用，請稍後再執行一次本腳本"
        ) from exc
    downloaded = 0
    try:
        with output:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as response:
                content_length = response.headers.get("Content-Length") or ""
                known = content_length.isascii() and content_length.isdigit()
                total_bytes = int(content_length) if known else None
                try:
                    while True:
                        chunk = response.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        try:
                            output.write(chunk)
                            output.flush()
                        except OSError as exc:
                            raise _Failed(
                                f"寫入 {temp} 失敗（{_why(exc)}）；請確認磁碟還有足夠的空間"
                            ) from exc
                        downloaded += len(chunk)
                        if total_bytes:
                            percent = min(100, downloaded * 100 // total_bytes)
                            print(f"\r[下載] {label}：{percent}%", end="", flush=True)
                        else:
                            print(f"\r[下載] {label}：{downloaded // 1024} KB", end="", flush=True)
                finally:
                    if downloaded:
                        print()  # end the progress line, also in front of an error message
        if total_bytes is not None and downloaded != total_bytes:
            # http.client returns a short body without complaining when the server hangs up.
            raise OSError(f"連線中途斷了，只收到 {downloaded:,} / {total_bytes:,} 位元組")
        if accept is not None:
            accept(temp)
        try:
            temp.replace(destination)
        except OSError as exc:
            raise _Failed(
                f"無法把下載好的檔案改名成 {destination}（{_why(exc)}）；原有的同名檔案可能正被"
                "其他程式開著，請關掉那個程式或刪除該檔案後，再執行一次本腳本"
            ) from exc
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise


def _is_toolchain(folder: Path) -> bool:
    try:
        return (folder / _GCC_RELATIVE).is_file()
    except OSError:  # a folder this account may not read
        return False


def _is_previous_install(folder: Path) -> bool:
    """True when `folder` looks like an Arm GNU Toolchain, whole or damaged, and nothing else.

    A folder is only ever replaced as a whole when its top level and its bin\\ hold nothing but
    what the toolchain puts there. Arm's zip and Arm's installer use the same few names at the
    top level, and every program in bin\\ is called arm-none-eabi-*; a general tools folder that
    merely has bin\\arm-none-eabi-gcc.exe next to other things is not an install to replace.
    gcc.exe itself need not be there: a virus scanner that took it away is exactly why a student
    reinstalls. What lies deeper (lib\\, share\\ ...) is not looked at.
    """

    try:
        top_level = [entry.name.lower() for entry in folder.iterdir()]
        programs = [entry.name.lower() for entry in (folder / "bin").iterdir()]
    except OSError:
        return False
    return (
        any(name.startswith("arm-none-eabi-") for name in programs)
        and all(name.startswith("arm-none-eabi-") or name == "gccvar.bat" for name in programs)
        and all(name in _INSTALL_TOP_LEVEL or name.endswith(_INSTALLER_MANIFEST) for name in top_level)
    )


def _is_archive_copy(path: Path) -> bool:
    """Named like the archive, also as a browser names a second download ("... (1).zip")."""

    name = path.name.lower()
    return name.startswith(_ARCHIVE_STEM) and name.endswith(".zip")


def _dest_tag(dest: Path) -> str:
    """Six hex digits that stand for `dest`'s name in the names of its staging siblings."""

    return hashlib.sha256(dest.name.casefold().encode("utf-8")).hexdigest()[:6]


def _new_sibling(dest: Path, prefix: str) -> Path:
    """A staging path next to `dest` that nothing occupies yet."""

    while True:
        candidate = dest.with_name(prefix + _dest_tag(dest) + secrets.token_hex(1))
        if not os.path.lexists(candidate):
            return candidate


def _staging_folders(dest: Path, prefix: str) -> list[Path]:
    """The staging folders with `prefix` that runs for this very `dest` left next to it."""

    name = re.compile(re.escape(prefix + _dest_tag(dest)) + "[0-9a-f]{2}")
    try:
        return sorted(
            entry
            for entry in dest.parent.iterdir()
            if name.fullmatch(entry.name)
            and entry.is_dir()
            and not entry.is_symlink()
            and not entry.is_junction()
        )
    except OSError:
        return []


def _path_problem(dest: Path, longest_member: int) -> str | None:
    """What is wrong with the path `dest` itself, whatever is on the disk; None when nothing is."""

    if not dest.name:
        example = f"{dest.drive}\\NuTeach_Tools\\arm-gnu-toolchain"
        return (
            f"不能把工具鏈直接裝在磁碟機的最上層（{dest}）；請用 --dest 指定一個資料夾，"
            f"例如：{_command(example)}"
        )
    names = "".join(dest.parts[1:])
    forbidden = sorted(char for char in _FORBIDDEN_NAME_CHARS if char in names)
    if forbidden:
        return (
            f"安裝位置 {dest} 裡有 Windows 的檔名不能用的字元（{' '.join(forbidden)}）。"
            "用引號包住 --dest 的路徑時，結尾不要加反斜線：命令列會把「\\\"」當成檔名裡的一個引號，"
            "連後面的參數一起吃進路徑裡"
        )
    root_chars = max(len(str(dest)), len(str(dest.parent)) + 1 + _STAGING_NAME_CHARS)
    deepest = root_chars + 1 + longest_member
    if deepest > _MAX_PATH_CHARS:
        return (
            f"安裝路徑太長：工具鏈裡最長的檔案路徑會有 {deepest} 字元，Windows 的上限是 "
            f"{_MAX_PATH_CHARS}。請把 Studio 資料夾搬到較短的路徑（例如 C:\\NuTeach AI Studio），"
            f"或用 --dest 指定較短的安裝位置，例如：{_command('C:\\NuTeach_Tools\\arm-gnu-toolchain')}"
        )
    return None


def _occupied_problem(dest: Path) -> str:
    """Why the folder `dest`, which holds something that is not a toolchain, is left alone."""

    entries = list(dest.iterdir())
    brought = [entry for entry in entries if entry.is_file() and _is_archive_copy(entry)]
    if brought and all(entry in brought or entry.name.endswith(".sha256asc") for entry in entries):
        # The offline route, one folder too deep.
        return (
            f"壓縮檔放錯位置了：{dest} 是工具鏈要裝進去的資料夾。請把裡面的檔案（zip，以及 .sha256asc "
            f"如果有）都搬到上一層（{dest.parent}），再執行一次本腳本"
        )
    if len(entries) == 1 and entries[0].is_dir() and _is_previous_install(entries[0]):
        # What unpacking a zip by hand gives; the Studio itself accepts it, this script does not.
        return (
            f"{dest} 裡是一份多包了一層資料夾的工具鏈（{entries[0].name}）；本腳本只更換 bin\\ 直接在"
            f"裡面的工具鏈資料夾。請先把 {dest} 改名（例如 {dest.name}-old），再執行一次本腳本；"
            "確定新的可以用之後，再刪掉改名的那一份"
        )
    return (
        f"{dest} 裡面已經有別的東西，不是「只裝了 Arm GNU Toolchain」的資料夾，所以不會動它。"
        f"想把工具鏈裝在這個資料夾裡面的話，請改用 --dest \"{dest}\\arm-gnu-toolchain\"；"
        "如果它是壞掉的舊工具鏈，請先把它改名或刪除，再執行一次本腳本"
    )


def _destination_problem(dest: Path, longest_member: int) -> str | None:
    """Why `dest` cannot receive the toolchain, in words for the student; None when it can.

    `dest` is replaced as a whole, so it has to be new, empty, or an earlier install of this
    toolchain. Pointing --dest at a folder that holds anything else (D:\\tools, say) would
    otherwise delete that folder's own bin\\, lib\\, include\\ and share\\.
    """

    problem = _path_problem(dest, longest_member)
    if problem:
        return problem
    if dest.exists() and not dest.is_dir():
        return f"{dest} 是一個檔案，不是資料夾；請用 --dest 指定一個資料夾"
    if dest.is_dir():
        if any(dest.iterdir()) and not _is_previous_install(dest):
            return _occupied_problem(dest)
        here = Path.cwd().resolve()
        if here == dest or dest in here.parents:
            return (
                f"命令視窗現在就開在 {dest} 裡面，Windows 不允許更換正在使用的資料夾；"
                "請先切到別的資料夾（例如 Studio 資料夾）再執行本腳本，或用 --dest 指定別的位置"
            )
    return None


def _free_bytes(folder: Path) -> int | None:
    try:
        return shutil.disk_usage(folder).free
    except OSError:
        return None


def _require_space(work_dir: Path, *, download: bool) -> None:
    """Refuse before the download, not at the end of the extraction, when the disk is too full."""

    needed = _INSTALLED_BYTES + (_ARCHIVE_BYTES if download else 0)
    free = _free_bytes(work_dir)
    if free is None or free >= needed:
        return
    parts = "下載的壓縮檔約 300 MB、解壓後約 1,200 MB" if download else "解壓後約 1,200 MB"
    raise _Failed(
        f"磁碟空間不足，{work_dir} 所在的磁碟只剩 {free // 10**6:,} MB，安裝需要約 "
        f"{needed // 10**6:,} MB（{parts}，再加一點餘裕）。請先清出空間，或用 --dest 指定另一個"
        f"磁碟上的位置，例如：{_command('D:\\NuTeach_Tools\\arm-gnu-toolchain')}"
    )


def _toolchain_root(extract_dir: Path) -> Path:
    """The folder under `extract_dir` that directly holds `bin/arm-none-eabi-gcc.exe`.

    Arm's 14.2.rel1 Windows zip has bin/, lib/, arm-none-eabi/ ... at its top level; a zip that
    wraps them in one folder is accepted too.
    """

    if _is_toolchain(extract_dir):
        return extract_dir
    folders = [entry for entry in extract_dir.iterdir() if entry.is_dir()]
    if len(folders) == 1 and _is_toolchain(folders[0]):
        return folders[0]
    raise _Failed(
        f"解壓後找不到 {_GCC_RELATIVE}（頂層有 {len(folders)} 個資料夾）；"
        "壓縮檔內容可能與預期的 Arm GNU Toolchain 版面不符"
    )


def _first_missing(members: list[zipfile.ZipInfo], extracted: Path) -> str | None:
    """The first file of the archive that is not on the disk in full, if there is one."""

    # The last entry of a name is the one that ends up on the disk, and Windows does not tell
    # names apart by letter case.
    expected = {
        info.filename.replace("\\", "/").casefold(): info for info in members if not info.is_dir()
    }
    for info in expected.values():
        target = extracted.joinpath(*PurePosixPath(info.filename.replace("\\", "/")).parts)
        try:
            if target.stat().st_size != info.file_size:
                return info.filename
        except OSError:
            return info.filename
    return None


def _rename(source: Path, target: Path) -> None:
    """`source.rename(target)`, retried for a few seconds while Windows says access is denied."""

    for attempt in range(1, _RENAME_ATTEMPTS + 1):
        try:
            source.rename(target)
            return
        except PermissionError:
            if attempt == _RENAME_ATTEMPTS:
                raise
            time.sleep(_RENAME_RETRY_SECONDS)


def _clear_read_only(function, path, _error) -> None:
    """`shutil.rmtree` hook: a read-only file is deleted after all, a file in use is left."""

    if function in (os.unlink, os.rmdir):
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IWRITE)
            function(path)


def _remove_tree(folder: Path) -> None:
    """Delete `folder` as far as Windows allows; the caller looks at what is left."""

    shutil.rmtree(folder, onexc=_clear_read_only)


def _put_back(parked: Path, dest: Path) -> None:
    """Rename the parked previous install back to `dest`, or say exactly where it now is."""

    if not parked.exists():
        return  # nothing had been moved out of the way
    try:
        try:
            parked.rename(dest)  # at once, without sleeping: a Ctrl+C may already be pending
        except PermissionError:
            _rename(parked, dest)
    except OSError as exc:
        raise _Failed(
            f"安裝沒有完成，而且原本的工具鏈暫時放不回去：它現在在 {parked}。"
            f"請把這個資料夾改名回 {dest.name}，或再執行一次本腳本（它會自動放回去）"
        ) from exc


def _swap_into_place(source_root: Path, dest: Path) -> None:
    """Make `source_root` the new `dest`; the previous `dest` survives until that has worked."""

    untouched = "；原本的工具鏈沒有被更動" if _is_toolchain(dest) else ""
    parked = _new_sibling(dest, _STAGING_OLD)
    try:
        if dest.is_dir() and not any(dest.iterdir()):
            dest.rmdir()  # an empty folder holds nothing to keep
        elif dest.exists():
            _rename(dest, parked)
    except OSError as exc:
        raise _Failed(
            f"無法更換 {dest}：它正在使用中（{_why(exc)}）。請先關掉 Studio、正在建置的工作，"
            f"以及開在這個資料夾裡的命令視窗或檔案總管，再重新執行本腳本{untouched}"
        ) from exc
    except BaseException:
        _put_back(parked, dest)  # Ctrl+C that arrived just after the rename had worked
        raise
    try:
        _rename(source_root, dest)
    except PermissionError as exc:
        _put_back(parked, dest)
        raise _Failed(
            "剛解壓好的檔案還被其他程式占用（多半是防毒軟體正在掃描），"
            f"請過幾分鐘再執行一次本腳本{untouched}"
        ) from exc
    except BaseException:
        # Ctrl+C included: a destination that existed before must not be left missing. The
        # interrupt can also arrive just after the rename has worked; the new toolchain is in
        # place then, and the old copy stays parked for the next run to clear.
        if source_root.exists() or not _is_toolchain(dest):
            _put_back(parked, dest)
        raise
    # The copy parked above, and any that an earlier run could not put back: all superseded now.
    try:
        for old_copy in _staging_folders(dest, _STAGING_OLD):
            _remove_tree(old_copy)
    except KeyboardInterrupt:
        pass  # the new toolchain is already in place; only an old copy is left to tidy up
    for old_copy in _staging_folders(dest, _STAGING_OLD):
        print(f"[警告] 舊版工具鏈沒有刪乾淨，可以稍後手動刪除：{old_copy}")


def _extract_and_install(archive_path: Path, dest: Path) -> None:
    """Extract `archive_path` next to `dest`, then swap the finished tree into place.

    Nothing under `dest` changes until the new tree is complete, and the swap is two renames, so
    a failure at any point -- a full disk, a file of the old install still in use by a running
    build -- leaves the previous install exactly as it was.

    The staging folder is made with a plain mkdir on purpose. A `tempfile` directory is private
    to the account that created it, and everything extracted inside keeps that ACL after the
    move: the toolchain would be unreadable to every other Windows account on the PC.
    """

    with zipfile.ZipFile(archive_path) as zf:
        members = zf.infolist()
    problem = _destination_problem(dest, max((len(info.filename) for info in members), default=0))
    if problem:
        raise _Failed(problem)

    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = _new_sibling(dest, _STAGING_NEW)
    staging.mkdir()
    try:
        safe_extract_zip(archive_path, staging)
        missing = _first_missing(members, staging)
        if missing:
            # The archive was read without an error and the file is not there: something on
            # this PC took it away. Swapping in a toolchain with holes would replace a working one.
            raise _Failed(
                f"解壓出來的工具鏈不完整：{missing} 不見了，多半是防毒軟體在解壓時把它移走了。"
                f"請把 {dest.parent} 加進防毒軟體的排除清單（或到它的隔離區把檔案還原並允許），"
                "再執行一次本腳本；不知道怎麼做的話，請把這一行訊息交給老師"
            )
        _swap_into_place(_toolchain_root(staging), dest)
    finally:
        # Gone already when the staging folder itself became `dest`.
        if staging.exists():
            _remove_tree(staging)


def _discard(folder: Path) -> None:
    # Said first: a real tree is 8,000 files and takes seconds to delete.
    print(f"[資訊] 清掉上一次安裝留下的暫存資料夾 {folder.name} …")
    _remove_tree(folder)
    if folder.exists():
        print(f"[資訊] {folder} 沒有刪乾淨（裡面有檔案正在使用），可以稍後手動刪除")


def _tidy_interrupted_runs(dest: Path) -> None:
    """Deal with what a killed run left next to `dest`.

    Closing the console window in the middle of an install skips every cleanup: the half
    extracted ".tc-new-*" tree stays (over 1 GiB), and so does a ".tc-old-*" folder -- which,
    when the run died between the two renames, is the student's only working toolchain.

    Called while this run holds the folder's lock: no other run is at work here, so every
    staging folder of this destination is a leftover.
    """

    for folder in _staging_folders(dest, _STAGING_OLD):
        if not _is_toolchain(folder) or _is_toolchain(dest):
            _discard(folder)  # of no use once `dest` holds a toolchain again
            continue
        try:
            if dest.is_dir():
                dest.rmdir()  # only an empty folder may make way for it
            _rename(folder, dest)
        except OSError:
            print(
                f"[資訊] 上一次安裝中斷時，原本的工具鏈被留在 {folder}，現在還放不回去；"
                f"這次安裝如果沒有成功，請把它改名回 {dest.name}"
            )
        else:
            print(f"[資訊] 上一次安裝中斷時被移開的工具鏈，已經放回 {dest}")
    for folder in _staging_folders(dest, _STAGING_NEW):
        _discard(folder)


@contextlib.contextmanager
def _only_run_for(dest: Path) -> Iterator[None]:
    """Hold the lock file next to `dest` open for as long as the install runs.

    A second console window installing here would take this run's staging folders for leftovers
    and its parked toolchain for one to put back, and both would write the same zip. Windows
    refuses to delete a file that another process has open, so a lock file that can be deleted
    belongs to a run that is gone. The file is also the first thing written next to `dest`: a
    folder that refuses new files is found out here, not after the download.
    """

    work_dir = dest.parent
    lock = dest.with_name(_LOCK_NAME)
    made_here = [folder for folder in (work_dir, *work_dir.parents) if not folder.exists()]
    try:
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _Refused(f"無法建立資料夾 {work_dir}（{_why(exc)}）；請用 --dest 指定別的位置") from exc
        try:
            with contextlib.suppress(OSError):
                os.chmod(lock, stat.S_IWRITE)  # a read-only leftover is still only a leftover
            lock.unlink(missing_ok=True)
            handle = lock.open("x")
        except OSError as exc:
            if os.path.lexists(lock):
                raise _Refused(
                    f"另一個視窗似乎正在 {work_dir} 裡安裝工具鏈，請等它結束（同時安裝會互相干擾）。"
                    f"確定沒有的話，手動刪除 {lock} 後再執行一次本腳本"
                ) from exc
            example = f"{dest.drive}\\NuTeach_Tools\\arm-gnu-toolchain"
            raise _Refused(
                f"無法在 {work_dir} 建立檔案（{_why(exc)}）。下載的壓縮檔與安裝用的暫存資料夾都放在安裝"
                f"位置的旁邊；請用 --dest 指定一個可以寫入的位置，例如：{_command(example)}"
            ) from exc
        try:
            yield
        finally:
            handle.close()
            with contextlib.suppress(OSError):
                lock.unlink(missing_ok=True)
    except _Refused:
        # A refused run leaves no empty folders behind (rmdir takes only those). A run that got
        # as far as the download keeps the folder: it is where the offline hint says to put the zip.
        for folder in made_here:
            with contextlib.suppress(OSError):
                folder.rmdir()
        raise


def _remove_downloads(paths: list[Path]) -> list[Path]:
    """Delete the files this run downloaded; returns the ones that could not be deleted."""

    left: list[Path] = []
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            left.append(path)
    return left


def _state_on_disk(dest: Path) -> str:
    """What an interrupted run leaves behind, read from the disk: Ctrl+C can arrive anywhere."""

    if _is_toolchain(dest):
        return f"{dest} 裡的工具鏈可以使用"
    if any(_is_toolchain(folder) for folder in _staging_folders(dest, _STAGING_OLD)):
        return "原本的工具鏈還沒放回去，再執行一次本腳本就會先自動放回"
    return "沒有安裝任何東西"


def _point_studio_at(installed_bin: Path) -> str:
    # `set` in a console would be gone as soon as that console closes, and a Studio started by
    # double-clicking 02_START.bat never sees it; setx stores the variable for the account.
    if "%" in str(installed_bin):
        # cmd would expand "%NAME%" inside the pasted line, and there is no way to quote it.
        return (
            f"請把使用者環境變數 NUTEACH_ARM_GCC_BIN 設成 {installed_bin}（這個路徑裡有 %，不能用 setx "
            "指令；請在開始功能表搜尋「編輯您帳戶的環境變數」來新增），"
            "再重新啟動 Studio（關掉黑色視窗後重新雙擊 02_START.bat）"
        )
    return (
        f'請在命令提示字元執行 setx NUTEACH_ARM_GCC_BIN "{installed_bin}" 把環境變數 NUTEACH_ARM_GCC_BIN '
        "存起來，再重新啟動 Studio（關掉黑色視窗後重新雙擊 02_START.bat）"
    )


def _report_probe(dest: Path) -> None:
    """Say which toolchain the Studio will actually use now."""

    installed_bin = dest / "bin"
    info = probe_toolchain()
    found = Path(info["bin_dir"]) if info.get("available") and info.get("bin_dir") else None
    tried = [Path(item) for item in info.get("checked") or []]
    if found is not None and found == installed_bin:
        print(f"[OK] 偵測到 Arm GNU Toolchain：{info.get('version')}（路徑：{found}）")
        return
    if installed_bin in tried:
        # The probe did look at the new install and moved on: its gcc did not run.
        print(
            f"[警告] 剛裝好的 {installed_bin / 'arm-none-eabi-gcc.exe'} 沒辦法執行：多半是防毒軟體或 "
            "Windows 的「智慧型應用程式控制」擋下了它。請把這一行訊息交給老師；處理好之後不必重裝，"
            "打開 Studio 的「部署到開發板」頁籤就會重新偵測。"
        )
        if found is not None:
            print(f"[資訊] Studio 目前改用另一份工具鏈：{info.get('version')}（路徑：{found}）")
    elif found is not None:
        print(f"[資訊] Studio 目前會先用另一份工具鏈：{info.get('version')}（路徑：{found}）")
        print(f"[資訊] 要改用剛裝好的這一份，{_point_studio_at(installed_bin)}")
    else:
        print(
            "[警告] 安裝完成，但 Studio 偵測不到它。Studio 自己只會找 runtime\\arm-gnu-toolchain 和 "
            f"Arm 官方安裝程式的預設位置；裝在別的位置時，{_point_studio_at(installed_bin)}"
        )


def _brought_archive(work_dir: Path) -> Path | None:
    """The pinned archive, when the student has already put it next to the destination.

    A browser calls a second download of the same file "... (1).zip": any name of that shape
    counts, and only a copy with the pinned SHA-256 is used.
    """

    try:
        copies = sorted(path for path in work_dir.iterdir() if path.is_file() and _is_archive_copy(path))
    except OSError:
        copies = []
    copies.sort(key=lambda path: path.name != ARCHIVE_NAME)  # the proper name first
    for path in copies:
        print(f"[資訊] 這裡已經有 {path.name}，先檢查它…")
        if _sha256_of(path) == PINNED_SHA256:
            print(f"[OK] SHA-256 與 {TOOLCHAIN_VERSION} 相符，直接使用，不必連網。")
            return path
        print(f"[資訊] 它和腳本記錄的 {TOOLCHAIN_VERSION} 不一樣（可能沒有下載完整），不使用。")
    return None


def _read_downloaded(part: Path, read):
    """`read(part)`; a file that cannot be read back is this PC's problem, not the network's."""

    try:
        return read(part)
    except OSError as exc:
        raise _Failed(f"讀不到剛下載的檔案 {part}（{_why(exc)}）；請稍後再執行一次本腳本") from exc


def _published_sha256(checksum_path: Path, downloaded: list[Path]) -> str:
    """Fetch Arm's `.sha256asc` and return the digest it gives for the archive."""

    not_pinned = (
        f"剛下載的壓縮檔和本腳本記錄的 {TOOLCHAIN_VERSION} 不同，沒有 Arm 的校驗檔就無法確認它。"
        "請稍後再執行一次本腳本；一直這樣的話，請告訴老師或 Studio 的維護者（Arm 可能重新發佈過這個檔案）"
    )
    digests: list[str] = []

    def names_the_archive(part: Path) -> None:
        text = _read_downloaded(part, Path.read_bytes).decode("utf-8-sig", errors="replace")
        try:
            digests.append(parse_sha256asc(text, ARCHIVE_NAME).lower())
        except ValueError as exc:
            raise _Failed(
                "從 Arm 下載到的校驗檔內容不對（學校／公司的網路可能把它換成了登入或封鎖頁面）。"
                + not_pinned
            ) from exc

    if checksum_path.exists():
        # The student's own: Arm's goes next to it under another name, and is the run's to delete.
        checksum_path = checksum_path.with_name(checksum_path.name + ".arm")
    try:
        _download(checksum_url(), checksum_path, label="SHA-256 校驗檔", accept=names_the_archive)
    except (OSError, http.client.HTTPException) as exc:
        raise _Failed(f"下載 SHA-256 校驗檔失敗（{exc}）。{not_pinned}") from exc
    downloaded.append(checksum_path)
    return digests[0]


def _fetch_verified(archive_path: Path, checksum_path: Path, downloaded: list[Path]) -> bool:
    """Download the archive and make sure it is Arm's; True when it is the pinned release.

    The download is judged while it is still "<zip>.part". A file the student already has under
    the archive's name is therefore replaced by a verified download only -- never by the login
    page of a school network. What is fetched is added to `downloaded` once it is in place,
    except a download that is the pinned release: that one is worth keeping when the run does
    not finish, so that fixing the problem does not cost a second 305 MB download.
    """

    hint = _offline_hint(archive_path.parent)
    pinned = False

    def is_arms_archive(part: Path) -> None:
        nonlocal pinned
        print("[資訊] 驗證 SHA-256…")
        actual = _read_downloaded(part, _sha256_of)
        if actual == PINNED_SHA256:
            print("[OK] SHA-256 驗證通過。")
            pinned = True
            return
        if not _read_downloaded(part, zipfile.is_zipfile):
            # A captive portal or a content filter answers with its own page, and says "200 OK".
            size = _read_downloaded(part, lambda path: path.stat().st_size)
            raise _Failed(
                f"下載到的不是完整的 zip 壓縮檔（{size:,} 位元組）：可能是下載中斷了，"
                f"或學校／公司的網路把它換成了登入或封鎖頁面。{hint}"
            )
        # Not the bytes recorded above: ask Arm what it publishes today.
        expected = _published_sha256(checksum_path, downloaded)
        if actual != expected:
            raise _Failed(
                f"SHA-256 驗證失敗：預期 {expected}，實際 {actual}；下載可能已毀損或遭竄改，請重新執行本腳本。"
            )
        print("[OK] SHA-256 與 Arm 目前公布的校驗檔相符。")
        print(
            f"[警告] 這份壓縮檔和本腳本記錄的 {TOOLCHAIN_VERSION}（SHA-256 {PINNED_SHA256[:12]}…）不是同一份；"
            "Arm 可能重新發佈過。照常安裝，但請把這件事告訴老師或 Studio 的維護者。"
        )

    try:
        _download(archive_url(), archive_path, label="工具鏈壓縮檔", accept=is_arms_archive)
    except (OSError, http.client.HTTPException) as exc:
        raise _Failed(f"下載失敗（{exc}）。{hint}") from exc
    if not pinned:
        downloaded.append(archive_path)
    return pinned


def _install(dest: Path, downloaded: list[Path], *, keep_zip: bool) -> int:
    """Get the archive, verify it and install it. `downloaded` is what an unfinished run deletes."""

    work_dir = dest.parent
    archive_path = work_dir / ARCHIVE_NAME
    checksum_path = work_dir / (ARCHIVE_NAME + ".sha256asc")
    # Only what this run downloaded is ever deleted. A zip the student fetched by hand and put
    # here is used when it is the pinned release, and is left alone either way unless a
    # verified download takes its place.
    brought: Path | None = None
    verified_download = False
    replaces_theirs = False
    arms_checksum = checksum_path.with_name(checksum_path.name + ".arm")  # see _published_sha256()
    try:
        # What a killed run left: no other run is at work in this folder (the lock is held).
        with contextlib.suppress(OSError):
            arms_checksum.unlink(missing_ok=True)
        for unfinished in (archive_path, checksum_path, arms_checksum):
            with contextlib.suppress(OSError):
                unfinished.with_name(unfinished.name + ".part").unlink(missing_ok=True)
        brought = _brought_archive(work_dir)
        _require_space(work_dir, download=brought is None)
        if brought is None:
            replaces_theirs = archive_path.is_file()
            if replaces_theirs:
                print(f"[資訊] 改為重新下載；下載並驗證過之後，新的檔案會取代 {ARCHIVE_NAME}。")
            print(f"[資訊] 準備下載 Arm GNU Toolchain {TOOLCHAIN_VERSION}…")
            verified_download = _fetch_verified(archive_path, checksum_path, downloaded)
            if replaces_theirs and archive_path in downloaded:
                # It took the place of a file the student had put there: theirs now, never deleted.
                downloaded.remove(archive_path)

        print(f"[資訊] 解壓並安裝到 {dest} …")
        _extract_and_install(brought or archive_path, dest)
    except Exception as exc:  # noqa: BLE001 - top-level cleanup must catch any failure mode
        print(f"[錯誤] 安裝失敗：{_explain(exc, work_dir)}", file=sys.stderr)
        if verified_download:
            print(f"[資訊] 壓縮檔已通過驗證，保留在 {archive_path}；排除問題後再執行一次，不必重新下載。")
        _remove_downloads(downloaded)
        return 1

    # The toolchain is in place from here on: nothing below may turn that into a failure. A
    # just-written zip is often still open in the antivirus scanner when it is deleted.
    print(f"[OK] Arm GNU Toolchain 已安裝完成：{dest}")
    if brought is not None:
        print(f"[資訊] 原本就在的壓縮檔沒有刪除；不再需要時可以手動刪除：{brought}")
    if replaces_theirs:
        print(f"[資訊] 原本就在的壓縮檔已經換成剛下載並驗證過的這一份，沒有刪除：{archive_path}")
    ours = verified_download and not replaces_theirs
    finished = [*downloaded, archive_path] if ours else list(downloaded)
    downloaded.clear()  # kept or deleted right here: a Ctrl+C further down changes nothing
    if not keep_zip:
        for leftover in _remove_downloads(finished):
            print(f"[警告] 下載檔暫時刪不掉（可能正在被防毒軟體掃描），可以稍後手動刪除：{leftover}")
    try:
        _report_probe(dest)
    except Exception as exc:  # noqa: BLE001 - a report that fails must not undo "installed"
        print(
            f"[警告] 工具鏈已經裝好，但無法確認 Studio 會用哪一份（{exc}）；"
            "請開 Studio 的「部署到開發板」頁籤確認。"
        )
    return 0


def _run(dest: Path, downloaded: list[Path], *, keep_zip: bool) -> int:
    too_long = long_path_reason(MCU_TOOLKIT_ROOT)
    if too_long:
        print(f"[警告] {too_long}。在目前的位置，就算裝好工具鏈也無法建置韌體。")

    # Everything that can be said before the 305 MB download is said before it.
    problem = _path_problem(dest, _PINNED_LONGEST_MEMBER_CHARS)
    if problem:
        raise _Refused(problem)
    with _only_run_for(dest):
        try:
            _tidy_interrupted_runs(dest)
        except OSError:
            pass  # a courtesy; whatever really blocks the install is reported by the check below
        replaces = False
        try:
            problem = _destination_problem(dest, _PINNED_LONGEST_MEMBER_CHARS)
            replaces = problem is None and dest.is_dir() and any(dest.iterdir())
        except OSError as exc:
            problem = (
                f"無法檢查安裝目的地 {dest}（{_why(exc)}）；請確認這個資料夾打得開，"
                "或用 --dest 指定別的位置"
            )
        if problem:
            raise _Refused(problem)
        if replaces:
            # Only the top level and bin\ were looked at; say what replacing means while the
            # student can still stop.
            print(
                f"[資訊] {dest} 裡已經有一份工具鏈：新的裝好之後會把這個資料夾整個換掉"
                "（你自己放在裡面的檔案也不會保留）。"
            )
        return _install(dest, downloaded, keep_zip=keep_zip)


def _resolved_destination(typed: Path) -> Path:
    """The absolute destination; refuses what Windows would silently change on the way."""

    # Windows drops a trailing space or dot when it creates a folder, so the name typed and the
    # name on the disk would differ, and the leftovers of a killed run could not be matched to
    # the destination. Judged as typed: once the folder exists, resolve() hands back its real
    # name -- or the whole path with a "\\?\" prefix.
    for part in re.split(r"[\\/]+", str(typed)):
        if part not in ("", ".", "..") and part != part.rstrip(". "):
            raise _Refused(
                f"安裝位置 {typed} 裡有資料夾名稱是以空白或句點結尾的；Windows 建立資料夾時會把它們去掉。"
                "請照資料夾實際的名稱輸入（用引號包住路徑時，引號裡的結尾不要留空白）"
            )
    return typed.resolve()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        # argparse words its complaints in English; the usual slip is a path with a space in it.
        self.print_usage(sys.stderr)
        print(
            f"[錯誤] 參數有誤（{message}）。路徑裡有空白時，要用引號把整個路徑包起來，"
            '例如：--dest "D:\\My Tools\\arm-gnu-toolchain"',
            file=sys.stderr,
        )
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        # Ctrl+C before anything had started, or again while the first one was being answered.
        with contextlib.suppress(Exception, KeyboardInterrupt):
            print("\n[中止] 已取消。", file=sys.stderr)
        return 130


def _main(argv: list[str] | None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Both: a log made with "> log.txt 2>&1" must not be half UTF-8 and half Big5.
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError, OSError):
                pass

    parser = _Parser(
        description="下載並安裝 Arm GNU Toolchain 到 NuTeach AI Studio 的 runtime 資料夾。",
        allow_abbrev=False,  # "--keep" must not start an install as "--keep-zip"
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=LOCAL_TOOLCHAIN_ROOT,
        help="工具鏈資料夾本身（裝好後裡面是 bin、lib…）；必須是新的、空的，或先前裝過的工具鏈。預設：%(default)s",
    )
    parser.add_argument(
        "--keep-zip",
        action="store_true",
        help="安裝完成後保留下載的壓縮檔（預設會刪除）",
    )
    args = parser.parse_args(argv)

    downloaded: list[Path] = []
    dest: Path | None = None
    try:
        dest = _resolved_destination(args.dest)
        return _run(dest, downloaded, keep_zip=args.keep_zip)
    except _Refused as exc:
        print(f"[錯誤] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if dest is None:
            raise  # nothing has been looked at yet; main() answers
        # Said from what is on disk now: the interrupt may have come before anything was
        # touched, or after the new toolchain was already in place.
        print(f"\n[中止] 已取消；{_state_on_disk(dest)}。", file=sys.stderr)
        _remove_downloads(downloaded)
        kept = dest.parent / ARCHIVE_NAME
        if kept.is_file():
            print(f"[資訊] {kept} 留在原處；下次執行會先檢查它，內容相符就不必重新下載。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
