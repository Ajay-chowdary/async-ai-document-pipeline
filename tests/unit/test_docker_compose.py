"""Compose file stays parseable; skipped when Docker is not installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKER = shutil.which("docker")


@pytest.mark.skipif(DOCKER is None, reason="docker not installed")
def test_compose_config_is_valid() -> None:
    assert DOCKER is not None
    result = subprocess.run(  # noqa: S603 — fixed argv, path from shutil.which
        [DOCKER, "compose", "config", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_dockerfiles_exist() -> None:
    assert (ROOT / "Dockerfile.api").is_file()
    assert (ROOT / "Dockerfile.worker").is_file()
    assert (ROOT / "docker-compose.yml").is_file()


def test_compose_declares_required_services() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for name in ("postgres:", "redis:", "migrate:", "api:", "worker:"):
        assert name in text
    assert "service_completed_successfully" in text
    assert "postgres_data:" in text
    assert "redis_data:" in text
    assert "uploads:" in text
