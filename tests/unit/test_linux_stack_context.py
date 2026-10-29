"""A clean image's source authority is the actual immutable Git commit."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "ops/linux/stack.py").is_file()
)
spec = importlib.util.spec_from_file_location("context_stack", ROOT / "ops/linux/stack.py")
stack_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stack_module)


def repository(path):
    files = {
        "pyproject.toml": "[project]\nname='fixture'\nversion='0.0.0'\n",
        "uv.lock": "version=1\n",
        "README.md": "Committed fixture\n",
        "src/ledgerops/__init__.py": "committed_source = True\n",
        "provider_sim/__init__.py": "committed_provider = True\n",
    }
    for name in (
        "launcher.py",
        "bootstrap.py",
        "Dockerfile",
        "Caddyfile",
        "requirements-build.txt",
        "requirements-runtime.txt",
    ):
        files["ops/linux/" + name] = "committed operations fixture\n"
    for name, raw in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(raw)
    for arguments in (
        ("init", "--quiet", "-b", "main"),
        ("add", "."),
        (
            "-c",
            "user.name=Local fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Committed build fixture",
        ),
    ):
        subprocess.run(["git", "-C", str(path), *arguments], check=True, capture_output=True)


def test_clean_context_uses_commit_even_when_worktree_changes_during_packaging(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    repository(source)
    monkeypatch.setattr(stack_module, "HERE", source / "ops/linux")
    original = stack_module.run

    def run(arguments, **kwargs):
        result = original(arguments, **kwargs)
        if "status" in arguments:
            (source / "src/ledgerops/__init__.py").write_text("changed_during_build = True\n")
            (source / "ops/linux/launcher.py").write_text("changed_operations = True\n")
        return result

    monkeypatch.setattr(stack_module, "run", run)
    context = tmp_path / "context"
    provenance, _ = stack_module.prepare_context(source, context)
    assert not provenance["dirty"]
    assert (context / "source/src/ledgerops/__init__.py").read_text() == "committed_source = True\n"
    assert (context / "scripts/launcher.py").read_text() == "committed operations fixture\n"
    assert set(json.loads((context / "source-provenance.json").read_bytes())) == {
        "schema_version",
        "revision",
        "dirty",
        "base_images",
    }


def test_dirty_source_or_uncommitted_external_ops_refuses_default_build(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    repository(source)
    monkeypatch.setattr(stack_module, "HERE", source / "ops/linux")
    (source / "src/ledgerops/__init__.py").write_text("uncommitted change\n")
    with pytest.raises(ValueError, match="CLEAN_COMMITTED_SOURCE_AND_OPS_REQUIRED"):
        stack_module.prepare_context(source, tmp_path / "forbidden-context")
    assert not (tmp_path / "forbidden-context").exists()
