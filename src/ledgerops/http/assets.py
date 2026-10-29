"""Only fixed bundled assets may be served; never an arbitrary filesystem path."""

from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True)
class Asset:
    raw: bytes
    content_type: str


ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/console.js": ("console.js", "text/javascript; charset=utf-8"),
    "/assets/console.css": ("console.css", "text/css; charset=utf-8"),
}


def asset_for(path):
    selected = ASSETS.get(path)
    if selected is None:
        return None
    name, mime = selected
    raw = files("ledgerops.http").joinpath("static", name).read_bytes()
    if len(raw) > 65536:
        raise RuntimeError("bundled asset size limit")
    return Asset(raw, mime)
