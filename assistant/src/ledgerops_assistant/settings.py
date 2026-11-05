"""Explicit local service configuration; no request can select an origin or secret."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from ledgerops_assistant.agent import Limits


@dataclass(repr=False)
class Settings:
    api_origin: str = "http://127.0.0.1:8090"
    database_url: str = field(default="", repr=False)
    providers: tuple[str, ...] = ("fake",)
    embedder: str = "bge"
    console_url: str = "http://127.0.0.1:8080"
    web_dist: Path = Path(__file__).resolve().parents[2] / "web" / "dist"
    demo_token: str = field(default="", repr=False)
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def environment(cls):
        providers = tuple(os.environ.get("LEDGEROPS_ASSISTANT_PROVIDERS", "fake").split(","))
        if not set(providers) <= {"fake", "openai_api", "ollama"}:
            raise ValueError(
                "HTTP service supports fake, openai_api or ollama; subscription is CLI only"
            )
        return cls(
            api_origin=os.environ.get("LEDGEROPS_ASSISTANT_API_ORIGIN", "http://127.0.0.1:8090"),
            database_url=os.environ.get("LEDGEROPS_ASSISTANT_DATABASE_URL", ""),
            web_dist=Path(os.environ.get("LEDGEROPS_ASSISTANT_WEB_DIST", str(cls.web_dist))),
            providers=providers,
            embedder=os.environ.get("LEDGEROPS_ASSISTANT_EMBEDDER", "bge"),
            console_url=os.environ.get("LEDGEROPS_ASSISTANT_CONSOLE_URL", "http://127.0.0.1:8080"),
            demo_token=os.environ.get("LEDGEROPS_ASSISTANT_DEMO_TOKEN", ""),
            limits=Limits(
                input_usd_per_million=float(os.environ.get("MODEL_INPUT_USD_PER_MILLION", "0")),
                output_usd_per_million=float(os.environ.get("MODEL_OUTPUT_USD_PER_MILLION", "0")),
                max_cost_usd=float(os.environ.get("MODEL_MAX_COST_USD", "0.10")),
            ),
        )
