from pathlib import Path


def test_gunicorn_bind_is_5000_only():
    repo_root = Path(__file__).resolve().parents[1]
    config_text = (repo_root / "gunicorn.conf.py").read_text(encoding="utf-8")

    assert "8000" not in config_text
    assert 'bind = "127.0.0.1:5000"' in config_text
