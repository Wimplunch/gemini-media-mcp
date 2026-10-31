"""File-system safety: allowed directories, input image checks, collision-free output."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Input formats we accept, detected from the file's magic bytes (never from the extension).
SUPPORTED_INPUT_TYPES = ("image/png", "image/jpeg", "image/webp", "image/heic", "image/heif")

_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
}


class FileSafetyError(ValueError):
    """A path or file was rejected (outside allowed dirs, wrong type, too large...)."""


@dataclass(frozen=True)
class InputImage:
    path: Path
    data: bytes
    mime_type: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def is_within(path: Path, allowed_dirs: Sequence[Path]) -> bool:
    return any(path == d or path.is_relative_to(d) for d in allowed_dirs)


def resolve_input_path(raw: str, allowed_dirs: Sequence[Path]) -> Path:
    """Resolve a user-supplied path (following symlinks) and require it inside allowed_dirs.

    Relative paths are taken relative to the first allowed directory.
    """
    if not raw or "\x00" in raw:
        raise FileSafetyError("Empty or invalid path")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = allowed_dirs[0] / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise FileSafetyError(f"File not found: {raw}") from None
    except (OSError, RuntimeError) as exc:  # RuntimeError: symlink loop
        raise FileSafetyError(f"Cannot resolve path {raw}: {exc.__class__.__name__}") from None
    if not is_within(resolved, allowed_dirs):
        raise FileSafetyError(
            f"Access denied: {raw} is outside the allowed directories "
            "(set GEMINI_MEDIA_ALLOWED_DIRS to allow more)"
        )
    if not resolved.is_file():
        raise FileSafetyError(f"Not a regular file: {raw}")
    return resolved


def sniff_image_type(head: bytes) -> str | None:
    """Return the MIME type for supported image formats based on magic bytes."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"heim", b"heis"):
            return "image/heic"
        if brand in (b"mif1", b"msf1", b"heif"):
            return "image/heif"
    return None


def load_input_image(raw: str, allowed_dirs: Sequence[Path], max_bytes: int) -> InputImage:
    """Validate and read one input image: location, size and real file type."""
    path = resolve_input_path(raw, allowed_dirs)
    size = path.stat().st_size
    if size == 0:
        raise FileSafetyError(f"Empty file: {raw}")
    if size > max_bytes:
        raise FileSafetyError(
            f"{raw} is {size / 1_048_576:.1f} MB; the limit is {max_bytes / 1_048_576:.1f} MB "
            "(GEMINI_MEDIA_MAX_INPUT_MB)"
        )
    # O_NOFOLLOW: refuse if the final component was swapped for a symlink after the check.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise FileSafetyError(f"{raw} grew beyond the size limit while reading")
    mime = sniff_image_type(data[:32])
    if mime is None:
        raise FileSafetyError(
            f"{raw} is not a supported image. Supported: PNG, JPEG, WebP, HEIC/HEIF"
        )
    return InputImage(path=path, data=data, mime_type=mime)


def slugify(text: str, max_len: int = 40, fallback: str = "output") -> str:
    """ASCII slug for file names: 'Ein Hund im Büro!' -> 'ein-hund-im-buro'."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    slug = slug[:max_len].rstrip("-")
    return slug or fallback


def extension_for(mime_type: str) -> str:
    return _EXTENSIONS.get(mime_type, ".bin")


def write_new_output(
    directory: Path, slug: str, mime_type: str, data: bytes, now: datetime
) -> Path:
    """Write data to '<timestamp>_<slug><ext>' without ever replacing an existing file.

    The matching sidecar name ('<stem>.json') must be free too, so both can be written.
    """
    ext = extension_for(mime_type)
    base = f"{now.strftime('%Y%m%d-%H%M%S')}_{slug}"
    for attempt in range(1, 1000):
        stem = base if attempt == 1 else f"{base}-{attempt}"
        target = directory / f"{stem}{ext}"
        if target.with_suffix(".json").exists():
            continue
        try:
            with target.open("xb") as fh:  # 'x' fails if the file exists
                fh.write(data)
        except FileExistsError:
            continue
        return target
    raise FileSafetyError(f"Could not find a free file name for {base}{ext}")


def write_sidecar(media_path: Path, metadata: dict[str, Any]) -> Path:
    """Write provenance metadata next to the media file ('<stem>.json'), never overwriting."""
    sidecar = media_path.with_suffix(".json")
    with sidecar.open("x", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return sidecar
