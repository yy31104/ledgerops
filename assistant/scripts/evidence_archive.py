"""Package immutable raw evidence for a release; restore only checksum-verified files."""

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
PREFIXES = ("assistant/eval/results/", "assistant/perf/results/")
OMIT = {
    "per_question",
    "answers",
    "samples_ms",
    "per_query",
    "raw_text",
    "explain",
    "ann_explain",
    "batch_profile",
    "profile",
    "context_profile",
    "histogram",
    "over_512_first_20",
    "top_five",
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def safe_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or path.as_posix() != name:
        raise ValueError("unsafe archive path")
    if not name.startswith(PREFIXES) or path.suffix not in (".json", ".csv"):
        raise ValueError("unexpected evidence path")
    return path


def summary(value):
    if isinstance(value, dict):
        return {key: summary(item) for key, item in value.items() if key not in OMIT}
    if isinstance(value, list):
        # Individual samples, answers and ranked IDs stay in the release asset.
        return (
            value
            if len(value) <= 10 and all(not isinstance(v, (dict, list)) for v in value)
            else {"entries": len(value)}
        )
    return value


def pack(root, output, tag, repository="yy31104/ledgerops"):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag):
        raise ValueError("invalid release tag")
    root, output = Path(root), Path(output)
    paths = sorted(
        path
        for prefix in PREFIXES
        for path in (root / prefix).glob("*")
        if path.is_file() and path.suffix in (".json", ".csv") and path.name != "summary.json"
    )
    if not paths:
        raise ValueError("no raw evidence files found")
    records, payloads, summaries = {}, {}, {}
    for path in paths:
        if path.is_symlink():
            raise ValueError("refusing symlink evidence source")
        name = path.relative_to(root).as_posix()
        safe_path(name)
        payload = path.read_bytes()
        records[name] = {"sha256": sha256(payload), "bytes": len(payload)}
        payloads[name] = payload
        if path.suffix == ".json":
            summaries[name] = summary(json.loads(payload))
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    contents = {"schema_version": 1, "source_commit": commit, "files": records}
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, data in [("manifest.json", canonical(contents)), *payloads.items()]:
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o644, 0
                archive.addfile(info, io.BytesIO(data))
    asset = f"ledgerops-evidence-{tag}.tar.gz"
    data = buffer.getvalue()
    manifest = {
        **contents,
        "release_tag": tag,
        "asset": asset,
        "url": f"https://github.com/{repository}/releases/download/{tag}/{asset}",
        "archive_sha256": sha256(data),
        "archive_bytes": len(data),
    }
    output.mkdir(parents=True, exist_ok=True)
    for name, artifact in [
        (asset, data),
        ("evidence-manifest.json", canonical(manifest)),
        ("summary.json", canonical({"schema_version": 1, "records": summaries})),
    ]:
        target = output / name
        if target.exists() and target.read_bytes() != artifact:
            raise ValueError(f"refusing to replace different release artifact: {name}")
        target.write_bytes(artifact)
    return manifest


def restore(manifest, cache, archive=None):
    """Reject traversal, links, extra files, corruption and symlink destinations."""
    manifest = (
        json.loads(Path(manifest).read_text()) if not isinstance(manifest, dict) else manifest
    )
    if manifest.get("schema_version") != 1 or not manifest.get("files"):
        raise ValueError("unsupported or empty manifest")
    for name in manifest["files"]:
        safe_path(name)
    if archive is None:
        url = urlsplit(manifest["url"])
        if url.scheme != "https" or url.hostname != "github.com" or url.username or url.password:
            raise ValueError("evidence URL must be a public HTTPS GitHub release asset")
        with urlopen(manifest["url"], timeout=60) as response:
            data = response.read(manifest["archive_bytes"] + 1)
    else:
        data = Path(archive).read_bytes()
    if len(data) != manifest["archive_bytes"] or sha256(data) != manifest["archive_sha256"]:
        raise ValueError("release archive checksum/size mismatch")
    cache = Path(cache)
    expected = manifest["files"]
    staged = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as contents:
        members = contents.getmembers()
        if len(members) != len(expected) + 1 or {m.name for m in members} != {
            "manifest.json",
            *expected,
        }:
            raise ValueError("archive membership mismatch")
        for member in members:
            if not member.isfile():
                raise ValueError("archive contains a nonregular file")
            if member.name == "manifest.json":
                embedded = json.load(contents.extractfile(member))
                if embedded != {
                    key: manifest[key] for key in ("schema_version", "source_commit", "files")
                }:
                    raise ValueError("embedded manifest mismatch")
                continue
            record = expected[member.name]
            if member.size != record["bytes"]:
                raise ValueError("evidence file size mismatch")
            payload = contents.extractfile(member).read()
            if sha256(payload) != record["sha256"]:
                raise ValueError("evidence file checksum mismatch")
            target = cache / safe_path(member.name)
            if any(p.is_symlink() for p in [target, *target.parents]):
                raise ValueError("symlink evidence destination")
            if target.exists() and target.read_bytes() != payload:
                raise ValueError("refusing to replace modified evidence")
            staged[target] = payload
    # No writes until every archive entry has been verified.
    for target, payload in staged.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
            temporary.write(payload)
            temporary_path = Path(temporary.name)
        temporary_path.replace(target)
    return sorted(path.relative_to(cache).as_posix() for path in staged)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("pack")
    create.add_argument("--root", type=Path, default=ROOT)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--tag", required=True)
    create.add_argument("--repository", default="yy31104/ledgerops")
    fetch = commands.add_parser("restore")
    fetch.add_argument("--manifest", type=Path, required=True)
    fetch.add_argument("--cache", type=Path, required=True)
    fetch.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.command == "pack":
        result = pack(args.root, args.output, args.tag, args.repository)
        print(f"Packaged {len(result['files'])} immutable records: {result['asset']}")
    else:
        result = restore(args.manifest, args.cache, args.archive)
        print(f"Restored {len(result)} checksum-verified records")


if __name__ == "__main__":
    main()
