from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import download_arm_toolchain as dl

# The digest developer.arm.com publishes for the pinned release.
PINNED_SHA256 = "6facb152ce431ba9a4517e939ea46f057380f8f1e56b62e8712b3f3b87d994e1"
# Byte-for-byte what developer.arm.com served for the pinned release on 2026-10-01: sha256sum's
# binary-mode line, with an asterisk in front of the file name.
ARM_PUBLISHED_SHA256ASC = (
    "6facb152ce431ba9a4517e939ea46f057380f8f1e56b62e8712b3f3b87d994e1 "
    "*arm-gnu-toolchain-14.2.rel1-mingw-w64-i686-arm-none-eabi.zip\n"
)
GCC = Path("bin") / "arm-none-eabi-gcc.exe"


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch) -> None:
    # A bug that skips a brought zip must not start a 305 MB download from a test run.
    real_urlopen = dl.urllib.request.urlopen

    def local_only(request, *args, **kwargs):
        url = getattr(request, "full_url", str(request))
        assert url.startswith("file:"), f"a test tried to open {url}"
        return real_urlopen(request, *args, **kwargs)

    monkeypatch.setattr(dl.urllib.request, "urlopen", local_only)


@pytest.fixture(autouse=True)
def _plenty_of_disk_space(monkeypatch) -> None:
    # The archives built here are a few hundred bytes; the real one needs 1.6 GB free.
    monkeypatch.setattr(dl, "_free_bytes", lambda folder: 10**12)


def _build_fake_archive(zip_path: Path) -> None:
    """A tiny zip that wraps the toolchain in one top-level folder (not what Arm ships)."""

    root = f"arm-gnu-toolchain-{dl.TOOLCHAIN_VERSION}-mingw-w64-i686-arm-none-eabi"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"{root}/bin/arm-none-eabi-gcc.exe", b"fake-gcc-binary")
        zf.writestr(f"{root}/lib/README.txt", b"placeholder\n")


def _build_flat_archive(zip_path: Path) -> None:
    """The real 14.2.rel1 zip's layout: bin/, lib/ ... and two files directly at the top."""

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(".version", b"14.2.rel1\n")
        zf.writestr("manifest.txt", b"placeholder\n")
        zf.writestr("bin/arm-none-eabi-gcc.exe", b"fake-gcc-binary")
        for folder in ("arm-none-eabi", "include", "lib", "libexec", "share"):
            zf.writestr(f"{folder}/README.txt", b"placeholder\n")


def _old_toolchain(dest: Path) -> None:
    """A previous install with one file the new archive does not contain."""

    (dest / "bin").mkdir(parents=True)
    (dest / GCC).write_bytes(b"old-gcc")
    (dest / "lib").mkdir()
    (dest / "lib" / "old_only.txt").write_bytes(b"old")


def _names(folder: Path) -> list[str]:
    return sorted(entry.name for entry in folder.iterdir())


def _leftover(dest: Path, kind: str, suffix: str = "0b") -> Path:
    """Where a killed run for `dest` leaves its ".tc-<kind>-*" staging folder."""

    return dest.with_name(f".tc-{kind}-{dl._dest_tag(dest)}{suffix}")


def _pin(monkeypatch, tmp_path: Path) -> Path:
    """Make a small archive count as the pinned release; returns that archive."""

    published = tmp_path / "published.zip"
    _build_flat_archive(published)
    monkeypatch.setattr(dl, "PINNED_SHA256", hashlib.sha256(published.read_bytes()).hexdigest())
    return published


def _deliver(destination: Path, write, accept) -> None:
    """What the real `_download` does with a body: judge it as "<name>.part", then move it in."""

    part = destination.with_name(destination.name + ".part")
    write(part)
    try:
        if accept is not None:
            accept(part)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(destination)


def _serve(
    monkeypatch, *, build=_build_flat_archive, checksum_line=None, calls=None, digest_case=str.lower
) -> None:
    """Replace the network: `_download` builds the archive and a checksum file for it."""

    def fake_download(url: str, destination: Path, *, label: str, accept=None) -> None:
        if calls is not None:
            calls.append(destination.name)
        if destination.name == dl.ARCHIVE_NAME:
            _deliver(destination, build, accept)
            return
        # Asked for while the archive is still being judged, that is, still a ".part" file.
        archive = destination.with_name(dl.ARCHIVE_NAME + ".part")
        line = (checksum_line or "{digest} *{name}\n").format(
            digest=digest_case(hashlib.sha256(archive.read_bytes()).hexdigest()), name=dl.ARCHIVE_NAME
        )
        _deliver(destination, lambda part: part.write_text(line, encoding="utf-8"), accept)

    monkeypatch.setattr(dl, "_download", fake_download)
    monkeypatch.setattr(dl, "probe_toolchain", lambda: {"available": False})


def _refuse_renaming(monkeypatch, prefixes: tuple[str, ...]) -> None:
    """Make every rename of a folder whose name starts with one of `prefixes` fail as in use."""

    rename = Path.rename

    def picky(self: Path, target: Path) -> Path:
        if self.name.startswith(prefixes):
            raise PermissionError(13, "Access is denied", str(self))
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", picky)
    monkeypatch.setattr(dl, "_RENAME_RETRY_SECONDS", 0)


def test_urls() -> None:
    assert dl.archive_url().endswith(
        "/14.2.rel1/binrel/arm-gnu-toolchain-14.2.rel1-mingw-w64-i686-arm-none-eabi.zip"
    )
    assert dl.checksum_url() == dl.archive_url() + ".sha256asc"


# --------------------------------------------------------------------------- checksum file
def test_parse_sha256asc() -> None:
    text = f"{PINNED_SHA256}  {dl.ARCHIVE_NAME}\n"
    assert dl.parse_sha256asc(text, dl.ARCHIVE_NAME) == PINNED_SHA256
    with pytest.raises(ValueError):
        dl.parse_sha256asc(f"{PINNED_SHA256}  other.zip\n", dl.ARCHIVE_NAME)


def test_parse_sha256asc_reads_the_line_arm_publishes() -> None:
    assert dl.parse_sha256asc(ARM_PUBLISHED_SHA256ASC, dl.ARCHIVE_NAME) == PINNED_SHA256
    with pytest.raises(ValueError):
        dl.parse_sha256asc(f"{PINNED_SHA256} *other.zip\n", dl.ARCHIVE_NAME)


def test_parse_sha256asc_only_takes_a_real_sha256() -> None:
    # A label line that happens to end with the archive name is not the checksum line.
    text = f"File: {dl.ARCHIVE_NAME}\n{PINNED_SHA256} *{dl.ARCHIVE_NAME}\n"
    assert dl.parse_sha256asc(text, dl.ARCHIVE_NAME) == PINNED_SHA256
    for not_a_digest in ("abc123", PINNED_SHA256[:-1], PINNED_SHA256 + "0", "z" * 64):
        with pytest.raises(ValueError):
            dl.parse_sha256asc(f"{not_a_digest}  {dl.ARCHIVE_NAME}\n", dl.ARCHIVE_NAME)


def test_pinned_sha256_is_the_digest_arm_publishes() -> None:
    assert dl.PINNED_SHA256 == PINNED_SHA256
    assert dl.parse_sha256asc(ARM_PUBLISHED_SHA256ASC, dl.ARCHIVE_NAME) == dl.PINNED_SHA256


# --------------------------------------------------------------------------- the download itself
def test_download_arrives_under_its_final_name_only_when_complete(tmp_path: Path, capsys) -> None:
    served = tmp_path / "served.bin"
    served.write_bytes(b"x" * 3000)
    target = tmp_path / "work" / "file.zip"

    dl._download(served.as_uri(), target, label="測試")

    assert target.read_bytes() == b"x" * 3000
    assert _names(target.parent) == ["file.zip"]  # no ".part" left
    assert "100%" in capsys.readouterr().out


def test_download_that_fails_leaves_no_part_file(tmp_path: Path) -> None:
    target = tmp_path / "work" / "file.zip"

    with pytest.raises(OSError):
        dl._download((tmp_path / "not-there.bin").as_uri(), target, label="測試")

    assert _names(target.parent) == []


def test_download_cut_short_by_the_server_is_a_failure(tmp_path: Path, monkeypatch) -> None:
    # http.client hands back a short body without raising when the connection just ends.
    class CutShort:
        def __init__(self) -> None:
            self.headers = {"Content-Length": "3000"}
            self.chunks = [b"x" * 1000, b""]

        def read(self, size: int) -> bytes:
            return self.chunks.pop(0)

    @contextlib.contextmanager
    def urlopen(request, timeout):
        yield CutShort()

    monkeypatch.setattr(dl.urllib.request, "urlopen", urlopen)
    target = tmp_path / "work" / "file.zip"

    with pytest.raises(OSError, match="1,000"):
        dl._download("https://example.invalid/file.zip", target, label="測試")

    assert _names(target.parent) == []


