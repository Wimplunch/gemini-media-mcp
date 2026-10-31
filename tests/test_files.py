from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from gemini_media_mcp.files import (
    FileSafetyError,
    load_input_image,
    resolve_input_path,
    slugify,
    sniff_image_type,
    write_new_output,
    write_sidecar,
)

from .conftest import JPEG, PNG

MB = 1024 * 1024


@pytest.fixture
def allowed(tmp_path: Path) -> list[Path]:
    d = tmp_path / "allowed"
    d.mkdir()
    return [d.resolve()]


def test_reads_valid_image(allowed: list[Path]) -> None:
    (allowed[0] / "face.png").write_bytes(PNG)
    img = load_input_image(str(allowed[0] / "face.png"), allowed, MB)
    assert img.mime_type == "image/png"
    assert img.data == PNG
    assert len(img.sha256) == 64


def test_relative_path_is_relative_to_first_allowed_dir(allowed: list[Path]) -> None:
    (allowed[0] / "logo.jpg").write_bytes(JPEG)
    assert load_input_image("logo.jpg", allowed, MB).mime_type == "image/jpeg"


def test_blocks_path_traversal(tmp_path: Path, allowed: list[Path]) -> None:
    (tmp_path / "secret.png").write_bytes(PNG)
    with pytest.raises(FileSafetyError, match="outside the allowed"):
        resolve_input_path("../secret.png", allowed)
    with pytest.raises(FileSafetyError, match="outside the allowed"):
        resolve_input_path(str(tmp_path / "secret.png"), allowed)


def test_blocks_symlink_escape(tmp_path: Path, allowed: list[Path]) -> None:
    target = tmp_path / "outside.png"
    target.write_bytes(PNG)
    (allowed[0] / "link.png").symlink_to(target)
    with pytest.raises(FileSafetyError, match="outside the allowed"):
        resolve_input_path(str(allowed[0] / "link.png"), allowed)


def test_symlink_inside_allowed_is_fine(allowed: list[Path]) -> None:
    real = allowed[0] / "real.png"
    real.write_bytes(PNG)
    (allowed[0] / "alias.png").symlink_to(real)
    assert resolve_input_path(str(allowed[0] / "alias.png"), allowed) == real


def test_symlinked_allowed_prefix_trick(tmp_path: Path, allowed: list[Path]) -> None:
    # /allowed-evil must not count as inside /allowed
    evil = tmp_path / "allowed-evil"
    evil.mkdir()
    (evil / "x.png").write_bytes(PNG)
    with pytest.raises(FileSafetyError, match="outside"):
        resolve_input_path(str(evil / "x.png"), allowed)


@pytest.mark.parametrize("raw", ["", "a\x00b"])
def test_rejects_empty_or_nul(allowed: list[Path], raw: str) -> None:
    with pytest.raises(FileSafetyError, match="invalid path"):
        resolve_input_path(raw, allowed)


def test_missing_file_and_directory(allowed: list[Path]) -> None:
    with pytest.raises(FileSafetyError, match="not found"):
        resolve_input_path("nope.png", allowed)
    (allowed[0] / "sub").mkdir()
    with pytest.raises(FileSafetyError, match="Not a regular file"):
        resolve_input_path("sub", allowed)


def test_symlink_loop(allowed: list[Path]) -> None:
    loop = allowed[0] / "loop.png"
    loop.symlink_to(loop)
    with pytest.raises(FileSafetyError):
        resolve_input_path(str(loop), allowed)


def test_size_limit(allowed: list[Path]) -> None:
    (allowed[0] / "big.png").write_bytes(PNG + b"\x00" * MB)
    with pytest.raises(FileSafetyError, match="limit"):
        load_input_image("big.png", allowed, MB)
    (allowed[0] / "empty.png").write_bytes(b"")
    with pytest.raises(FileSafetyError, match="Empty"):
        load_input_image("empty.png", allowed, MB)


def test_mime_checked_by_content_not_extension(allowed: list[Path]) -> None:
    (allowed[0] / "fake.png").write_text("#!/bin/sh\necho hi\n")
    with pytest.raises(FileSafetyError, match="not a supported image"):
        load_input_image("fake.png", allowed, MB)


@pytest.mark.parametrize(
    ("head", "mime"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"\x00\x00\x00\x18ftypheic", "image/heic"),
        (b"\x00\x00\x00\x18ftypmif1", "image/heif"),
        (b"GIF89a", None),
        (b"\x00\x00\x00\x18ftypmp42", None),
    ],
)
def test_sniff(head: bytes, mime: str | None) -> None:
    assert sniff_image_type(head) == mime


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("Ein Hund im Büro!", "ein-hund-im-buro"),
        ("../../etc/passwd", "etc-passwd"),
        ("   ", "output"),
        ("a" * 80, "a" * 40),
    ],
)
def test_slugify(text: str, slug: str) -> None:
    assert slugify(text) == slug


def test_write_never_overwrites(tmp_path: Path) -> None:
    now = datetime(2026, 10, 31, 9, 5, 7)
    first = write_new_output(tmp_path, "cat", "image/png", b"one", now)
    second = write_new_output(tmp_path, "cat", "image/png", b"two", now)
    assert first.name == "20261031-090507_cat.png"
    assert second.name == "20261031-090507_cat-2.png"
    assert first.read_bytes() == b"one"
    # An existing sidecar also blocks the stem.
    (tmp_path / "20261031-090507_cat-3.json").write_text("{}")
    third = write_new_output(tmp_path, "cat", "image/png", b"three", now)
    assert third.name == "20261031-090507_cat-4.png"


def test_write_extension_by_mime(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1)
    assert write_new_output(tmp_path, "v", "video/mp4", b"x", now).suffix == ".mp4"
    assert write_new_output(tmp_path, "j", "image/jpeg", b"x", now).suffix == ".jpg"
    assert write_new_output(tmp_path, "u", "application/x-weird", b"x", now).suffix == ".bin"


def test_write_gives_up_eventually(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "exists", lambda self: True)
    with pytest.raises(FileSafetyError, match="free file name"):
        write_new_output(tmp_path, "x", "image/png", b"x", datetime(2026, 1, 1))


def test_sidecar_never_overwrites(tmp_path: Path) -> None:
    media = tmp_path / "a.png"
    media.write_bytes(b"x")
    sidecar = write_sidecar(media, {"prompt": "Grüße"})
    assert sidecar.name == "a.json"
    assert "Grüße" in sidecar.read_text("utf-8")
    with pytest.raises(FileExistsError):
        write_sidecar(media, {})


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX only")
def test_reads_through_nofollow(allowed: list[Path]) -> None:
    (allowed[0] / "ok.png").write_bytes(PNG)
    assert load_input_image("ok.png", allowed, MB).data == PNG
