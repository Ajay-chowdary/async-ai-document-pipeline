"""Structural checks for the Terraform demo module (no AWS calls)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TF = ROOT / "terraform"


def test_required_terraform_files_exist() -> None:
    for name in (
        "versions.tf",
        "providers.tf",
        "variables.tf",
        "main.tf",
        "outputs.tf",
        "user_data.sh",
        "terraform.tfvars.example",
        "README.md",
        ".gitignore",
    ):
        assert (TF / name).is_file(), name


def test_cidr_variables_have_no_open_world_default() -> None:
    text = (TF / "variables.tf").read_text(encoding="utf-8")
    assert 'variable "ssh_ingress_cidr"' in text
    assert 'variable "http_ingress_cidr"' in text
    # Required explicitly: neither variable block may default to the world.
    assert 'default     = "0.0.0.0/0"' not in text
    assert 'default = "0.0.0.0/0"' not in text


def test_user_data_reads_openai_key_from_ssm() -> None:
    text = (TF / "user_data.sh").read_text(encoding="utf-8")
    assert "ssm get-parameter" in text
    assert "OPENAI_API_KEY" in text
    assert "--with-decryption" in text


def test_tfvars_example_does_not_embed_api_key_value() -> None:
    text = (TF / "terraform.tfvars.example").read_text(encoding="utf-8")
    assert "openai_ssm_parameter_name" in text
    assert "sk-" not in text.replace("sk-...", "")


def test_main_wires_iam_instance_profile_and_security_group() -> None:
    text = (TF / "main.tf").read_text(encoding="utf-8")
    assert "aws_instance" in text
    assert "aws_security_group" in text
    assert "aws_iam_role" in text
    assert "aws_iam_instance_profile" in text
    assert "AmazonSSMManagedInstanceCore" in text
