"""Member photos: webcam captures and uploaded files, normalized the same way.

Photos are downscaled and re-encoded as JPEG on the way in. That strips EXIF
(including any location tags a phone upload might carry) and keeps a roster of
several hundred students to a few tens of megabytes.
"""
from __future__ import annotations

import base64
import binascii
import io
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.config import get_settings

MAX_EDGE = 640
JPEG_QUALITY = 85


class PhotoError(ValueError):
    pass


def photo_dir() -> Path:
    directory = get_settings().photo_dir
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_image_bytes(raw: bytes, member_id: int) -> str:
    """Persist an image and return the filename to store on the member row."""
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise PhotoError("That file is not a readable image.") from exc

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image.thumbnail((MAX_EDGE, MAX_EDGE))

    filename = f"member-{member_id}-{uuid.uuid4().hex[:8]}.jpg"
    image.save(photo_dir() / filename, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return filename


def save_data_url(data_url: str, member_id: int) -> str:
    """Persist a `data:image/...;base64,...` string, as the webcam sends it."""
    if not data_url:
        raise PhotoError("No image data received.")
    payload = data_url.split(",", 1)[-1] if data_url.startswith("data:") else data_url
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PhotoError("Image data was not valid base64.") from exc
    return save_image_bytes(raw, member_id)


def delete_photo(filename: str | None) -> None:
    if not filename:
        return
    # Guard against a stored value ever being used to escape the photo dir.
    target = (photo_dir() / Path(filename).name).resolve()
    if target.parent == photo_dir().resolve():
        target.unlink(missing_ok=True)
