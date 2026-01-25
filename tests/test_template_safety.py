from pathlib import Path


def test_templates_do_not_call_get_default_company():
    templates_dir = Path(__file__).resolve().parents[1] / "templates"
    template_text = "\n".join(path.read_text(encoding="utf-8") for path in templates_dir.rglob("*.html"))
    assert "get_default_company" not in template_text
