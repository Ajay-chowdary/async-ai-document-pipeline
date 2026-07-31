"""Scaffold smoke test: the package imports cleanly and has no import cycles."""

import importlib

import pytest

import app

CORE_MODULES = [
    "app.core.config",
    "app.core.enums",
    "app.core.exceptions",
    "app.core.logging",
]


def test_version_exposed() -> None:
    assert app.__version__ == "0.1.0"


@pytest.mark.parametrize("module_name", CORE_MODULES)
def test_module_imports(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None


def test_enums_do_not_import_config() -> None:
    """``core.enums`` is the shared leaf that keeps db and schemas decoupled.

    If it ever grows a dependency on configuration, the import graph stops
    being a DAG the moment models start importing it.
    """
    module = importlib.import_module("app.core.enums")
    assert not hasattr(module, "Settings")
    assert not hasattr(module, "get_settings")