def test_a_download_that_is_turned_down_leaves_the_file_it_would_have_replaced(tmp_path: Path) -> None:
    served = tmp_path / "served.bin"
    served.write_bytes(b"a login page")
    target = tmp_path / "work" / "file.zip"
    target.parent.mkdir()
    target.write_bytes(b"what the student brought")
    seen: list[bytes] = []

    def turn_down(part: Path) -> None:
        seen.append(part.read_bytes())  # the whole body is there to be judged
        raise RuntimeError("not what was asked for")

    with pytest.raises(RuntimeError, match="not what was asked for"):
        dl._download(served.as_uri(), target, label="測試", accept=turn_down)

    assert seen == [b"a login page"]
    assert target.read_bytes() == b"what the student brought"
    assert _names(target.parent) == ["file.zip"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to replace a file that is open")
def test_download_that_cannot_replace_a_file_in_use_is_not_called_a_network_failure(
    tmp_path: Path,
) -> None:
    served = tmp_path / "served.bin"
    served.write_bytes(b"new")
    target = tmp_path / "work" / "file.zip"
    target.parent.mkdir()
    target.write_bytes(b"old")

    # RuntimeError, not OSError: main() words an OSError as "cannot reach developer.arm.com".
    with target.open("rb"), pytest.raises(RuntimeError, match=re.escape(str(target))):
        dl._download(served.as_uri(), target, label="測試")

    assert target.read_bytes() == b"old"
    assert _names(target.parent) == ["file.zip"]


# --------------------------------------------------------------------------- extract + swap
def test_extract_and_install_moves_top_level_contents(tmp_path: Path) -> None:
    archive_path = tmp_path / "downloads" / dl.ARCHIVE_NAME
    _build_fake_archive(archive_path)
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"

    dl._extract_and_install(archive_path, dest)

    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"
    assert (dest / "lib" / "README.txt").is_file()
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_extract_and_install_accepts_the_layout_arm_ships(tmp_path: Path) -> None:
    archive_path = tmp_path / "downloads" / dl.ARCHIVE_NAME
    _build_flat_archive(archive_path)
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"

    dl._extract_and_install(archive_path, dest)

    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"
    assert (dest / "arm-none-eabi" / "README.txt").is_file()
    assert (dest / "manifest.txt").is_file()
    # No staging folder is left behind, next to the archive or next to the install.
    assert _names(archive_path.parent) == [dl.ARCHIVE_NAME]
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_extract_and_install_rejects_unexpected_layout(tmp_path: Path) -> None:
    archive_path = tmp_path / "downloads" / dl.ARCHIVE_NAME
    archive_path.parent.mkdir()
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("one/marker.txt", b"a\n")
        zf.writestr("two/marker.txt", b"b\n")
    dest = tmp_path / "runtime" / "dest"

    with pytest.raises(RuntimeError):
        dl._extract_and_install(archive_path, dest)

    assert not dest.exists()
    assert _names(dest.parent) == []


def test_reinstall_replaces_the_previous_toolchain_as_a_whole(tmp_path: Path) -> None:
    archive_path = tmp_path / "downloads" / dl.ARCHIVE_NAME
    _build_flat_archive(archive_path)
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)

    dl._extract_and_install(archive_path, dest)

    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"
    assert not (dest / "lib" / "old_only.txt").exists()
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to rename a folder with a file in use")
def test_previous_toolchain_survives_when_one_of_its_files_is_in_use(
    tmp_path: Path, monkeypatch
) -> None:
    archive_path = tmp_path / "downloads" / dl.ARCHIVE_NAME
    _build_flat_archive(archive_path)
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    monkeypatch.setattr(dl, "_RENAME_RETRY_SECONDS", 0)

    # The open handle stands for a build that is running right now.
    with (dest / GCC).open("rb"), pytest.raises(RuntimeError, match=re.escape(str(dest))) as refusal:
        dl._extract_and_install(archive_path, dest)

    assert "沒有被更動" in str(refusal.value)
    # Not half replaced: every old file is still there and nothing new was mixed in.
    assert (dest / GCC).read_bytes() == b"old-gcc"
    assert (dest / "lib" / "old_only.txt").read_bytes() == b"old"
    assert not (dest / "manifest.txt").exists()
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_a_folder_the_virus_scanner_still_holds_is_renamed_on_a_later_attempt(
    tmp_path: Path, monkeypatch
) -> None:
    archive_path = tmp_path / "downloads" / dl.ARCHIVE_NAME
    _build_flat_archive(archive_path)
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    monkeypatch.setattr(dl, "_RENAME_RETRY_SECONDS", 0)
    rename = Path.rename
    refusals = {"left": 2}

    def busy_at_first(self: Path, target: Path) -> Path:
        if refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError(13, "Access is denied", str(self))
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", busy_at_first)

    dl._extract_and_install(archive_path, dest)

    assert refusals["left"] == 0
    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_staging_folder_is_not_a_private_tempfile_directory() -> None:
    # tempfile.mkdtemp() folders carry an owner-only ACL on Windows (Python 3.12.4+), and files
    # extracted inside keep it after being moved: the installed toolchain would be unreadable to
    # every other account on the PC. The staging folder must come from a plain mkdir.
    assert not hasattr(dl, "tempfile")


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("icacls"), reason="needs Windows icacls")
def test_the_installed_toolchain_takes_its_permissions_from_the_folder_it_is_in(tmp_path: Path) -> None:
    # mkdir(mode=0o700) gives the same owner-only ACL as a tempfile directory. Whoever may read
    # runtime\ has to be able to read the toolchain in it, exactly like any folder made there.
    def access_entries(path: Path) -> list[str]:
        listing = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
        return sorted(line.replace(str(path), "").strip() for line in listing.splitlines() if ":(" in line)

    archive_path = tmp_path / "downloads" / dl.ARCHIVE_NAME
    _build_flat_archive(archive_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    # pytest's own folders are owner-only; let ordinary users (S-1-5-32-545) read this one, as
    # they may read a Studio folder.
    subprocess.run(
        ["icacls", str(runtime), "/grant", "*S-1-5-32-545:(OI)(CI)(RX)"], capture_output=True, check=True
    )
    ordinary_folder = runtime / "made-with-mkdir"
    ordinary_folder.mkdir()
    dest = runtime / "arm-gnu-toolchain"

    dl._extract_and_install(archive_path, dest)

    assert len(access_entries(ordinary_folder)) > len(access_entries(tmp_path))  # the grant arrived
    assert access_entries(dest) == access_entries(ordinary_folder)


# --------------------------------------------------------------------------- a swap that fails
def test_previous_toolchain_is_put_back_when_the_new_tree_cannot_be_moved_in(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _serve(monkeypatch)
    _refuse_renaming(monkeypatch, (dl._STAGING_NEW,))  # the virus scanner never lets go

    assert dl.main(["--dest", str(dest)]) == 1

    assert (dest / GCC).read_bytes() == b"old-gcc"
    assert (dest / "lib" / "old_only.txt").read_bytes() == b"old"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]
    # A sentence for the student, not the raw Windows error.
    err = capsys.readouterr().err
    assert "Access is denied" not in err
    assert "沒有被更動" in err


@pytest.mark.parametrize("empty_folder_there", [False, True])
def test_a_first_install_that_cannot_be_moved_in_says_nothing_about_an_old_toolchain(
    tmp_path: Path, monkeypatch, capsys, empty_folder_there: bool
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    if empty_folder_there:
        dest.mkdir(parents=True)
    _serve(monkeypatch)
    _refuse_renaming(monkeypatch, (dl._STAGING_NEW,))

    assert dl.main(["--dest", str(dest)]) == 1

    err = capsys.readouterr().err
    assert "占用" in err
    # There was no toolchain to leave untouched, and none to put back.
    assert "沒有被更動" not in err and "放不回去" not in err
    assert _names(dest.parent) == []


def test_ctrl_c_just_after_the_old_toolchain_was_moved_aside_puts_it_back(
    tmp_path: Path, monkeypatch
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _serve(monkeypatch)
    rename = Path.rename

    def interrupted_once_parked(self: Path, target: Path) -> Path:
        result = rename(self, target)
        if Path(target).name.startswith(dl._STAGING_OLD):
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(Path, "rename", interrupted_once_parked)

    assert dl.main(["--dest", str(dest)]) == 130

    assert (dest / GCC).read_bytes() == b"old-gcc"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_ctrl_c_just_after_the_new_toolchain_is_in_place_does_not_undo_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _serve(monkeypatch)
    rename = Path.rename

    def interrupted_once_moved_in(self: Path, target: Path) -> Path:
        result = rename(self, target)
        if self.name.startswith(dl._STAGING_NEW):
            raise KeyboardInterrupt
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupted_once_moved_in)
        assert dl.main(["--dest", str(dest)]) == 130

    # The new toolchain stays; nothing claims that the old one could not be put back.
    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"
    err = capsys.readouterr().err
    assert "可以使用" in err and "放不回去" not in err
    # The old copy the interrupt left parked goes away on the next run.
    assert len(_names(dest.parent)) == 2
    dl._tidy_interrupted_runs(dest)
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_ctrl_c_between_the_two_renames_puts_the_previous_toolchain_back(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _serve(monkeypatch)
    rename = Path.rename

    def interrupted(self: Path, target: Path) -> Path:
        if self.name.startswith(dl._STAGING_NEW):
            raise KeyboardInterrupt
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", interrupted)

    assert dl.main(["--dest", str(dest)]) == 130

    assert (dest / GCC).read_bytes() == b"old-gcc"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]
    assert "可以使用" in capsys.readouterr().err


def test_ctrl_c_that_leaves_the_old_toolchain_parked_says_the_next_run_restores_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _serve(monkeypatch)
    rename = Path.rename

    def interrupted(self: Path, target: Path) -> Path:
        if self.name.startswith((dl._STAGING_NEW, dl._STAGING_OLD)):
            raise KeyboardInterrupt  # at the move-in, and again at the put-back that follows
        return rename(self, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupted)
        assert dl.main(["--dest", str(dest)]) == 130

    # Not "nothing was installed", and not "the toolchain can be used": it is parked.
    assert not dest.exists()
    assert "自動放回" in capsys.readouterr().err

    dl._tidy_interrupted_runs(dest)
    assert (dest / GCC).read_bytes() == b"old-gcc"


def test_a_toolchain_that_cannot_be_put_back_is_named_and_restored_by_the_next_run(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _serve(monkeypatch)

    with monkeypatch.context() as patch:
        _refuse_renaming(patch, (dl._STAGING_NEW, dl._STAGING_OLD))
        assert dl.main(["--dest", str(dest)]) == 1

    # The student is told which folder now holds their toolchain.
    parked = [name for name in _names(dest.parent) if name.startswith(dl._STAGING_OLD)]
    assert len(parked) == 1 and not dest.exists()
    assert parked[0] in capsys.readouterr().err

    dl._tidy_interrupted_runs(dest)  # what the next run does before anything else

    assert (dest / GCC).read_bytes() == b"old-gcc"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_ctrl_c_while_the_old_copy_is_deleted_still_counts_as_installed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _serve(monkeypatch)
    rmtree = shutil.rmtree

    def interrupted(path, *args, **kwargs) -> None:
        if Path(path).name.startswith(dl._STAGING_OLD):
            raise KeyboardInterrupt
        rmtree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(dl.shutil, "rmtree", interrupted)
        assert dl.main(["--dest", str(dest)]) == 0

    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"
    # The old copy the interrupt left behind is named, and goes away on the next run.
    assert len(_names(dest.parent)) == 2
    out = capsys.readouterr().out
    assert "沒有刪乾淨" in out and _leftover(dest, "old", "").name in out
    dl._tidy_interrupted_runs(dest)
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


# --------------------------------------------------------------------------- leftovers of a killed run
@pytest.mark.parametrize("empty_folder_in_the_way", [False, True])
def test_a_toolchain_parked_by_a_killed_run_is_put_back_before_anything_else(
    tmp_path: Path, monkeypatch, empty_folder_in_the_way: bool
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(_leftover(dest, "old"))
    if empty_folder_in_the_way:
        dest.mkdir()

    def offline(url: str, destination: Path, *, label: str, accept=None) -> None:
        raise OSError("getaddrinfo failed")

    monkeypatch.setattr(dl, "_download", offline)

    # The download fails, and the student still has the toolchain they had before.
    assert dl.main(["--dest", str(dest)]) == 1
    assert (dest / GCC).read_bytes() == b"old-gcc"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_a_parked_toolchain_is_kept_when_the_destination_cannot_take_it_back(
    tmp_path: Path, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    parked = _leftover(dest, "old")
    _old_toolchain(parked)
    (dest / "notes").mkdir(parents=True)

    dl._tidy_interrupted_runs(dest)

    assert (parked / GCC).read_bytes() == b"old-gcc"
    assert (dest / "notes").is_dir()
    assert str(parked) in capsys.readouterr().out


def test_a_parked_old_copy_is_cleared_once_the_destination_holds_a_toolchain(tmp_path: Path) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _old_toolchain(_leftover(dest, "old"))

    dl._tidy_interrupted_runs(dest)

    assert (dest / GCC).read_bytes() == b"old-gcc"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_a_parked_copy_that_could_not_be_put_back_goes_once_a_new_toolchain_is_in(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    parked = _leftover(dest, "old")
    _old_toolchain(parked)
    _serve(monkeypatch)
    _refuse_renaming(monkeypatch, (dl._STAGING_OLD,))  # an Explorer window is open inside it, say

    assert dl.main(["--dest", str(dest)]) == 0

    # Said where the old toolchain was while it mattered; superseded, it does not stay behind.
    assert str(parked) in capsys.readouterr().out
    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_a_half_extracted_tree_of_a_killed_run_is_cleared(tmp_path: Path, capsys) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    half_extracted = _leftover(dest, "new")
    (half_extracted / "bin").mkdir(parents=True)
    (half_extracted / GCC).write_bytes(b"half")

    dl._tidy_interrupted_runs(dest)

    assert _names(dest.parent) == []
    assert half_extracted.name in capsys.readouterr().out


def test_names_that_only_look_like_leftovers_are_left_alone(tmp_path: Path) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    tag = dl._dest_tag(dest)
    lookalikes = [
        f".tc-old-{tag}0b".upper(),
        f".tc-old-{tag}0b.bak",
        f".tc-old-{tag}0b2",
        f".tc-old-{tag}",
        f".tc-new-{tag}wx",
        ".tc-old-notes",
        f"x.tc-old-{tag}0b",
    ]
    for name in lookalikes:
        _old_toolchain(dest.parent / name)
    a_file = _leftover(dest, "new", "f1")
    a_file.write_bytes(b"a file, not a folder")

    dl._tidy_interrupted_runs(dest)

    assert _names(dest.parent) == sorted([*lookalikes, a_file.name])


def test_leftovers_of_another_destination_in_the_same_folder_are_not_touched(tmp_path: Path) -> None:
    # A run for D:\tools\gcc-a was killed between its two renames; this one installs gcc-b.
    dest_a = tmp_path / "tools" / "gcc-a"
    dest_b = tmp_path / "tools" / "gcc-b"
    parked_a = _leftover(dest_a, "old")
    half_extracted_a = _leftover(dest_a, "new")
    _old_toolchain(parked_a)
    (half_extracted_a / "bin").mkdir(parents=True)

    dl._tidy_interrupted_runs(dest_b)

    # gcc-a's toolchain neither became gcc-b nor was deleted.
    assert _names(dest_a.parent) == sorted([parked_a.name, half_extracted_a.name])

    dl._tidy_interrupted_runs(dest_a)

    assert (dest_a / GCC).read_bytes() == b"old-gcc"
    assert _names(dest_a.parent) == ["gcc-a"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to delete a read-only file")
def test_read_only_files_of_the_previous_install_are_deleted_too(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    (dest / "lib" / "old_only.txt").chmod(stat.S_IREAD)
    _serve(monkeypatch)

    assert dl.main(["--dest", str(dest)]) == 0

    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


# --------------------------------------------------------------------------- one run at a time
@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to delete a file that is open")
def test_a_second_run_for_the_same_destination_leaves_the_first_alone(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    parked = _leftover(dest, "old")  # the first run is between its two renames
    _old_toolchain(parked)
    lock = dest.with_name(dl._LOCK_NAME)
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    with lock.open("x"):  # the first run, in another console window
        assert dl.main(["--dest", str(dest)]) == 1

    assert calls == []
    # Its parked toolchain was not taken for a leftover and "put back" under its feet.
    assert not dest.exists()
    assert (parked / GCC).read_bytes() == b"old-gcc"
    assert str(lock) in capsys.readouterr().err

    # The lock file of a run that is gone stops nobody.
    assert dl.main(["--dest", str(dest)]) == 0
    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_a_read_only_lock_file_nobody_holds_does_not_stop_the_run(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    lock = dest.with_name(dl._LOCK_NAME)
    lock.parent.mkdir()
    lock.write_bytes(b"")
    lock.chmod(stat.S_IREAD)
    _serve(monkeypatch)

    assert dl.main(["--dest", str(dest)]) == 0

    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_gcc_taken_away_by_a_virus_scanner_during_the_install_is_named_as_such(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _serve(monkeypatch)
    extract = dl.safe_extract_zip

    def extract_then_quarantine(archive: Path, destination: Path) -> None:
        extract(archive, destination)
        (destination / GCC).unlink()

    monkeypatch.setattr(dl, "safe_extract_zip", extract_then_quarantine)

    assert dl.main(["--dest", str(dest)]) == 1

    err = capsys.readouterr().err
    assert "防毒" in err and "版面" not in err  # the archive is fine; the PC removed the file
    assert (dest / GCC).read_bytes() == b"old-gcc"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_a_folder_that_refuses_new_files_is_reported_before_the_download(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # C:\ lets a standard account create folders but not files: --dest C:\arm-gnu-toolchain.
    dest = tmp_path / "top" / "arm-gnu-toolchain"
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)
    open_path = Path.open

    def no_new_files(self: Path, mode: str = "r", *args, **kwargs):
        if self.parent == dest.parent and mode[0] in "wx":
            raise PermissionError(13, "Permission denied", str(self))
        return open_path(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_new_files)

    assert dl.main(["--dest", str(dest)]) == 1

    assert calls == []
    err = capsys.readouterr().err
    assert str(dest.parent) in err and "--dest" in err
    assert "developer.arm.com" not in err  # nothing is wrong with the network


# --------------------------------------------------------------------------- destination checks
def test_extract_and_install_refuses_a_destination_too_deep_for_windows(
    tmp_path: Path, monkeypatch
) -> None:
    archive_path = tmp_path / "downloads" / dl.ARCHIVE_NAME
    _build_flat_archive(archive_path)
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    monkeypatch.setattr(dl, "_MAX_PATH_CHARS", len(str(dest)) + 10)

    with pytest.raises(RuntimeError, match="--dest"):
        dl._extract_and_install(archive_path, dest)

    assert not dest.exists()


def test_main_refuses_a_destination_too_deep_before_downloading(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    monkeypatch.setattr(dl, "_MAX_PATH_CHARS", len(str(dest)) + 10)
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(dest)]) == 1

    assert calls == []
    assert not dest.exists()


def test_path_check_measures_the_installed_path_not_only_the_staging_folder(tmp_path: Path) -> None:
    dest = tmp_path / ("d" * 40)  # a destination name longer than the staging folders' names
    fits = dl._MAX_PATH_CHARS - len(str(dest)) - 1
    assert dl._destination_problem(dest, fits) is None
    assert "--dest" in dl._destination_problem(dest, fits + 1)


def test_default_destination_name_is_judged_by_its_own_length(tmp_path: Path) -> None:
    # The staging siblings are shorter than "arm-gnu-toolchain", so they never make the check
    # stricter than the installed toolchain itself needs.
    dest = tmp_path / "arm-gnu-toolchain"
    fits = dl._MAX_PATH_CHARS - len(str(dest)) - 1
    assert dl._destination_problem(dest, fits) is None
    assert dl._destination_problem(dest, fits + 1) is not None


def test_a_short_destination_name_is_judged_by_the_staging_folder_next_to_it(tmp_path: Path) -> None:
    # The tree is extracted into ".tc-new-<8 hex>" first, and that name is longer than "gcc".
    dest = tmp_path / "gcc"
    staging = _leftover(dest, "new")
    fits = dl._MAX_PATH_CHARS - len(str(staging)) - 1
    assert dl._destination_problem(dest, fits) is None
    assert dl._destination_problem(dest, fits + 1) is not None


def test_pinned_longest_member_matches_a_path_inside_the_real_archive() -> None:
    # arm-none-eabi/include/c++/14.2.1/ext/pb_ds/detail/cc_hash_table_map_/
    # constructor_destructor_no_store_hash_fn_imps.hpp, read from the archive's central directory.
    longest = (
        "arm-none-eabi/include/c++/14.2.1/ext/pb_ds/detail/cc_hash_table_map_/"
        "constructor_destructor_no_store_hash_fn_imps.hpp"
    )
    assert len(longest) == dl._PINNED_LONGEST_MEMBER_CHARS


def test_main_refuses_a_destination_that_holds_other_files(tmp_path: Path, monkeypatch, capsys) -> None:
    dest = tmp_path / "tools"
    (dest / "bin").mkdir(parents=True)
    (dest / "bin" / "my_other_tool.exe").write_bytes(b"mine")
    (dest / "share").mkdir()
    (dest / "share" / "notes.txt").write_bytes(b"mine")
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(dest)]) == 1

    assert calls == []  # refused before the 305 MB download
    assert (dest / "bin" / "my_other_tool.exe").read_bytes() == b"mine"
    assert (dest / "share" / "notes.txt").read_bytes() == b"mine"
    # --dest was probably meant as the parent: the remedy that keeps the folder comes first.
    assert str(dest / "arm-gnu-toolchain") in capsys.readouterr().err


@pytest.mark.parametrize("foreign", ["bin/gcc.exe", "home/notes.txt", "notes.txt"])
def test_main_refuses_a_tools_folder_that_merely_contains_the_toolchain(
    tmp_path: Path, monkeypatch, foreign: str
) -> None:
    # bin\arm-none-eabi-gcc.exe is there, and so is something that is not part of a toolchain:
    # replacing the folder as a whole would delete it.
    dest = tmp_path / "tools"
    _old_toolchain(dest)
    (dest / foreign).parent.mkdir(exist_ok=True)
    (dest / foreign).write_bytes(b"mine")
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(dest)]) == 1

    assert calls == []
    assert (dest / foreign).read_bytes() == b"mine"
    assert (dest / GCC).read_bytes() == b"old-gcc"


def test_a_toolchain_from_arms_installer_counts_as_a_previous_install(tmp_path: Path) -> None:
    dest = tmp_path / "13.3 rel1"
    _old_toolchain(dest)
    (dest / "arm-none-eabi").mkdir()
    (dest / ".version").write_bytes(b"13.3.rel1\n")
    (dest / "bin" / "gccvar.bat").write_bytes(b"@echo off\r\n")
    (dest / "uninstall.exe").write_bytes(b"uninstaller")
    (dest / "13.3.rel1-mingw-w64-i686-arm-none-eabi-manifest.txt").write_bytes(b"manifest\n")

    assert dl._is_previous_install(dest)
    assert dl._destination_problem(dest, 10) is None


def test_an_install_whose_gcc_was_taken_away_can_still_be_reinstalled(tmp_path: Path, monkeypatch) -> None:
    # A virus scanner quarantined arm-none-eabi-gcc.exe: the Studio reports the toolchain as
    # missing, and running this script is what the student is told to do.
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    (dest / GCC).unlink()
    (dest / "bin" / "arm-none-eabi-ld.exe").write_bytes(b"old-ld")
    (dest / "desktop.ini").write_bytes(b"[.ShellClassInfo]\r\n")  # Explorer's, nobody's work
    _serve(monkeypatch)

    assert dl.main(["--dest", str(dest)]) == 0

    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_a_file_of_the_students_next_to_the_toolchain_keeps_the_folder_from_being_replaced(
    tmp_path: Path,
) -> None:
    # Arm's installer leaves "<release>-mingw-w64-i686-arm-none-eabi-manifest.txt"; any other
    # "...-manifest.txt" is somebody's file.
    dest = tmp_path / "tools"
    _old_toolchain(dest)
    (dest / "13.3.rel1-mingw-w64-i686-arm-none-eabi-manifest.txt").write_bytes(b"Arm's\n")
    assert dl._is_previous_install(dest)

    (dest / "homework-manifest.txt").write_bytes(b"mine\n")
    assert not dl._is_previous_install(dest)


def test_a_folder_of_libraries_is_not_an_install_to_replace(tmp_path: Path) -> None:
    # include\ and lib\ are names a toolchain uses too; without its programs it is someone's SDK.
    sdk = tmp_path / "sdk"
    for folder in ("bin", "include", "lib"):
        (sdk / folder).mkdir(parents=True)
    (sdk / "lib" / "libmine.a").write_bytes(b"mine")

    assert not dl._is_previous_install(sdk)
    assert dl._destination_problem(sdk, 10) is not None


def test_a_folder_that_may_not_be_read_is_not_a_toolchain(tmp_path: Path, monkeypatch) -> None:
    def denied(self: Path) -> bool:
        raise PermissionError(13, "Access is denied", str(self))

    monkeypatch.setattr(Path, "is_file", denied)

    assert dl._is_toolchain(tmp_path) is False


def test_main_refuses_a_destination_that_is_a_file(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "arm-gnu-toolchain"
    dest.write_bytes(b"not a folder")
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(dest)]) == 1

    assert calls == []
    assert dest.read_bytes() == b"not a folder"


def test_main_refuses_the_top_of_a_drive(tmp_path: Path, monkeypatch, capsys) -> None:
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", tmp_path.anchor]) == 1

    assert calls == []
    # Refused for what it is, with a place that works: a standard account may not create the
    # downloaded zip directly in C:\, so the example sits one folder further down.
    err = capsys.readouterr().err
    assert "最上層" in err
    assert str(Path(tmp_path.anchor) / "NuTeach_Tools" / "arm-gnu-toolchain") in err
    # A drive that does not exist holds no files: only the name itself can refuse it.
    assert "最上層" in dl._destination_problem(Path("Q:\\"), dl._PINNED_LONGEST_MEMBER_CHARS)


def test_a_destination_swallowed_by_a_closing_quote_is_refused_before_downloading(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # --dest "D:\my tools\gcc\" --keep-zip reaches Python as ONE argument, the backslash having
    # escaped the quote: D:\my tools\gcc" --keep-zip
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(tmp_path / "my tools" / 'gcc" --keep-zip')]) == 1

    assert calls == []
    assert "反斜線" in capsys.readouterr().err
    assert not (tmp_path / "my tools").exists()


@pytest.mark.parametrize("typed", ["gcc ", "gcc.", "tools /gcc"])
def test_a_folder_name_ending_in_a_space_or_dot_is_refused_before_downloading(
    tmp_path: Path, monkeypatch, capsys, typed: str
) -> None:
    # Windows drops them when it creates the folder: the name typed and the name on disk would
    # differ, and the leftovers of a killed run could not be matched to the destination again.
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(tmp_path / "t") + "\\" + typed.replace("/", "\\")]) == 1

    assert calls == []
    assert "結尾" in capsys.readouterr().err
    assert not (tmp_path / "t").exists()


def test_a_refused_run_leaves_no_empty_folders(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "new1" / "new2" / "arm-gnu-toolchain"
    _serve(monkeypatch)
    open_path = Path.open

    def no_new_files(self: Path, mode: str = "r", *args, **kwargs):
        if self.parent == dest.parent and mode[0] in "wx":
            raise PermissionError(13, "Permission denied", str(self))
        return open_path(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_new_files)

    assert dl.main(["--dest", str(dest)]) == 1

    assert _names(tmp_path) == []


def test_the_folder_the_offline_hint_names_is_still_there(tmp_path: Path, monkeypatch, capsys) -> None:
    # --dest in a folder that did not exist, on a network that cannot reach Arm: the hint says
    # to put the zip into that folder, so the run must not have taken it away again.
    dest = tmp_path / "NuTeach_Tools" / "arm-gnu-toolchain"

    def offline(url: str, destination: Path, *, label: str, accept=None) -> None:
        raise OSError("getaddrinfo failed")

    monkeypatch.setattr(dl, "_download", offline)

    assert dl.main(["--dest", str(dest)]) == 1

    assert str(dest.parent) in capsys.readouterr().err
    assert dest.parent.is_dir()


def test_a_disk_that_is_too_full_is_refused_before_the_download(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)
    monkeypatch.setattr(dl, "_free_bytes", lambda folder: 500_000_000)

    assert dl.main(["--dest", str(dest)]) == 1

    assert calls == []
    assert "500 MB" in capsys.readouterr().err
    assert not dest.exists()


def test_the_space_needed_counts_the_download_only_when_there_is_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dl, "_free_bytes", lambda folder: dl._INSTALLED_BYTES)

    dl._require_space(tmp_path, download=False)  # the zip is already here
    with pytest.raises(RuntimeError, match="1,565 MB"):
        dl._require_space(tmp_path, download=True)


@pytest.mark.parametrize("empty", [False, True])
def test_main_refuses_to_replace_the_folder_the_console_is_in(
    tmp_path: Path, monkeypatch, empty: bool
) -> None:
    # Windows would refuse the rename only after the download; say so before it.
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    if empty:
        dest.mkdir(parents=True)
    else:
        _old_toolchain(dest)
    monkeypatch.chdir(dest if empty else dest / "bin")
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(dest)]) == 1

    assert calls == []
    assert empty or (dest / GCC).read_bytes() == b"old-gcc"


