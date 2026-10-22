import pytest

from simtune import util


def test_rich_tables_preserve_literal_markup_without_printing(capsys):
    pytest.importorskip("rich")
    text = "[red]literal[/red]"
    rendered = util.render_table([{"value": text}], ["value"])
    assert text in rendered
    assert capsys.readouterr().out == ""


def test_plain_tables_without_optional_dependencies(monkeypatch):
    monkeypatch.setattr(util, "optional_import", lambda name: None)
    assert util.render_table([{"a": 1}], ["a"]) == "a\n1"
