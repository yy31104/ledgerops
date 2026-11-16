"""Export an unrated 32-answer record as CSV and optional readable Markdown."""

import argparse
import csv
import html
import json
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote

COHORT_IDS = [f"v1-{i:03}" for i in range(1, 33)]

JUDGEMENT = "judgement (complete/incomplete/incorrect)"

FIELDS = (
    "id",
    "question",
    "required_facts",
    "provided_sources",
    "answer",
    "run_status",
    JUDGEMENT,
)


def csv_text(value):
    """Keep model/source text literal in spreadsheet programs, including leading controls."""
    text = str(value)
    leading = text.lstrip(" \t\r\n\v\f\ufeff")
    if leading.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def source_url(source, revision):
    anchor = source["section_id"].partition("#")[2]
    return (
        "https://github.com/yy31104/ledgerops/blob/"
        + quote(revision.split("+")[0], safe="")
        + "/"
        + quote(source["path"], safe="/")
        + "#"
        + quote(anchor, safe="-_")
    )


def export_review(record, csv_path, markdown_path=None):
    rows = record["answers"]
    if [row["id"] for row in rows] != COHORT_IDS:
        raise ValueError("human review export requires the original 32-question cohort")
    if any(row.get("judgement") not in (None, "") for row in rows):
        raise ValueError("record already contains human judgements")
    paths = [csv_path] + ([markdown_path] if markdown_path else [])
    if len({p.resolve() for p in paths}) != len(paths):
        raise ValueError("export paths must be distinct")
    created = []
    try:
        with ExitStack() as stack:
            streams = []
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                streams.append(
                    stack.enter_context(
                        path.open(
                            "x", encoding="utf-8-sig" if path == csv_path else "utf-8", newline=""
                        )
                    )
                )
                created.append(path)
            writer = csv.DictWriter(streams[0], fieldnames=FIELDS)
            writer.writeheader()
            markdown = streams[1] if markdown_path else None
            if markdown:
                markdown.write(
                    "# Human answer review\n\n"
                    "Rate each answer complete, incomplete or incorrect. "
                    "All judgements are blank.\n"
                )
            for row in rows:
                sources = [
                    f"[{source['chunk_id']}]\n"
                    f"{source_url(source, record['corpus_revision'])}\n{source['text']}"
                    for source in row.get("provided_sources", [])
                ]
                answer = row.get("result", {}).get("text", "")
                status = row["status"] + (": " + row["error_type"] if "error_type" in row else "")
                facts = "\n".join(f"{i}. {fact}" for i, fact in enumerate(row["key_facts"], 1))
                values = {
                    "id": row["id"],
                    "question": row["question"],
                    "required_facts": facts,
                    "provided_sources": "\n\n".join(sources),
                    "answer": answer,
                    "run_status": status,
                    JUDGEMENT: "",
                }
                writer.writerow({key: csv_text(value) for key, value in values.items()})
                if markdown:
                    markdown.write(f"\n## {html.escape(row['id'])}\n\n")
                    for label, value in [
                        ("Question", row["question"]),
                        ("Required facts", facts),
                        ("Answer", answer),
                        ("Run status", status),
                    ]:
                        markdown.write(f"### {label}\n\n<pre>{html.escape(value)}</pre>\n\n")
                    markdown.write("Judgement (complete / incomplete / incorrect): \n\n")
                    for source in row.get("provided_sources", []):
                        url = html.escape(source_url(source, record["corpus_revision"]), quote=True)
                        label = html.escape(source["chunk_id"])
                        markdown.write(
                            f'<details><summary><a href="{url}">{label}</a></summary>\n\n<pre>'
                            + html.escape(source["text"])
                            + "</pre>\n\n</details>\n"
                        )
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    count = export_review(json.loads(args.record.read_text()), args.csv, args.markdown)
    print(f"Exported {count} rows with blank human judgements.")


if __name__ == "__main__":
    main()
