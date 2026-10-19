"""Detect a file's real type from its content. File names and extensions are not trusted."""

PDF = "application/pdf"
JPEG = "image/jpeg"
PNG = "image/png"
HEIC = "image/heic"
WEBP = "image/webp"
UNKNOWN = "application/octet-stream"

_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"}


def sniff_mime(data: bytes) -> str:
    head = data[:32]
    if head.startswith(b"%PDF-"):
        return PDF
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head[4:8] == b"ftyp" and head[8:12] in _HEIC_BRANDS:
        return HEIC
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return WEBP
    return UNKNOWN
