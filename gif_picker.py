"""Choose a valid GIF at random from the user's reaction folder."""

from __future__ import annotations

import secrets
import struct
from pathlib import Path


MAX_GIF_BYTES = 10_000_000
MAX_GIF_SIDE = 2000


def valid_gifs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    valid = []
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.lower() != ".gif":
            continue
        try:
            if not 10 <= path.stat().st_size <= MAX_GIF_BYTES:
                continue
            with path.open("rb") as stream:
                header = stream.read(10)
        except OSError:
            continue
        if header[:6] not in (b"GIF87a", b"GIF89a"):
            continue
        width, height = struct.unpack("<HH", header[6:10])
        if 0 < width <= MAX_GIF_SIDE and 0 < height <= MAX_GIF_SIDE:
            valid.append(path)
    return valid


def choose_gif(folder: Path) -> Path | None:
    candidates = valid_gifs(folder)
    return secrets.choice(candidates) if candidates else None
