"""Split subscription-call input tokens into their parts.

Two methods that check each other:

1. Local counts with the o200k_base tokenizer: our instructions, schema, message
   framing, retrieved passages and question, plus the input sections that Codex adds,
   rendered locally by the bundled CLI (``codex debug prompt-input``, no model call).
2. Controlled calls through the official subscription adapter: the same question
   with 0, 3, 6 or all of its passages; doubled instructions; empty evidence; and
   the v1.1 adapter configuration against an isolated one.

Only section names and counts of the Codex-added input are stored, never its text.

    uv sync --locked --extra subscription --extra measurement
    LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION=1 uv run --locked python -m scripts.token_attribution \\
        --v11-record .cache/evidence/assistant/eval/results/v1.1-human-review.json \\
        --output .cache/token-attribution.json --summary eval/v12-token-attribution.json
"""

import argparse
import json
import random
import statistics
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from ledgerops_assistant.corpus import Chunk
from ledgerops_assistant.providers.base import ANSWER_SCHEMA, SYSTEM_PROMPT, user_message
from ledgerops_assistant.providers.subscription_worker import overrides

MODEL = "gpt-6.1-sol"
DEVELOPER = "Answer using supplied evidence. Do not call tools."
QUESTION_ID = "v1-001"
SEED = 20261117


def encoder():
    import tiktoken

    return tiktoken.get_encoding("o200k_base")


def injected_sections(isolated, count):
    """Codex-added input for one request, by section; text is discarded."""
    from codex_cli_bin import bundled_codex_path

    arguments = [str(bundled_codex_path()), "debug", "prompt-input"]
    for override in (
        *overrides(isolated),
        f'model="{MODEL}"',
        'sandbox_mode="read-only"',
        'approval_policy="never"',
        f'developer_instructions="{DEVELOPER}"',
    ):
        arguments += ["-c", override]
    with tempfile.TemporaryDirectory(prefix="ledgerops-prompt-") as directory:
        rendered = json.loads(
            subprocess.check_output([*arguments, "PROMPT"], cwd=directory, text=True)
        )
    sections = []
    for item in rendered:
        for block in item.get("content") or []:
            text = block.get("text", "")
            if text in (DEVELOPER, "PROMPT"):
                continue
            name = text[1:].split(">")[0].split()[0] if text.startswith("<") else "other"
            sections.append({"section": name, "role": item.get("role"), "tokens": count(text)})
    return sections


def local_parts(question, passages, count):
    message = user_message(question, passages)
    framing = user_message("", [])
    return {
        "instructions": count(SYSTEM_PROMPT),
        "developer_instruction": count(DEVELOPER),
        "schema": count(json.dumps(ANSWER_SCHEMA)),
        "message_framing": count(framing),
        "question": count(user_message(question, [])) - count(framing),
        "context": count(message) - count(user_message(question, [])),
        "message": count(message),
    }


def plan(passages):
    full = len(passages)
    conditions = []
    for repeat in range(4):
        conditions += [
            {"name": "empty", "isolated": False, "passages": None, "repeat": repeat},
            {"name": "full", "isolated": False, "passages": full, "repeat": repeat},
            {"name": "empty", "isolated": True, "passages": None, "repeat": repeat},
            {"name": "full", "isolated": True, "passages": full, "repeat": repeat},
        ]
    for repeat in range(2):
        for size in (0, 3, 6):
            conditions.append(
                {"name": f"passages-{size}", "isolated": True, "passages": size, "repeat": repeat}
            )
        conditions.append(
            {"name": "double-instructions", "isolated": True, "passages": full, "repeat": repeat}
        )
    random.Random(SEED).shuffle(conditions)
    return conditions


def request_text(condition, question, passages):
    if condition["passages"] is None:
        return SYSTEM_PROMPT, user_message("", [])
    instructions = SYSTEM_PROMPT
    if condition["name"] == "double-instructions":
        instructions = SYSTEM_PROMPT + "\n\n" + SYSTEM_PROMPT
    return instructions, user_message(question, passages[: condition["passages"]])


def fit(points):
    """Least squares y = a + b x."""
    xs, ys = [x for x, _ in points], [y for _, y in points]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    b = sum((x - mx) * (y - my) for x, y in points) / sum((x - mx) ** 2 for x in xs)
    a = my - b * mx
    residual = max(abs(y - (a + b * x)) for x, y in points)
    return {"intercept": round(a, 1), "slope": round(b, 4), "max_abs_residual": round(residual, 1)}


