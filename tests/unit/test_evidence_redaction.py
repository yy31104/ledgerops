"""Portable evidence retains numerical/hash facts and safe failure behavior."""

import json
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ops.evidence_redact import RedactionError, redact_file, redact_text


def test_longest_path_rule_handles_plain_uri_and_json_spelling_without_sibling_rewrite():
    prefixes = [("/Users/Ada", "$USER_HOME"), ("/Users/Ada/work/repo", "$REPO_ROOT")]
    original = (
        "rootdir: /Users/Ada/work/repo\nfile:///Users/Ada/work/repo/src/code.py:23\n"
        'sibling: /Users/Ada/work/repository\n{"source":"/Users/Ada/notes"}'
    )
    result = redact_text(original, prefixes)
    assert result == (
        "rootdir: $REPO_ROOT\nfile://$REPO_ROOT/src/code.py:23\n"
        'sibling: $USER_HOME/work/repository\n{"source":"$USER_HOME/notes"}'
    )


def test_encoded_path_and_hostname_are_replaced_without_overlapping_hosts():
    assert (
        redact_text(
            "file:///home/Ada%20Lovelace/project/x local-host.local other.local-host.local",
            [("/home/Ada Lovelace/project", "$REPO_ROOT")],
            ["local-host.local"],
        )
        == "file://$REPO_ROOT/x redacted-host other.local-host.local"
    )


def test_xml_escaped_path_is_redacted_without_changing_other_attributes():
    result = redact_text(
        '<testcase file="/home/A&amp;B/project/test.py" time="0.5"/>',
        [("/home/A&B/project", "$REPO_ROOT")],
    )
    assert ET.fromstring(result).attrib == {"file": "$REPO_ROOT/test.py", "time": "0.5"}


@pytest.mark.parametrize(
    "prefix,placeholder", [("1684", "$COUNT"), ("/", "$ROOT"), ("/home/alice", "unmarked")]
)
def test_non_path_or_unmarked_rules_cannot_silently_replace_evidence_facts(prefix, placeholder):
    with pytest.raises(RedactionError):
        redact_text("1684", [(prefix, placeholder)])


@pytest.mark.parametrize("suffix", ["json", "xml"])
def test_structured_evidence_preserves_counts_times_and_digest_and_keeps_source(tmp_path, suffix):
    digest = "d" * 64
    raw = (
        json.dumps(
            {"path": "/home/alice/repo/src", "passed": 1684, "time": 445.97, "sha256": digest}
        )
        if suffix == "json"
        else f'<testsuite tests="1684" time="445.97" hostname="alice.local">'
        f'<testcase file="/home/alice/repo/test.py"/><property value="{digest}"/></testsuite>'
    ).encode()
    source, output = tmp_path / f"original.{suffix}", tmp_path / f"copy.{suffix}"
    source.write_bytes(raw)
    report = redact_file(
        source, output, prefixes=[("/home/alice/repo", "$REPO_ROOT")], hostnames=["alice.local"]
    )
    assert source.read_bytes() == raw and report["changed"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    if suffix == "json":
        assert json.loads(output.read_bytes()) == {
            "path": "$REPO_ROOT/src",
            "passed": 1684,
            "time": 445.97,
            "sha256": digest,
        }
    else:
        root = ET.fromstring(output.read_bytes())
        assert root.attrib == {"tests": "1684", "time": "445.97", "hostname": "redacted-host"}
        assert root.find("testcase").get("file") == "$REPO_ROOT/test.py"
        assert root.find("property").get("value") == digest


@pytest.mark.parametrize("kind", ["same", "existing", "symlink"])
def test_publication_never_overwrites_source_existing_file_or_symlink_target(tmp_path, kind):
    source, destination = tmp_path / "raw.log", tmp_path / "output.log"
    source.write_text("source fact")
    other = tmp_path / "other.log"
    other.write_text("existing fact")
    if kind == "same":
        destination = source
    elif kind == "existing":
        destination.write_text("existing output")
    else:
        destination.symlink_to(other)
    before = set(tmp_path.iterdir())
    with pytest.raises(FileExistsError):
        redact_file(source, destination)
    assert source.read_text() == "source fact" and other.read_text() == "existing fact"
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    "raw,suffix", [(b"\0binary", ".log"), (b"{bad", ".json"), (b"<bad", ".xml"), (b"\xff", ".log")]
)
def test_bad_evidence_does_not_publish_partial_or_repaired_data(tmp_path, raw, suffix):
    source, output = tmp_path / ("source" + suffix), tmp_path / ("copy" + suffix)
    source.write_bytes(raw)
    with pytest.raises(RedactionError):
        redact_file(source, output)
    assert not output.exists() and source.read_bytes() == raw


def test_cli_failure_does_not_echo_private_input_path(tmp_path):
    private = tmp_path / "private-machine-name"
    private.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "ops/evidence_redact.py"),
            str(private / "absent.log"),
            str(tmp_path / "copy.log"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"error": "EVIDENCE_REDACTION_FAILED"}
    assert result.stderr == "" and str(private) not in result.stdout
