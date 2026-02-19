"""Helpers to query visible time events.

Visible events exclude original events that have been superseded by
an approved punch correction replacement.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.sql import Select

from app.models import Employee, TimeEvent, TimeEventSupersession


def _not_superseded_condition():
    superseded_subquery = select(TimeEventSupersession.id).where(
        TimeEventSupersession.original_event_id == TimeEvent.id
    )
    return ~superseded_subquery.exists()


def visible_time_events_stmt() -> Select:
    return select(TimeEvent).where(_not_superseded_condition())


def visible_employee_events_between_stmt(
    employee_id: uuid.UUID,
    start: datetime,
    end: datetime,
) -> Select:
    return (
        visible_time_events_stmt()
        .where(TimeEvent.employee_id == employee_id, TimeEvent.ts >= start, TimeEvent.ts <= end)
        .order_by(TimeEvent.ts.asc())
    )


def visible_employee_recent_events_stmt(employee_id: uuid.UUID, limit: int) -> Select:
    return (
        visible_time_events_stmt()
        .where(TimeEvent.employee_id == employee_id)
        .order_by(TimeEvent.ts.desc())
        .limit(limit)
    )


def visible_events_with_employee_between_stmt(start: datetime, end: datetime) -> Select:
    return (
        select(TimeEvent, Employee)
        .join(Employee, Employee.id == TimeEvent.employee_id)
        .where(TimeEvent.ts >= start, TimeEvent.ts <= end, _not_superseded_condition())
        .order_by(TimeEvent.ts.asc())
    )
