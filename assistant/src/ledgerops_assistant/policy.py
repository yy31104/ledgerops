"""The measured release default; a service can explicitly select the other retriever."""

import json
from importlib.resources import files

from ledgerops_assistant.support import SUPPORT_MODES

METHODS = {"vector", "hybrid"}


def _policy():
    return json.loads(files("ledgerops_assistant").joinpath("retrieval_policy.json").read_text())


def default_retrieval():
    method = _policy()["method"]
    if method not in METHODS:
        raise ValueError("invalid release retrieval policy")
    return method


def scope_threshold():
    value = _policy()["scope_threshold"]
    if type(value) not in (float, int) or not -1 <= value <= 1:
        raise ValueError("invalid release scope threshold")
    return float(value)


def coverage_threshold():
    value = _policy()["coverage_threshold"]
    if type(value) is not float or not 0 < value <= 1:
        raise ValueError("invalid release coverage threshold")
    return value


def support_threshold():
    value = _policy().get("support_threshold")
    if value is not None and (type(value) not in (float, int) or not 0 <= value <= 1):
        raise ValueError("invalid release passage support threshold")
    return float(value) if value is not None else None


def support_mode():
    mode = _policy().get("support_mode", "single-span")
    if mode not in SUPPORT_MODES:
        raise ValueError("invalid release passage support mode")
    return mode
