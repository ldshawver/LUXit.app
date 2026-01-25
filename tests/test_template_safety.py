from pathlib import Path
import re


def _load_templates_text():
    templates_dir = Path(__file__).resolve().parents[1] / "templates"
    return "\n".join(path.read_text(encoding="utf-8") for path in templates_dir.rglob("*.html"))


def _load_jinja_tags():
    template_text = _load_templates_text()
    return "\n".join(re.findall(r"{[{%].*?[}%]}", template_text, re.DOTALL))


def test_templates_do_not_call_get_default_company():
    template_text = _load_templates_text()
    assert "get_default_company" not in template_text


def test_templates_do_not_call_model_methods_or_query():
    template_text = _load_jinja_tags()
    assert re.search(r"current_user\.[a-zA-Z_]+\s*\(", template_text) is None
    assert re.search(r"\.query\b", template_text) is None