def test_main_accepts_an_empty_destination_folder(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    dest.mkdir(parents=True)
    _serve(monkeypatch)

    assert dl.main(["--dest", str(dest)]) == 0
    assert (dest / GCC).is_file()


# --------------------------------------------------------------------------- main()
def test_main_installs_from_what_arm_actually_serves(tmp_path: Path, monkeypatch, capsys) -> None:
    """The checksum line and the zip layout both as published, not as once assumed."""

    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _serve(monkeypatch)

    assert dl.main(["--dest", str(dest)]) == 0
    assert (dest / GCC).is_file()
    assert _names(dest.parent) == ["arm-gnu-toolchain"]
    # No toolchain is found by the (replaced) probe: the ending that explains where the
    # Studio looks and how to point it at this one.
    out = capsys.readouterr().out
    assert "偵測不到" in out and "setx NUTEACH_ARM_GCC_BIN" in out


def test_main_cleans_up_on_checksum_mismatch(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _serve(monkeypatch, build=_build_fake_archive, checksum_line="0" * 64 + "  {name}\n")

    exit_code = dl.main(["--dest", str(dest)])

    assert exit_code != 0
    assert not dest.exists()
    assert not (tmp_path / "runtime" / dl.ARCHIVE_NAME).exists()
    assert not (tmp_path / "runtime" / (dl.ARCHIVE_NAME + ".sha256asc")).exists()


def test_main_succeeds_with_matching_checksum(tmp_path: Path, monkeypatch, capsys) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _serve(monkeypatch, build=_build_fake_archive, checksum_line="{digest}  {name}\n")
    probed = {}

    def fake_probe() -> dict:
        probed["called"] = True
        return {"available": True, "bin_dir": str(dest / "bin"), "version": "fake 14.2.1", "source": "local",
                "checked": []}

    monkeypatch.setattr(dl, "probe_toolchain", fake_probe)

    exit_code = dl.main(["--dest", str(dest), "--keep-zip"])

    assert exit_code == 0
    assert (dest / GCC).is_file()
    assert probed.get("called") is True
    assert "[OK] 偵測到 Arm GNU Toolchain：fake 14.2.1" in capsys.readouterr().out
    # --keep-zip must leave the downloaded archive and checksum file in place.
    assert (tmp_path / "runtime" / dl.ARCHIVE_NAME).exists()
    assert (tmp_path / "runtime" / (dl.ARCHIVE_NAME + ".sha256asc")).exists()


def test_main_without_keep_zip_removes_downloads(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _serve(monkeypatch, build=_build_fake_archive, checksum_line="{digest}  {name}\n")

    exit_code = dl.main(["--dest", str(dest)])

    assert exit_code == 0
    assert (dest / GCC).is_file()
    assert not (tmp_path / "runtime" / dl.ARCHIVE_NAME).exists()
    assert not (tmp_path / "runtime" / (dl.ARCHIVE_NAME + ".sha256asc")).exists()


def test_main_keeps_a_zip_the_student_already_had_when_the_download_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    work_dir = tmp_path / "runtime"
    work_dir.mkdir()
    hand_downloaded = work_dir / dl.ARCHIVE_NAME
    hand_downloaded.write_bytes(b"fetched by hand on another network")

    def blocked(url: str, destination: Path, *, label: str, accept=None) -> None:
        raise OSError("getaddrinfo failed")

    monkeypatch.setattr(dl, "_download", blocked)

    assert dl.main(["--dest", str(work_dir / "arm-gnu-toolchain")]) == 1
    assert hand_downloaded.read_bytes() == b"fetched by hand on another network"
    # The message says how to install without network: which file, and where to put it.
    err = capsys.readouterr().err
    assert dl.archive_url() in err and str(work_dir) in err


def test_a_zip_already_next_to_the_destination_is_used_without_any_network(
    tmp_path: Path, monkeypatch
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    archive_path = dest.parent / dl.ARCHIVE_NAME
    _build_flat_archive(archive_path)
    monkeypatch.setattr(dl, "PINNED_SHA256", hashlib.sha256(archive_path.read_bytes()).hexdigest())
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(dest)]) == 0

    assert calls == []
    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"
    # The student's own file: used, and never deleted.
    assert _names(dest.parent) == ["arm-gnu-toolchain", dl.ARCHIVE_NAME]


def test_a_zip_that_is_not_the_pinned_release_is_replaced_by_a_download(
    tmp_path: Path, monkeypatch
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    dest.parent.mkdir()
    (dest.parent / dl.ARCHIVE_NAME).write_bytes(b"half a download")
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(dest)]) == 0

    assert calls == [dl.ARCHIVE_NAME, dl.ARCHIVE_NAME + ".sha256asc"]
    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"


def test_a_download_that_is_the_pinned_release_needs_no_checksum_file(
    tmp_path: Path, monkeypatch
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    published = _pin(monkeypatch, tmp_path)
    calls: list[str] = []
    _serve(monkeypatch, build=lambda destination: shutil.copyfile(published, destination), calls=calls)

    assert dl.main(["--dest", str(dest)]) == 0

    # One request, not two: a second one could only fail.
    assert calls == [dl.ARCHIVE_NAME]
    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_a_login_page_served_instead_of_the_zip_is_called_what_it_is(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # A school network's portal or filter answers every URL with its own page, as "200 OK".
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    calls: list[str] = []

    def portal(url: str, destination: Path, *, label: str, accept=None) -> None:
        calls.append(destination.name)
        page = "<html><body>請先登入校園網路</body></html>".encode("big5")
        _deliver(destination, lambda part: part.write_bytes(page), accept)

    monkeypatch.setattr(dl, "_download", portal)

    assert dl.main(["--dest", str(dest)]) == 1

    # Recognised from the "zip" itself; no second request is needed to find out.
    assert calls == [dl.ARCHIVE_NAME]
    err = capsys.readouterr().err
    assert "不是完整的 zip" in err and "登入" in err
    assert dl.archive_url() in err and str(dest.parent) in err  # the way without this network
    # The folder the hint names is there to take the zip, and nothing else is in it.
    assert _names(dest.parent) == []


def test_a_checksum_file_that_is_not_arms_is_reported_in_plain_words(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"

    def filtered(url: str, destination: Path, *, label: str, accept=None) -> None:
        if destination.name == dl.ARCHIVE_NAME:
            _deliver(destination, _build_flat_archive, accept)  # a real zip, not the pinned bytes
        else:
            page = "<html>這個網站已被封鎖</html>".encode("big5")  # not UTF-8
            _deliver(destination, lambda part: part.write_bytes(page), accept)

    monkeypatch.setattr(dl, "_download", filtered)

    assert dl.main(["--dest", str(dest)]) == 1

    err = capsys.readouterr().err
    assert "校驗檔" in err
    assert "codec" not in err and "sha256asc contents" not in err
    assert _names(dest.parent) == []


def test_a_zip_the_student_brought_is_not_replaced_by_a_login_page(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Not the pinned bytes, so a download is attempted -- and the school network answers with
    # its own page. Only a verified download may take the place of the student's file.
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    brought = dest.parent / dl.ARCHIVE_NAME
    _build_fake_archive(brought)
    before = brought.read_bytes()

    def portal(url: str, destination: Path, *, label: str, accept=None) -> None:
        page = b"<html><body>Please sign in</body></html>"
        _deliver(destination, lambda part: part.write_bytes(page), accept)

    monkeypatch.setattr(dl, "_download", portal)

    assert dl.main(["--dest", str(dest)]) == 1

    assert brought.read_bytes() == before
    assert _names(dest.parent) == [dl.ARCHIVE_NAME]
    assert "登入" in capsys.readouterr().err


def test_a_zip_that_is_still_being_copied_is_reported_in_plain_words(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # The teacher's copy of the zip into runtime\ has not finished: Windows refuses to open it.
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    brought = dest.parent / dl.ARCHIVE_NAME
    _build_flat_archive(brought)

    def held(path: Path) -> str:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(dl, "_sha256_of", held)

    assert dl.main(["--dest", str(dest)]) == 1

    err = capsys.readouterr().err
    assert str(brought) in err and "稍後" in err
    assert "Errno" not in err
    assert brought.is_file()


def test_a_verified_download_is_kept_for_the_next_run_when_installing_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    published = _pin(monkeypatch, tmp_path)
    calls: list[str] = []
    _serve(monkeypatch, build=lambda destination: shutil.copyfile(published, destination), calls=calls)

    with monkeypatch.context() as patch:
        _refuse_renaming(patch, ("arm-gnu-toolchain",))  # a build is using the old toolchain
        assert dl.main(["--dest", str(dest)]) == 1

    assert (dest / GCC).read_bytes() == b"old-gcc"
    assert _names(dest.parent) == ["arm-gnu-toolchain", dl.ARCHIVE_NAME]
    # The student is told the 305 MB are still there, and where.
    assert str(dest.parent / dl.ARCHIVE_NAME) in capsys.readouterr().out

    calls.clear()
    assert dl.main(["--dest", str(dest)]) == 0

    assert calls == []  # no second 305 MB download
    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"


def test_main_returns_130_and_tidies_up_on_ctrl_c(tmp_path: Path, monkeypatch, capsys) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"

    def interrupted(url: str, destination: Path, *, label: str, accept=None) -> None:
        if destination.name == dl.ARCHIVE_NAME:
            _deliver(destination, _build_flat_archive, accept)
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(dl, "_download", interrupted)

    assert dl.main(["--dest", str(dest)]) == 130
    assert _names(dest.parent) == []  # neither a toolchain nor a download
    assert "沒有安裝任何東西" in capsys.readouterr().err


def test_a_download_arm_republished_is_installed_with_a_warning(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Not the bytes this script records, but what Arm's own checksum file names today.
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(dest)]) == 0

    assert calls == [dl.ARCHIVE_NAME, dl.ARCHIVE_NAME + ".sha256asc"]
    assert "不是同一份" in capsys.readouterr().out
    assert (dest / GCC).is_file()


def test_a_disk_that_fills_up_while_extracting_is_named_as_such(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _serve(monkeypatch)

    def disk_full(archive: Path, destination: Path) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(dl, "safe_extract_zip", disk_full)

    assert dl.main(["--dest", str(dest)]) == 1

    err = capsys.readouterr().err
    assert "磁碟滿了" in err and "Errno" not in err
    assert (dest / GCC).read_bytes() == b"old-gcc"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_part_files_of_a_killed_download_are_cleared(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    published = _pin(monkeypatch, tmp_path)
    dest.parent.mkdir()
    shutil.copyfile(published, dest.parent / dl.ARCHIVE_NAME)  # the zip is here: no download
    for name in (dl.ARCHIVE_NAME, dl.ARCHIVE_NAME + ".sha256asc"):
        (dest.parent / (name + ".part")).write_bytes(b"half")
    _serve(monkeypatch)

    assert dl.main(["--dest", str(dest)]) == 0

    assert _names(dest.parent) == ["arm-gnu-toolchain", dl.ARCHIVE_NAME]


def test_ctrl_c_during_extraction_leaves_no_staging_folder(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _serve(monkeypatch)
    extract = dl.safe_extract_zip

    def interrupted_after_extracting(archive: Path, destination: Path) -> None:
        extract(archive, destination)
        raise KeyboardInterrupt

    monkeypatch.setattr(dl, "safe_extract_zip", interrupted_after_extracting)

    assert dl.main(["--dest", str(dest)]) == 130
    assert (dest / GCC).read_bytes() == b"old-gcc"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_ctrl_c_keeps_a_download_that_is_already_verified(tmp_path: Path, monkeypatch, capsys) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    published = _pin(monkeypatch, tmp_path)
    _serve(monkeypatch, build=lambda destination: shutil.copyfile(published, destination))

    def interrupted(archive: Path, destination: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(dl, "safe_extract_zip", interrupted)

    assert dl.main(["--dest", str(dest)]) == 130

    # Interrupting the extraction does not cost a second 305 MB download, and the run says so.
    assert _names(dest.parent) == [dl.ARCHIVE_NAME]
    assert str(dest.parent / dl.ARCHIVE_NAME) in capsys.readouterr().out


@pytest.mark.parametrize("step", ["_tidy_interrupted_runs", "_destination_problem", "_report_probe"])
def test_ctrl_c_at_any_step_ends_the_run_without_a_traceback(
    tmp_path: Path, monkeypatch, capsys, step: str
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _serve(monkeypatch)

    def interrupted(*args, **kwargs) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(dl, step, interrupted)

    assert dl.main(["--dest", str(dest)]) == 130

    assert "可以使用" in capsys.readouterr().err
    assert (dest / GCC).is_file()
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_a_second_ctrl_c_while_the_first_is_answered_is_not_a_traceback(
    tmp_path: Path, monkeypatch
) -> None:
    def interrupted(*args, **kwargs) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(dl, "_tidy_interrupted_runs", interrupted)
    monkeypatch.setattr(dl, "_state_on_disk", interrupted)

    assert dl.main(["--dest", str(tmp_path / "runtime" / "arm-gnu-toolchain")]) == 130


def test_a_kept_zip_survives_ctrl_c_after_the_install(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _serve(monkeypatch)

    def interrupted() -> dict:
        raise KeyboardInterrupt

    monkeypatch.setattr(dl, "probe_toolchain", interrupted)

    assert dl.main(["--dest", str(dest), "--keep-zip"]) == 130

    assert (dest / GCC).is_file()
    assert (dest.parent / dl.ARCHIVE_NAME).is_file()


def test_a_failed_run_never_deletes_a_toolchain_it_did_not_install(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # A second console window finishes its own install while this run is still downloading,
    # and this run then loses the network: the other run's toolchain has to stay.
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"

    def other_run_finishes_then_offline(url: str, destination: Path, *, label: str, accept=None) -> None:
        if destination.name == dl.ARCHIVE_NAME:
            _old_toolchain(dest)
            _deliver(destination, _build_flat_archive, accept)
            return
        raise OSError("connection reset")

    monkeypatch.setattr(dl, "_download", other_run_finishes_then_offline)

    assert dl.main(["--dest", str(dest)]) == 1
    assert (dest / GCC).read_bytes() == b"old-gcc"
    # It says which of the two downloads failed.
    assert "校驗檔失敗" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to delete a file that is open")
def test_a_zip_that_cannot_be_deleted_afterwards_does_not_undo_the_install(
    tmp_path: Path, monkeypatch
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _serve(monkeypatch)
    install = dl._extract_and_install
    held = []

    def install_then_hold_the_zip(archive_path: Path, target: Path) -> None:
        install(archive_path, target)
        held.append(archive_path.open("rb"))  # an antivirus scan of the fresh download

    monkeypatch.setattr(dl, "_extract_and_install", install_then_hold_the_zip)
    try:
        assert dl.main(["--dest", str(dest)]) == 0
        assert (dest / GCC).is_file()
        assert (tmp_path / "runtime" / dl.ARCHIVE_NAME).exists()
    finally:
        for handle in held:
            handle.close()


def test_main_names_the_env_var_when_another_toolchain_is_found_first(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "tools" / "arm-gnu-toolchain"
    _serve(monkeypatch)
    elsewhere = tmp_path / "Program Files" / "13.3 rel1" / "bin"
    monkeypatch.setattr(dl, "probe_toolchain", lambda: {
        "available": True, "bin_dir": str(elsewhere), "version": "13.3.1", "source": "program_files",
        "checked": [],
    })

    assert dl.main(["--dest", str(dest)]) == 0

    out = capsys.readouterr().out
    # The command to run, not just the variable's name: `set` in a console would not reach a
    # Studio started by double-clicking 02_START.bat.
    assert f'setx NUTEACH_ARM_GCC_BIN "{dest / "bin"}"' in out


def test_no_setx_line_is_offered_for_a_path_cmd_would_rewrite(tmp_path: Path, monkeypatch, capsys) -> None:
    # Pasted into cmd, "%TEMP%" inside the line would be replaced by the variable's value.
    dest = tmp_path / "%TEMP%" / "arm-gnu-toolchain"
    _serve(monkeypatch)

    assert dl.main(["--dest", str(dest)]) == 0

    out = capsys.readouterr().out
    assert "NUTEACH_ARM_GCC_BIN" in out and str(dest / "bin") in out
    assert "setx NUTEACH_ARM_GCC_BIN" not in out


def test_main_says_so_when_the_gcc_it_installed_does_not_run(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # The probe looked at the new install and moved on: pointing NUTEACH_ARM_GCC_BIN at it, the
    # advice for a toolchain the Studio does not look for, would not help.
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _serve(monkeypatch)
    monkeypatch.setattr(dl, "probe_toolchain", lambda: {
        "available": False, "bin_dir": None, "version": None, "source": None,
        "checked": [str(dest / "bin")],
    })

    assert dl.main(["--dest", str(dest)]) == 0

    out = capsys.readouterr().out
    assert str(dest / GCC) in out and "NUTEACH_ARM_GCC_BIN" not in out


# --------------------------------------------------------------------------- the offline route, slightly off
def test_a_checksum_file_the_student_brought_is_never_deleted(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    dest.parent.mkdir()
    theirs = dest.parent / (dl.ARCHIVE_NAME + ".sha256asc")
    theirs.write_bytes(b"fetched by hand")
    _serve(monkeypatch)  # not the pinned bytes, so Arm's checksum file is asked for

    assert dl.main(["--dest", str(dest)]) == 0

    # Arm's own went next to it under another name and is gone again; theirs is as it was.
    assert theirs.read_bytes() == b"fetched by hand"
    assert _names(dest.parent) == sorted(["arm-gnu-toolchain", theirs.name])


def test_a_copy_the_browser_renamed_is_used_too(tmp_path: Path, monkeypatch) -> None:
    # Downloading the same file a second time gives "... (1).zip"; the bytes are what counts.
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    published = _pin(monkeypatch, tmp_path)
    dest.parent.mkdir()
    renamed = dest.parent / dl.ARCHIVE_NAME.replace(".zip", " (1).zip")
    shutil.copyfile(published, renamed)
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(dest)]) == 0

    assert calls == []
    assert (dest / GCC).read_bytes() == b"fake-gcc-binary"
    assert _names(dest.parent) == sorted(["arm-gnu-toolchain", renamed.name])


def test_a_zip_put_inside_the_destination_folder_is_pointed_one_level_up(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    dest.mkdir(parents=True)
    _build_flat_archive(dest / dl.ARCHIVE_NAME)
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(dest)]) == 1

    assert calls == []
    err = capsys.readouterr().err
    assert "放錯位置" in err and str(dest.parent) in err
    assert (dest / dl.ARCHIVE_NAME).is_file()


def test_a_toolchain_wrapped_in_one_more_folder_gets_advice_of_its_own(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # What unpacking a zip by hand leaves, and what the Studio's own probe accepts.
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    wrapped = dest / "arm-gnu-toolchain-13.3.rel1-mingw-w64-i686-arm-none-eabi"
    _old_toolchain(wrapped)
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(dest)]) == 1

    assert calls == []
    assert "多包了一層" in capsys.readouterr().err
    assert (wrapped / GCC).read_bytes() == b"old-gcc"


def test_replacing_an_existing_toolchain_is_announced_before_the_download(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Only the top level and bin\ are looked at: what the student keeps deeper inside goes too.
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    (dest / "lib" / "my_own_library.a").write_bytes(b"mine")
    _serve(monkeypatch)

    assert dl.main(["--dest", str(dest)]) == 0

    out = capsys.readouterr().out
    assert "整個換掉" in out
    assert out.index("整個換掉") < out.index("準備下載")


@pytest.mark.parametrize("empty_folder_there", [False, True])
def test_a_first_install_announces_no_replacement(
    tmp_path: Path, monkeypatch, capsys, empty_folder_there: bool
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    if empty_folder_there:
        dest.mkdir(parents=True)
    _serve(monkeypatch)

    assert dl.main(["--dest", str(dest)]) == 0

    assert "整個換掉" not in capsys.readouterr().out


# --------------------------------------------------------------------------- what the PC does to the files
def test_a_tree_with_a_file_missing_is_never_swapped_in(tmp_path: Path, monkeypatch, capsys) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _serve(monkeypatch)
    extract = dl.safe_extract_zip

    def extract_then_lose_a_file(archive: Path, destination: Path) -> None:
        extract(archive, destination)
        (destination / "lib" / "README.txt").unlink()  # not gcc.exe: the tree still "looks" fine

    monkeypatch.setattr(dl, "safe_extract_zip", extract_then_lose_a_file)

    assert dl.main(["--dest", str(dest)]) == 1

    err = capsys.readouterr().err
    assert "lib/README.txt" in err and "防毒" in err
    assert (dest / GCC).read_bytes() == b"old-gcc"
    assert _names(dest.parent) == ["arm-gnu-toolchain"]


def test_a_file_cut_short_on_the_disk_counts_as_missing(tmp_path: Path) -> None:
    archive_path = tmp_path / dl.ARCHIVE_NAME
    _build_flat_archive(archive_path)
    extracted = tmp_path / "tree"
    dl.safe_extract_zip(archive_path, extracted)
    with zipfile.ZipFile(archive_path) as zf:
        members = zf.infolist()
    assert dl._first_missing(members, extracted) is None

    (extracted / GCC).write_bytes(b"fake")  # shorter than what the archive holds

    assert dl._first_missing(members, extracted) == "bin/arm-none-eabi-gcc.exe"


def test_a_report_that_fails_does_not_undo_the_install(tmp_path: Path, monkeypatch, capsys) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _serve(monkeypatch)

    def broken() -> dict:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(dl, "probe_toolchain", broken)

    assert dl.main(["--dest", str(dest)]) == 0

    assert (dest / GCC).is_file()
    assert "已經裝好" in capsys.readouterr().out


def test_the_studio_folder_path_warning_is_shown(tmp_path: Path, monkeypatch, capsys) -> None:
    _serve(monkeypatch)
    monkeypatch.setattr(dl, "long_path_reason", lambda root: "Studio 資料夾路徑太長")

    assert dl.main(["--dest", str(tmp_path / "runtime" / "arm-gnu-toolchain")]) == 0

    assert "Studio 資料夾路徑太長" in capsys.readouterr().out


# --------------------------------------------------------------------------- the command line
def test_a_path_with_a_space_and_no_quotes_is_answered_in_chinese(capsys) -> None:
    with pytest.raises(SystemExit) as stop:
        dl.main(["--dest", "D:\\my", "tools\\arm-gnu-toolchain"])

    assert stop.value.code == 2
    assert "引號" in capsys.readouterr().err


def test_help_survives_a_percent_sign_in_the_studio_path(tmp_path: Path, monkeypatch, capsys) -> None:
    # argparse %-formats help strings; the default destination is part of one.
    monkeypatch.setattr(dl, "LOCAL_TOOLCHAIN_ROOT", tmp_path / "AI 100%_class" / "runtime" / "arm-gnu-toolchain")

    with pytest.raises(SystemExit) as stop:
        dl.main(["--help"])

    assert stop.value.code == 0
    assert "100%_class" in capsys.readouterr().out


def test_a_destination_under_a_file_is_refused_in_plain_words(tmp_path: Path, monkeypatch, capsys) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"mine")
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(notes / "arm-gnu-toolchain")]) == 1

    assert calls == []
    assert "無法建立資料夾" in capsys.readouterr().err
    assert notes.read_bytes() == b"mine"


def test_a_destination_that_cannot_be_looked_into_is_refused_in_plain_words(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    def denied(target: Path, longest_member: int) -> None:
        raise PermissionError(13, "Permission denied", str(target))

    monkeypatch.setattr(dl, "_destination_problem", denied)

    assert dl.main(["--dest", str(dest)]) == 1

    assert calls == []
    err = capsys.readouterr().err
    assert "無法檢查" in err and "Permission denied" not in err


# --------------------------------------------------------------------------- the download, on this PC's side
def _fake_urlopen(monkeypatch, body: bytes) -> None:
    class Response:
        def __init__(self) -> None:
            self.headers = {"Content-Length": str(len(body))}
            self.chunks = [body, b""]

        def read(self, size: int) -> bytes:
            return self.chunks.pop(0)

    @contextlib.contextmanager
    def urlopen(request, timeout):
        yield Response()

    monkeypatch.setattr(dl.urllib.request, "urlopen", urlopen)


def test_a_part_file_that_cannot_be_created_is_this_pcs_problem(tmp_path: Path, monkeypatch, capsys) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _fake_urlopen(monkeypatch, b"never read")
    monkeypatch.setattr(dl, "probe_toolchain", lambda: {"available": False})
    open_path = Path.open

    def no_part_file(self: Path, mode: str = "r", *args, **kwargs):
        if self.name.endswith(".part"):
            raise PermissionError(13, "Permission denied", str(self))
        return open_path(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_part_file)

    assert dl.main(["--dest", str(dest)]) == 1

    err = capsys.readouterr().err
    assert "無法建立下載檔" in err
    assert "developer.arm.com" not in err  # nothing is wrong with the network


def test_a_disk_that_fills_up_during_the_download_is_this_pcs_problem(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _fake_urlopen(monkeypatch, b"x" * 1000)
    monkeypatch.setattr(dl, "probe_toolchain", lambda: {"available": False})
    open_path = Path.open

    class FullDisk(io.BytesIO):
        def write(self, data) -> int:
            raise OSError(28, "No space left on device")

    def full(self: Path, mode: str = "r", *args, **kwargs):
        if self.name.endswith(".part"):
            return FullDisk()
        return open_path(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", full)

    assert dl.main(["--dest", str(dest)]) == 1

    err = capsys.readouterr().err
    assert "空間" in err
    assert "developer.arm.com" not in err


def test_a_connection_that_breaks_the_http_rules_is_a_failed_download(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"

    def broken(url: str, destination: Path, *, label: str, accept=None) -> None:
        raise http.client.IncompleteRead(b"half")

    monkeypatch.setattr(dl, "_download", broken)

    assert dl.main(["--dest", str(dest)]) == 1

    err = capsys.readouterr().err
    assert "下載失敗" in err and dl.archive_url() in err


@pytest.mark.parametrize("checksum_line", ["\ufeff{digest} *{name}\n", "{digest} *{name}\r\n"])
def test_a_checksum_file_with_a_bom_or_windows_line_ends_is_read(
    tmp_path: Path, monkeypatch, checksum_line: str
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _serve(monkeypatch, checksum_line=checksum_line)

    assert dl.main(["--dest", str(dest)]) == 0


def test_a_checksum_in_capital_letters_matches(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _serve(monkeypatch, digest_case=str.upper)

    assert dl.main(["--dest", str(dest)]) == 0


# --------------------------------------------------------------------------- space, with the zip already here
@pytest.mark.parametrize("free", [1_400_000_000, None])
def test_the_download_is_not_counted_when_the_zip_is_already_here(
    tmp_path: Path, monkeypatch, free: int | None
) -> None:
    # Enough for the tree, not for tree + zip; and a disk whose free space cannot be asked.
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    published = _pin(monkeypatch, tmp_path)
    dest.parent.mkdir()
    shutil.copyfile(published, dest.parent / dl.ARCHIVE_NAME)
    _serve(monkeypatch)
    monkeypatch.setattr(dl, "_free_bytes", lambda folder: free)

    assert dl.main(["--dest", str(dest)]) == 0


# --------------------------------------------------------------------------- names and links
@pytest.mark.skipif(sys.platform != "win32", reason="Windows folder names ignore letter case")
def test_a_parked_toolchain_is_found_when_the_destination_is_typed_in_capitals(tmp_path: Path) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(_leftover(dest, "old"))

    dl._tidy_interrupted_runs(dest.with_name("ARM-GNU-Toolchain"))

    assert (dest / GCC).read_bytes() == b"old-gcc"
    assert len(_names(dest.parent)) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows folder names ignore letter case")
def test_an_install_spelled_in_capitals_is_still_an_install(tmp_path: Path) -> None:
    dest = tmp_path / "TC"
    (dest / "BIN").mkdir(parents=True)
    (dest / "BIN" / "ARM-NONE-EABI-GCC.EXE").write_bytes(b"gcc")
    (dest / "LIB").mkdir()
    (dest / "13.3.REL1-MINGW-W64-I686-ARM-NONE-EABI-MANIFEST.TXT").write_bytes(b"Arm's")

    assert dl._is_previous_install(dest)


def test_putting_the_old_toolchain_back_waits_for_a_scanner_to_let_go(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    parked = _leftover(dest, "old")
    _old_toolchain(parked)
    monkeypatch.setattr(dl, "_RENAME_RETRY_SECONDS", 0)
    rename = Path.rename
    refusals = {"left": 2}

    def busy_at_first(self: Path, target: Path) -> Path:
        if refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError(13, "Access is denied", str(self))
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", busy_at_first)

    dl._put_back(parked, dest)

    assert (dest / GCC).read_bytes() == b"old-gcc"


@pytest.mark.skipif(sys.platform != "win32", reason="needs an NTFS junction")
def test_a_junction_named_like_a_leftover_is_not_followed(tmp_path: Path) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    dest.parent.mkdir()
    elsewhere = tmp_path / "my-own-toolchain"
    _old_toolchain(elsewhere)
    link = _leftover(dest, "old")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(elsewhere)], capture_output=True, check=False
    )
    if made.returncode != 0:
        pytest.skip("this account may not create junctions")

    dl._tidy_interrupted_runs(dest)

    # Not taken for the student's parked toolchain and renamed into place, nor deleted.
    assert not dest.exists()
    assert (elsewhere / GCC).read_bytes() == b"old-gcc"
    assert (elsewhere / "lib" / "old_only.txt").is_file()
    assert _names(dest.parent) == [link.name]


# --------------------------------------------------------------------------- found by the last review
def test_a_brought_copy_is_named_when_the_run_ends(tmp_path: Path, monkeypatch, capsys) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    published = _pin(monkeypatch, tmp_path)
    dest.parent.mkdir()
    renamed = dest.parent / dl.ARCHIVE_NAME.replace(".zip", " (1).ZIP").upper()  # capitals count too
    shutil.copyfile(published, renamed)
    _serve(monkeypatch)

    assert dl.main(["--dest", str(dest)]) == 0

    out = capsys.readouterr().out
    assert f"原本就在的壓縮檔沒有刪除；不再需要時可以手動刪除：{renamed}" in out
    assert renamed.is_file()


def test_the_properly_named_zip_is_looked_at_first(tmp_path: Path, monkeypatch, capsys) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    published = _pin(monkeypatch, tmp_path)
    dest.parent.mkdir()
    proper = dest.parent / dl.ARCHIVE_NAME
    proper.write_bytes(b"a download that broke off")
    second = dest.parent / dl.ARCHIVE_NAME.replace(".zip", " (1).zip")
    earlier = dest.parent / dl.ARCHIVE_NAME.replace(".zip", " - copy.zip")  # sorts before the proper name
    for copy in (second, earlier):
        shutil.copyfile(published, copy)
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(dest)]) == 0

    out = capsys.readouterr().out
    assert calls == []
    assert out.index(f"這裡已經有 {proper.name}，") < out.index(f"這裡已經有 {second.name}，")
    assert proper.read_bytes() == b"a download that broke off"  # not used, and not touched either


def test_a_checksum_file_alone_in_the_destination_is_not_a_misplaced_zip(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    dest.mkdir(parents=True)
    (dest / (dl.ARCHIVE_NAME + ".sha256asc")).write_bytes(b"fetched by hand")
    _serve(monkeypatch)

    assert dl.main(["--dest", str(dest)]) == 1

    err = capsys.readouterr().err
    assert "放錯位置" not in err  # there is no zip in there to move
    assert str(dest / "arm-gnu-toolchain") in err


def test_a_wrapped_toolchain_without_gcc_still_gets_the_wrapped_advice(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    wrapped = dest / "arm-gnu-toolchain-13.3.rel1-mingw-w64-i686-arm-none-eabi"
    _old_toolchain(wrapped)
    (wrapped / GCC).unlink()
    (wrapped / "bin" / "arm-none-eabi-ld.exe").write_bytes(b"old-ld")
    _serve(monkeypatch)

    assert dl.main(["--dest", str(dest)]) == 1

    assert "多包了一層" in capsys.readouterr().err


def test_an_error_nobody_foresaw_is_handed_to_the_teacher(tmp_path: Path, monkeypatch, capsys) -> None:
    # zipfile's own complaints are RuntimeErrors in English: not the script's wording.
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    _serve(monkeypatch)

    def unsupported(archive: Path, destination: Path) -> None:
        raise NotImplementedError("That compression method is not supported")

    monkeypatch.setattr(dl, "safe_extract_zip", unsupported)

    assert dl.main(["--dest", str(dest)]) == 1

    err = capsys.readouterr().err
    assert "沒有預期到的錯誤" in err and "老師" in err
    assert (dest / GCC).read_bytes() == b"old-gcc"


def test_an_archive_that_names_a_file_twice_is_not_taken_for_a_scanner_at_work(tmp_path: Path) -> None:
    import warnings

    archive_path = tmp_path / "downloads" / dl.ARCHIVE_NAME
    archive_path.parent.mkdir()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about the duplicate name
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("bin/arm-none-eabi-gcc.exe", b"fake-gcc-binary")
            zf.writestr("share/twice.txt", b"first")
            zf.writestr("share/twice.txt", b"the second, longer one")
            zf.writestr("share/Readme.txt", b"one")
            zf.writestr("share/README.TXT", b"another, as Windows sees it")
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"

    dl._extract_and_install(archive_path, dest)

    assert (dest / "share" / "twice.txt").read_bytes() == b"the second, longer one"


def test_a_zip_the_student_brought_is_replaced_by_a_verified_download_and_then_kept(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Arm republished the file: the download differs from the recorded digest but matches Arm's
    # checksum file. It takes the place of what the student had -- and is theirs from then on.
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    _old_toolchain(dest)
    brought = dest.parent / dl.ARCHIVE_NAME
    brought.write_bytes(b"an older copy")
    _serve(monkeypatch)

    with monkeypatch.context() as patch:
        _refuse_renaming(patch, ("arm-gnu-toolchain",))  # the install itself then fails
        assert dl.main(["--dest", str(dest)]) == 1

    assert zipfile.is_zipfile(brought)  # the verified download, not deleted with the failure
    assert _names(dest.parent) == ["arm-gnu-toolchain", dl.ARCHIVE_NAME]
    capsys.readouterr()

    assert dl.main(["--dest", str(dest)]) == 0

    assert brought.is_file()
    assert "沒有刪除" in capsys.readouterr().out


def test_a_pinned_download_that_replaced_the_students_zip_is_kept_after_the_install(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = tmp_path / "runtime" / "arm-gnu-toolchain"
    published = _pin(monkeypatch, tmp_path)
    dest.parent.mkdir()
    brought = dest.parent / dl.ARCHIVE_NAME
    brought.write_bytes(b"a download that broke off")
    _serve(monkeypatch, build=lambda destination: shutil.copyfile(published, destination))

    assert dl.main(["--dest", str(dest)]) == 0

    # Replaced, as announced -- and not deleted with the run's own downloads.
    assert brought.read_bytes() == published.read_bytes()
    assert f"沒有刪除：{brought}" in capsys.readouterr().out


def test_an_existing_folder_typed_with_a_trailing_space_is_refused_too(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Windows resolves the name to the real folder; the path printed (and the setx line) would
    # then come out with a "\\?\" prefix.
    (tmp_path / "tools").mkdir()
    calls: list[str] = []
    _serve(monkeypatch, calls=calls)

    assert dl.main(["--dest", str(tmp_path) + "\\tools \\arm-gnu-toolchain"]) == 1

    assert calls == []
    assert "結尾" in capsys.readouterr().err
    assert _names(tmp_path / "tools") == []


def test_an_abbreviated_option_is_not_guessed(tmp_path: Path, monkeypatch, capsys) -> None:
    # "--keep" must not start an install as "--keep-zip". Should it ever be accepted again,
    # the run this test starts stays inside the test folder.
    monkeypatch.setattr(dl, "LOCAL_TOOLCHAIN_ROOT", tmp_path / "runtime" / "arm-gnu-toolchain")
    _serve(monkeypatch)
    with pytest.raises(SystemExit) as stop:
        dl.main(["--keep"])

    assert stop.value.code == 2
    assert "參數有誤" in capsys.readouterr().err


def test_a_stalled_download_does_not_keep_ctrl_c_waiting_for_minutes() -> None:
    # Windows does not deliver Ctrl+C while a socket read is waiting: the read timeout is the wait.
    assert dl._DOWNLOAD_TIMEOUT <= 60


@pytest.mark.skipif(sys.platform != "win32", reason="the console code page question is a Windows one")
def test_error_lines_are_utf8_when_the_output_goes_to_a_file(tmp_path: Path) -> None:
    # "> log.txt 2>&1" to send to the teacher: one encoding, not half UTF-8 and half Big5.
    # As a student's console has it: nothing in the environment that already asks for UTF-8.
    plain = {key: value for key, value in os.environ.items() if key not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    done = subprocess.run(
        [sys.executable, "-B", dl.__file__, "--dest", tmp_path.anchor],
        capture_output=True, check=False, env=plain,
    )

    assert done.returncode == 1
    assert "最上層" in done.stderr.decode("utf-8")
