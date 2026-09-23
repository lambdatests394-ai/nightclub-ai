"""Pure filename validation and deterministic canonical object naming."""
import re
import unicodedata
from uuid import UUID

MIME_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
DANGEROUS = frozenset({
    "exe", "com", "bat", "cmd", "ps1", "msi", "dll", "scr", "vbs", "js", "mjs",
    "html", "htm", "xhtml", "svg", "php", "py", "sh", "jar", "lnk", "hta", "cpl",
})


def sanitize_filename(filename: str, mime_type: str) -> str:
    if len(filename.encode("utf-8")) > 255:
        raise ValueError("Invalid filename")
    name = unicodedata.normalize("NFKC", filename)
    if (not name.strip() or len(name.encode("utf-8")) > 255 or name.lstrip().startswith(".")
            or any(ch in "/\\:" or unicodedata.category(ch).startswith("C") for ch in name)):
        raise ValueError("Invalid filename")
    parts = name.lower().split(".")
    if any(part.strip() in DANGEROUS for part in parts[1:]):
        raise ValueError("Prohibited filename extension")
    if mime_type not in MIME_EXTENSIONS:
        raise ValueError("Unsupported image type")
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode().lower()
    stem = re.sub("-+", "-", re.sub("[^a-z0-9_-]+", "-", stem)).strip("-_")[:80].rstrip("-_") or "asset"
    return stem + "." + MIME_EXTENSIONS[mime_type]


def storage_key(organization_id: UUID, asset_id: UUID, filename: str, mime_type: str) -> str:
    return f"org/{organization_id}/assets/{asset_id}/{sanitize_filename(filename, mime_type)}"
