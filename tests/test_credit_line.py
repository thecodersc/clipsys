"""The program's credit line is appended to the posted description."""

from types import SimpleNamespace

from clipsys.cli import _with_credit


def test_credit_line_appended():
    prog = SimpleNamespace(credit_line="Clip from @creator")
    assert _with_credit("Great moment.", prog) == "Great moment.\n\nClip from @creator"
    assert _with_credit("", prog) == "Clip from @creator"
    assert _with_credit("Great moment.", SimpleNamespace(credit_line="")) == "Great moment."
    assert _with_credit("Great moment.", None) == "Great moment."
