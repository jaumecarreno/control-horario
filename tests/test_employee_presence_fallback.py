from __future__ import annotations

from app.blueprints.employee import _enum_value
from app.models import ShiftPeriod


def test_enum_value_accepts_enum_and_plain_string():
    assert _enum_value(ShiftPeriod.DAILY) == "DAILY"
    assert _enum_value("DAILY") == "DAILY"
    assert _enum_value(None, fallback="DAILY") == "DAILY"
