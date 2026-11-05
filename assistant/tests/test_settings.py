import pytest

from ledgerops_assistant.settings import Settings
from ledgerops_assistant.tools import ReadOnlyAPI


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:8090",
        "https://127.2.3.4:8090/",
        "https://localhost",
        "http://LOCALHOST:8090",
        "http://[::1]:8090",
        "https://[0:0:0:0:0:0:0:1]/",
    ],
)
def test_demo_token_is_allowed_only_for_loopback_api_origins(origin):
    assert Settings(api_origin=origin, demo_token="synthetic").demo_token == "synthetic"
    assert ReadOnlyAPI(origin, "lo1_" + "x" * 43).base_url == origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://api.example.invalid",
        "http://0.0.0.0:8090",
        "http://[::]:8090",
        "http://127.2.3.4:8090",
        "http://[0:0:0:0:0:0:0:1]:8090",
        "http://localhost.example.invalid:8090",
        "http://127.0.0.1.example.invalid:8090",
        "http://localhost@api.example.invalid",
        "http://user@localhost:8090",
        "http://localhost:bad-port",
        "http://localhost:70000",
        "http://localhost:0",
        "http://[::1",
        "http://[::1%25interface]:8090",
        "http://localhost/v1",
        "http://localhost?target=remote",
        "http://localhost#fragment",
        "file://localhost/",
        "localhost:8090",
        "",
    ],
)
def test_demo_token_rejects_remote_and_malformed_origins(origin):
    with pytest.raises(ValueError, match="demo token requires a loopback"):
        Settings(api_origin=origin, demo_token="synthetic")


def test_environment_cannot_enable_a_remote_demo_token(monkeypatch):
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_API_ORIGIN", "https://api.example.invalid")
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_DEMO_TOKEN", "synthetic")
    with pytest.raises(ValueError, match="demo token requires a loopback"):
        Settings.environment()
    monkeypatch.delenv("LEDGEROPS_ASSISTANT_DEMO_TOKEN")
    assert Settings.environment().api_origin == "https://api.example.invalid"
