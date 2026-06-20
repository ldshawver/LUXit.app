import subprocess
import sys
from pathlib import Path

SCRIPT = Path("scripts/documenso/verify_documenso_signing_config.py")


def run_checker(root: Path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--skip-network"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_checker_passes_without_documenso_config(tmp_path):
    (tmp_path / "template.html").write_text("<a href='https://documenso.example.com/sign/abc'>Sign</a>")

    result = run_checker(tmp_path)

    assert result.returncode == 0
    assert "PASS" in result.stdout


def test_checker_blocks_known_broken_mypaylink_deep_link(tmp_path):
    (tmp_path / "template.html").write_text(
        "https://app.mypaylink.app/app/contractor-hub/contracts/735551c2-ec6c-41e6-976d-1eef4e13bfa5/sign"
    )

    result = run_checker(tmp_path)

    assert result.returncode == 1
    assert "known-broken" in result.stderr


def test_checker_requires_https_public_documenso_url(tmp_path):
    (tmp_path / "compose.yml").write_text("APP_URL=http://documenso.example.com\n")

    result = run_checker(tmp_path)

    assert result.returncode == 1
    assert "must use https" in result.stderr
