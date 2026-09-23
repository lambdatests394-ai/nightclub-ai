"""Bounded original-byte verification. Worker threads never access persistence."""
import asyncio
from contextlib import aclosing
from dataclasses import dataclass
import hashlib
from io import BytesIO
import re
import warnings
from struct import error as struct_error

from PIL import Image, UnidentifiedImageError

from backend.app.modules.assets.storage import StorageInvalidResponse, StorageProvider, StorageTimeout

FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
GEO_PATTERN = re.compile(r"(?:\bgps\b|gps[\s:_-]?(?:info|latitude|longitude|position|altitude|dest|timestamp|datestamp|mapdatum)|geo(?:location|tag)|location(?:latitude|longitude)|geo:(?:lat|long))", re.I)


@dataclass(frozen=True)
class Verification:
    rejection_code: str | None = None
    width: int | None = None
    height: int | None = None


def decode_image(data: bytes, expected_mime: str) -> Verification:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format not in FORMAT_MIME:
                    return Verification("unsupported_format")
                if FORMAT_MIME[image.format] != expected_mime:
                    return Verification("mime_mismatch")
                width, height = image.size
                if not (0 < width <= 8192 and 0 < height <= 8192 and width * height <= 20_000_000):
                    return Verification("dimension_limit_exceeded")
                if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
                    return Verification("animated_image")
                # EXIF GPS IFD (0x8825), including GPS nested inside Exif IFD.
                exif = image.getexif()
                if 0x8825 in exif or 0x8825 in exif.get_ifd(0x8769):
                    return Verification("gps_metadata_present")
                metadata = " ".join(str(k) + " " + (v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v))
                                    for k, v in image.info.items() if k not in {"icc_profile", "exif"})
                if GEO_PATTERN.search(metadata):
                    return Verification("gps_metadata_present")
            # Metadata inspection can consume PNG's fp. verify() requires a fresh open.
            with Image.open(BytesIO(data)) as container:
                container.verify()
            with Image.open(BytesIO(data)) as decoded:
                decoded.load()
            return Verification(width=width, height=height)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return Verification("dimension_limit_exceeded")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError, KeyError, TypeError, struct_error):
        return Verification("invalid_image")


async def verify_object(provider: StorageProvider, *, bucket: str, key: str, expected_size: int,
                        expected_sha: str, expected_mime: str, max_bytes: int, timeout: int) -> Verification:
    try:
        async with asyncio.timeout(timeout):
            metadata = await provider.stat_object(bucket, key)
            if metadata.bucket != bucket or metadata.key != key:
                raise StorageInvalidResponse()
            digest, data = hashlib.sha256(), bytearray()
            async with aclosing(provider.stream_object(bucket, key)) as stream:
                async for chunk in stream:
                    if not isinstance(chunk, bytes):
                        raise StorageInvalidResponse()
                    if len(data) + len(chunk) > max_bytes:
                        return Verification("size_limit_exceeded")
                    digest.update(chunk)
                    data.extend(chunk)
            if len(data) != expected_size:
                return Verification("size_mismatch")
            if digest.hexdigest() != expected_sha:
                return Verification("sha256_mismatch")
            # Off-loop decode; cancellation can discard its bounded result, never commit it.
            return await asyncio.to_thread(decode_image, bytes(data), expected_mime)
    except TimeoutError:
        raise StorageTimeout() from None
