"""Deleting data requires explicit labels even without any surviving container."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "ops/linux/stack.py").is_file()
)
spec = importlib.util.spec_from_file_location("linux_stack", ROOT / "ops/linux/stack.py")
stack_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stack_module)


@pytest.mark.parametrize(
    "labels",
    [
        None,
        {},
        {"com.docker.compose.project": "ledgerops-other"},
        {"com.docker.compose.project": "ledgerops-owned"},
    ],
)
def test_unlabelled_or_foreign_named_volume_prevents_any_delete(tmp_path, monkeypatch, labels):
    calls = []
    state = tmp_path / "stack.json"
    state.write_text(json.dumps({"project": "ledgerops-owned", "environment": {}}))

    def run(arguments, **kwargs):
        calls.append(arguments)
        if arguments[1:3] == ["volume", "ls"]:
            return SimpleNamespace(stdout="ledgerops-owned_postgres-data\n")
        if arguments[1:3] == ["volume", "inspect"]:
            return SimpleNamespace(stdout=json.dumps([{"Labels": labels}]))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(stack_module, "run", run)
    stack = stack_module.Stack("ledgerops-owned", state)
    with pytest.raises(ValueError, match="UNEXPECTED_VOLUME_SCOPE"):
        stack.dispose()
    assert not any("compose" in call or "rm" in call or "down" in call for call in calls)


def test_foreign_container_scope_prevents_down_without_touching_volumes(tmp_path, monkeypatch):
    calls = []
    state = tmp_path / "stack.json"
    state.write_text(json.dumps({"project": "ledgerops-owned", "environment": {}}))

    def run(arguments, **kwargs):
        calls.append(arguments)
        if arguments[1] == "ps":
            return SimpleNamespace(stdout="foreign-container-id\n")
        if arguments[1] == "inspect":
            return SimpleNamespace(
                stdout=json.dumps(
                    [
                        {
                            "Config": {
                                "Labels": {
                                    "com.docker.compose.project": "ledgerops-owned",
                                    "io.ledgerops.scope": "other-owner",
                                }
                            }
                        }
                    ]
                )
            )
        raise AssertionError("No later inspection or deletion may run")

    monkeypatch.setattr(stack_module, "run", run)
    stack = stack_module.Stack("ledgerops-owned", state)
    with pytest.raises(ValueError, match="UNEXPECTED_CONTAINER_SCOPE"):
        stack.down()
    assert not any("compose" in call or "down" in call for call in calls)
