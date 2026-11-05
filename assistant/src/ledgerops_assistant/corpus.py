"""Load the LedgerOps docs and cut them into citable chunks.

A chunk never crosses a heading, so every chunk belongs to exactly one section and can
be cited as ``path#anchor``. The anchor uses GitHub's heading slug, so a citation is
also a working link. Long sections are split on paragraph boundaries; each piece keeps
the document and section title at the top so it still makes sense on its own.
"""

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

CHUNKER_VERSION = "headings-v1"
MAX_WORDS = 220

# Versioned operating/investigation/evidence sources from the reviewed study.
# New files require a label review and a new development baseline. Deployment
# guides, rename proposals and assistant evaluation material are not auto-indexed.
CORPUS_FILES = (
    "README.md",
    "docs/ACTIVE-HOLD-INDEX.md",
    "docs/AUTHENTICATION.md",
    "docs/BACKUP-RESTORE.md",
    "docs/EVIDENCE-EXCHANGE.md",
    "docs/EXPORT-AND-VERIFY.md",
    "docs/HTTP-SERVICE.md",
    "docs/MAINTENANCE-CAPACITY.md",
    "docs/MAINTENANCE.md",
    "docs/OPERATOR-ACTIONS.md",
    "docs/PAYOUTS.md",
    "docs/RECONCILIATION.md",
    "docs/RELEASE-READINESS.md",
    "docs/REVERSAL.md",
    "docs/STREAM10.md",
    "docs/STREAMING-EVIDENCE.md",
    "docs/adr/0001-initialization.md",
    "docs/adr/0002-ledger-foundation.md",
    "docs/adr/0003-purchase-admission.md",
    "docs/adr/0004-contention-and-recovery.md",
    "ops/linux/README.md",
)

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str  # path#anchor/part
    section_id: str  # path#anchor, the unit that evaluation labels refer to
    path: str
    title: str  # document title (first heading)
    heading: str  # section heading
    text: str  # what the retrievers and the model see

    @property
    def body(self) -> str:
        return self.text.split("\n\n", 1)[1] if "\n\n" in self.text else self.text


@dataclass(frozen=True)
class Corpus:
    root: Path
    revision: str  # git commit of the docs, with "+dirty" when they differ from it
    chunks: tuple[Chunk, ...]
    file_digests: dict[str, str]

    @property
    def digest(self) -> str:
        h = hashlib.sha256(CHUNKER_VERSION.encode())
        for chunk in self.chunks:
            h.update(b"\0" + chunk.chunk_id.encode() + b"\0" + chunk.text.encode())
        return h.hexdigest()

    def section_ids(self) -> set[str]:
        return {chunk.section_id for chunk in self.chunks}

    def by_id(self) -> dict[str, Chunk]:
        return {chunk.chunk_id: chunk for chunk in self.chunks}


def slug(heading: str) -> str:
    """GitHub's anchor rule: lowercase, drop punctuation, spaces become hyphens."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _sections(text: str):
    """Yield (heading, body lines) pairs, ignoring '#' lines inside code fences."""
    heading, body, fenced = None, [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        match = None if fenced else _HEADING.match(line)
        if match:
            if heading is not None:
                yield heading, body
            heading, body = match.group(2), []
        else:
            body.append(line)
    if heading is not None:
        yield heading, body


def _paragraphs(lines: list[str]) -> list[str]:
    """Blank-line separated blocks; a fenced code block stays one block."""
    blocks, current, fenced = [], [], False
    for line in lines:
        if _FENCE.match(line):
            fenced = not fenced
        if not line.strip() and not fenced:
            if current:
                blocks.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _split_long(block: str) -> list[str]:
    if len(block.split()) <= MAX_WORDS or block.lstrip().startswith(("```", "~~~")):
        return [block]
    sentences = re.split(r"(?<=[.!?])\s+", block)
    pieces, current = [], []
    for sentence in sentences:
        if current and len(" ".join(current + [sentence]).split()) > MAX_WORDS:
            pieces.append(" ".join(current))
            current = []
        current.append(sentence)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _pack(blocks: list[str]) -> list[str]:
    parts, current, words = [], [], 0
    for block in (piece for b in blocks for piece in _split_long(b)):
        size = len(block.split())
        if current and words + size > MAX_WORDS:
            parts.append("\n\n".join(current))
            current, words = [], 0
        current.append(block)
        words += size
    if current:
        parts.append("\n\n".join(current))
    return parts


def chunk_markdown(path: str, text: str) -> list[Chunk]:
    chunks, title, used = [], None, {}
    for heading, lines in _sections(text):
        title = title or heading
        anchor = slug(heading)
        # GitHub disambiguates repeated headings with -1, -2, ...
        count = used.get(anchor, 0)
        used[anchor] = count + 1
        if count:
            anchor = f"{anchor}-{count}"
        section_id = f"{path}#{anchor}"
        label = title if heading == title else f"{title} > {heading}"
        for part, body in enumerate(_pack(_paragraphs(lines))):
            chunks.append(
                Chunk(
                    chunk_id=f"{section_id}/{part}",
                    section_id=section_id,
                    path=path,
                    title=title,
                    heading=heading,
                    text=f"{label}\n\n{body}",
                )
            )
    return chunks


def _git_revision(root: Path, paths: list[str]) -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return head + ("+dirty" if dirty else "")


def repository_root() -> Path:
    return Path(
        os.environ.get("LEDGEROPS_ASSISTANT_CORPUS_ROOT", Path(__file__).resolve().parents[3])
    )


def load_corpus(root: Path | None = None) -> Corpus:
    root = (root or repository_root()).resolve()
    paths = [root / path for path in CORPUS_FILES]
    if any(not p.resolve().is_relative_to(root) for p in paths):
        raise ValueError("corpus document resolves outside the configured root")
    if any(not p.is_file() for p in paths):
        raise FileNotFoundError("a reviewed corpus file is missing")
    chunks, digests = [], {}
    for file in paths:
        relative = file.relative_to(root).as_posix()
        raw = file.read_bytes()
        digests[relative] = hashlib.sha256(raw).hexdigest()
        chunks.extend(chunk_markdown(relative, raw.decode("utf-8")))
    revision = _git_revision(root, list(digests))
    return Corpus(root=root, revision=revision, chunks=tuple(chunks), file_digests=digests)