def summarize(record, history, count):
    calls = [c for c in record["calls"] if c.get("status") == "recorded"]

    def inputs(name, isolated):
        return [
            c["usage"]["input_tokens"]
            for c in calls
            if c["name"] == name and c["isolated"] is isolated
        ]

    passages = [Chunk(**source) for source in record["question"]["provided_sources"]]
    question = record["question"]["question"]
    points = [
        (
            local_parts(question, passages[: c["passages"]], count)["context"],
            c["usage"]["input_tokens"],
        )
        for c in calls
        if c["isolated"] and c["name"] in ("passages-0", "passages-3", "passages-6", "full")
    ]
    single = [
        c["usage"]["input_tokens"] == c["usage"]["last_request"]["input_tokens"] for c in calls
    ]
    conditions = {
        f"{name} ({'isolated' if isolated else 'v1.1 adapter'})": inputs(name, isolated)
        for name in ("empty", "full", "passages-0", "passages-3", "passages-6")
        for isolated in (False, True)
        if inputs(name, isolated)
    }
    conditions["double-instructions (isolated)"] = inputs("double-instructions", True)
    measured_instructions = statistics.mean(inputs("double-instructions", True)) - statistics.mean(
        inputs("full", True)
    )
    parts = local_parts(question, passages, count)
    regression = fit(points)
    ours = (
        parts["instructions"]
        + parts["developer_instruction"]
        + parts["schema"]
        + parts["message_framing"]
        + parts["question"]
    )
    default_sections = record["injected"]["v1.1 adapter"]
    isolated_sections = record["injected"]["isolated"]
    default_injected = sum(s["tokens"] for s in default_sections)
    isolated_injected = sum(s["tokens"] for s in isolated_sections)
    empty_default = inputs("empty", False)
    empty_isolated = inputs("empty", True)
    answers = []
    for row in history["answers"]:
        sources = [Chunk(**s) for s in row["provided_sources"]]
        local = local_parts(row["question"], sources, count)
        reported = row["generation"]["usage"]["input_tokens"]
        known = (
            local["instructions"]
            + local["developer_instruction"]
            + local["schema"]
            + local["message_framing"]
            + local["question"]
            + local["context"]
            + default_injected
        )
        answers.append(
            {
                "id": row["id"],
                "reported_input": reported,
                "instructions_and_schema": local["instructions"]
                + local["developer_instruction"]
                + local["schema"],
                "question": local["question"],
                "retrieved_context": local["context"] + local["message_framing"],
                "codex_added_input": default_injected,
                "tool_definitions_and_other_framing": reported - known,
            }
        )
    remainder = sorted(a["tool_definitions_and_other_framing"] for a in answers)
    return {
        "schema_version": 1,
        "model": MODEL,
        "tokenizer": "o200k_base (tiktoken)",
        "question": record["question"]["id"],
        "started_at": record["started_at"],
        "calls": {
            "planned": len(record["calls"]),
            "recorded": len(calls),
            "single_model_request": sum(single),
            "turn_items": sorted({json.dumps(c["usage"]["turn_items"]) for c in calls}),
        },
        "input_tokens_by_condition": conditions,
        "context_regression_isolated": {
            **regression,
            "x": "local o200k_base tokens of the supplied passages",
            "y": "reported input tokens",
        },
        "instructions": {
            "local_tokens": parts["instructions"],
            "measured_by_doubling": round(measured_instructions, 1),
        },
        "isolated_fixed_framing": {
            "intercept": regression["intercept"],
            "our_instructions_schema_framing_question": ours,
            "codex_added_input_rendered": isolated_injected,
            "tool_definitions_and_other_framing": round(
                regression["intercept"] - ours - isolated_injected, 1
            ),
        },
        "codex_added_input": {
            "v1.1 adapter": default_sections,
            "isolated": isolated_sections,
        },
        "empty_evidence_contrast": {
            "v1.1 adapter": empty_default,
            "isolated": empty_isolated,
        },
        "v11_answers": {
            "per_answer": answers,
            "tool_definitions_and_other_framing": {
                "min": remainder[0],
                "median": statistics.median(remainder),
                "max": remainder[-1],
            },
            "note": "Codex-added input is rendered with today's local configuration; "
            "the v1.1 calls ran on 2026-11-16 with the same adapter settings.",
        },
    }


def collect(output, history, count, provider_factory):
    row = next(a for a in history["answers"] if a["id"] == QUESTION_ID)
    passages = [Chunk(**source) for source in row["provided_sources"]]
    record = {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "model": MODEL,
        "billing": "ChatGPT subscription quota; no API key",
        "retry_policy": "One attempt per call; no retries",
        "question": {
            "id": row["id"],
            "question": row["question"],
            "provided_sources": row["provided_sources"],
        },
        "injected": {
            "v1.1 adapter": injected_sections(False, count),
            "isolated": injected_sections(True, count),
        },
        "calls": [{**c, "status": "not_started"} for c in plan(passages)],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(record, stream, indent=2)
    providers = {flag: provider_factory(flag) for flag in (False, True)}
    for call in record["calls"]:
        instructions, message = request_text(call, row["question"], passages)
        call["local"] = {"instructions": count(instructions), "message": count(message)}
        started = time.perf_counter()
        try:
            text, usage = providers[call["isolated"]].generate(instructions, message, ANSWER_SCHEMA)
            call.update(status="recorded", usage=usage, response=text)
        except Exception as error:
            call.update(status="error", error_type=type(error).__name__)
        call["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        output.write_text(json.dumps(record, indent=2) + "\n")
        tokens = call.get("usage", {}).get("input_tokens")
        print(f"{call['name']:20} isolated={call['isolated']!s:5} {tokens}", flush=True)
    record["status"] = (
        "completed" if all(c["status"] == "recorded" for c in record["calls"]) else "partial"
    )
    record["finished_at"] = datetime.now(UTC).isoformat()
    output.write_text(json.dumps(record, indent=2) + "\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--v11-record", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--summarize-only", action="store_true")
    args = parser.parse_args()
    history = json.loads(args.v11_record.read_text())
    tokenizer = encoder()

    def count(text):
        return len(tokenizer.encode(text))

    if args.summarize_only:
        record = json.loads(args.output.read_text())
    else:
        from ledgerops_assistant.providers.subscription import SubscriptionProvider

        record = collect(
            args.output,
            history,
            count,
            lambda isolated: SubscriptionProvider(model=MODEL, isolated=isolated),
        )
    summary = summarize(record, history, count)
    if args.summary:
        args.summary.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: summary[k] for k in list(summary)[5:10]}, indent=2))


if __name__ == "__main__":
    main()
