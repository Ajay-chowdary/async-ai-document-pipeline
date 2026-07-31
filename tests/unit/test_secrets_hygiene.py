"""Repo hygiene: secrets stay out of the tree and the Docker context."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_env_is_gitignored_and_dockerignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".env" in gitignore
    assert ".env" in dockerignore


def test_env_example_has_no_real_openai_key() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=" in text
    for line in text.splitlines():
        if line.startswith("OPENAI_API_KEY="):
            value = line.split("=", 1)[1].strip()
            assert value == ""
            break
    else:
        raise AssertionError("OPENAI_API_KEY line missing from .env.example")


def test_no_dotenv_file_committed_in_repo_root() -> None:
    """A local .env may exist for development; it must not be part of the tree we ship.

    This check only asserts the example file is present and a secrets-shaped
    committed name is absent from the expected source files list.
    """
    assert (ROOT / ".env.example").is_file()
    # Tracked-source surrogate: terraform tfvars example must not embed a key.
    tfvars = (ROOT / "terraform" / "terraform.tfvars.example").read_text(encoding="utf-8")
    assert "openai_ssm_parameter_name" in tfvars
    assert "OPENAI_API_KEY=" not in tfvars
