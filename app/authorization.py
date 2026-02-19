"""Authorization capabilities and semantic permission decorators."""

from __future__ import annotations

import functools
from typing import Callable

from flask import abort

from app.models import Membership, MembershipRole
from app.tenant import current_membership

ADMIN_ROLES = {MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.MANAGER}


def can_manage_users(role: MembershipRole) -> bool:
    return role in ADMIN_ROLES


def can_manage_employees(role: MembershipRole) -> bool:
    return role in ADMIN_ROLES


def can_manage_shifts(role: MembershipRole) -> bool:
    return role in ADMIN_ROLES


def can_approve_leaves(role: MembershipRole) -> bool:
    return role in ADMIN_ROLES


def can_export_payroll(role: MembershipRole) -> bool:
    return role in ADMIN_ROLES


def can_view_adjustments(role: MembershipRole) -> bool:
    return role in ADMIN_ROLES


def can_access_employee_self_service(membership: Membership) -> bool:
    return membership.employee_id is not None


def can_create_manual_punches(membership: Membership) -> bool:
    return can_access_employee_self_service(membership)


def _membership_role_predicate(check: Callable[[MembershipRole], bool]) -> Callable[[Membership], bool]:
    def predicate(membership: Membership) -> bool:
        return check(membership.role)

    return predicate


def permission_required(permission_name: str, check: Callable[[Membership], bool]):
    def decorator(view: Callable):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            membership = current_membership()
            if membership is None or not check(membership):
                abort(403, description=f"Insufficient permissions: {permission_name}.")
            return view(*args, **kwargs)

        return wrapped

    return decorator


manage_users_required = permission_required("manage_users", _membership_role_predicate(can_manage_users))
manage_employees_required = permission_required("manage_employees", _membership_role_predicate(can_manage_employees))
manage_shifts_required = permission_required("manage_shifts", _membership_role_predicate(can_manage_shifts))
approve_leaves_required = permission_required("approve_leaves", _membership_role_predicate(can_approve_leaves))
export_payroll_required = permission_required("export_payroll", _membership_role_predicate(can_export_payroll))
view_adjustments_required = permission_required("view_adjustments", _membership_role_predicate(can_view_adjustments))
employee_self_service_required = permission_required("employee_self_service", can_access_employee_self_service)
manual_punch_required = permission_required("manual_punch", can_create_manual_punches)
