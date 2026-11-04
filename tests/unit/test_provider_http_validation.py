"""Pure request/parser checks; real transport/store behavior lives in tests/provider."""

import pytest

from provider_sim.server import FaultControls, RequestError, decode_body, require_token


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b"[]", b"\xff"])
def test_strict_provider_json(raw):
    with pytest.raises(RequestError):
        decode_body(raw)


def test_provider_json_nested_duplicate_is_rejected():
    with pytest.raises(RequestError):
        decode_body(b'{"x":{"a":1,"a":2}}')


def test_provider_json_object_is_preserved():
    assert decode_body(b'{"principal_minor":"60","fee_minor":"2"}') == {
        "principal_minor": "60",
        "fee_minor": "2",
    }


@pytest.mark.parametrize("token", [None, "short", "a" * 31 + "\n", "a" * 513, "a" * 32 + "\u754c"])
def test_provider_token_rejects_ambiguous_credentials(token):
    with pytest.raises(ValueError):
        require_token(token)


def test_control_fault_only_applies_to_named_key_once():
    controls = FaultControls()
    controls.arm({"provider_key": "target", "point": "before_commit", "action": "pause"})
    controls.hook("other")("before_commit")
    controls.hook("target")("after_commit")
    assert controls.state()["gate"]["reached"] is False
    with pytest.raises(RequestError) as error:
        controls.arm({"provider_key": "next", "point": "before_commit", "action": "pause"})
    assert error.value.status == 409
    controls.release()
    controls.hook("target")("before_commit")
    assert controls.state()["gate"]["reached"] is False


def test_configurations_are_key_scoped():
    controls = FaultControls()
    controls.configure({"provider_key": "one", "lookup_lag": True, "put_outcome": "ACCEPTED"})
    assert controls.settings("two") == {}
    result = controls.settings("one")
    result["lookup_lag"] = False
    assert controls.settings("one")["lookup_lag"] is True
