"""Copy textual evidence with explicitly selected local metadata redacted.

This is a path/hostname portability tool, not a credential scrubber. It never
edits its input, overwrites an output or changes a signed/hashed source artifact.
"""

import argparse
import json
import os
import re
import stat
import tempfile
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path
from urllib.parse import quote

MAX_BYTES = 32 * 1024 * 1024


class RedactionError(ValueError):
    pass


def redact_text(text, prefixes=(), hostnames=()):
    """Replace whole path prefixes, longest first, retaining unrelated facts."""
    replacements = {}
    for prefix, placeholder in prefixes:
        if (
            not prefix
            or not Path(prefix).is_absolute()
            or any(ord(char) < 32 for char in prefix)
            or len(placeholder) > 64
            or not re.fullmatch(r"\$[A-Z][A-Z0-9_]*", placeholder)
        ):
            raise RedactionError("INVALID_PATH_RULE")
        prefix = prefix.rstrip("/\\")
        if not prefix:
            raise RedactionError("ROOT_PATH_RULE_FORBIDDEN")
        for spelling in {
            prefix,
            quote(prefix, safe="/:"),
            json.dumps(prefix)[1:-1],
            escape(prefix, quote=True),
        }:
            if spelling in replacements and replacements[spelling] != placeholder:
                raise RedactionError("CONFLICTING_PATH_RULE")
            replacements[spelling] = placeholder
    # One pass prevents a replacement token from being interpreted as input to
    # another rule. A sibling such as /repo-old must not be mistaken for /repo.
    if replacements:
        pattern = re.compile(
            "(?:"
            + "|".join(re.escape(p) for p in sorted(replacements, key=len, reverse=True))
            + r")(?=$|[/\\\s\"'`<>:;,?\)\]\}])"
        )
        text = pattern.sub(lambda match: replacements[match.group()], text)
    for hostname in hostnames:
        if not hostname or any(ord(char) < 32 for char in hostname):
            raise RedactionError("INVALID_HOSTNAME_RULE")
        text = re.sub(r"(?<![\w.-])" + re.escape(hostname) + r"(?![\w.-])", "redacted-host", text)
    return text


def redact_file(source, destination, *, prefixes=(), hostnames=()):
    """Publish a new0600 text copy; failed validation leaves no destination."""
    source, destination = Path(source), Path(destination)
    descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_BYTES:
            raise RedactionError("BOUNDED_REGULAR_TEXT_REQUIRED")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or b"\0" in raw:
        raise RedactionError("BOUNDED_REGULAR_TEXT_REQUIRED")
    try:
        text = raw.decode("utf-8")
        result = redact_text(text, prefixes, hostnames).encode("utf-8")
        # Validate before and after rather than silently repairing bad evidence.
        if source.suffix.lower() == ".json":
            json.loads(raw)
            json.loads(result)
        elif source.suffix.lower() == ".xml":
            ET.fromstring(raw)
            ET.fromstring(result)
    except (UnicodeError, json.JSONDecodeError, ET.ParseError) as exc:
        raise RedactionError("INVALID_TEXT_EVIDENCE") from exc
    stage = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            stage = Path(stream.name)
            stream.write(result)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic no-replace publication: input==output, existing outputs and
        # symlink destinations fail without changing either existing file.
        os.link(stage, destination)
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)
    return {"changed": result != raw, "input_bytes": len(raw), "output_bytes": len(result)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--prefix", action="append", nargs=2, default=[], metavar=("PATH", "$TOKEN")
    )
    parser.add_argument("--hostname", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                redact_file(args.input, args.output, prefixes=args.prefix, hostnames=args.hostname),
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError):
        print(json.dumps({"error": "EVIDENCE_REDACTION_FAILED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
