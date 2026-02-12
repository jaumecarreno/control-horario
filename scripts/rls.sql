-- Enable and enforce tenant isolation for tenant-scoped tables.
-- Application code must set:
--   SET LOCAL app.tenant_id = '<tenant-uuid>';
-- and optionally:
--   SET LOCAL app.actor_user_id = '<user-uuid>';

ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE memberships FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS memberships_tenant_isolation ON memberships;
CREATE POLICY memberships_tenant_isolation ON memberships
USING (tenant_id = current_setting('app.tenant_id')::uuid)
WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
DROP POLICY IF EXISTS memberships_actor_select ON memberships;
CREATE POLICY memberships_actor_select ON memberships
FOR SELECT
USING (user_id = current_setting('app.actor_user_id', true)::uuid);

ALTER TABLE sites ENABLE ROW LEVEL SECURITY;
ALTER TABLE sites FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sites_tenant_isolation ON sites;
CREATE POLICY sites_tenant_isolation ON sites
USING (tenant_id = current_setting('app.tenant_id')::uuid)
WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE employees ENABLE ROW LEVEL SECURITY;
ALTER TABLE employees FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS employees_tenant_isolation ON employees;
CREATE POLICY employees_tenant_isolation ON employees
USING (tenant_id = current_setting('app.tenant_id')::uuid)
WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE time_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE time_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS time_events_tenant_isolation ON time_events;
CREATE POLICY time_events_tenant_isolation ON time_events
USING (tenant_id = current_setting('app.tenant_id')::uuid)
WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE time_adjustments ENABLE ROW LEVEL SECURITY;
ALTER TABLE time_adjustments FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS time_adjustments_tenant_isolation ON time_adjustments;
CREATE POLICY time_adjustments_tenant_isolation ON time_adjustments
USING (tenant_id = current_setting('app.tenant_id')::uuid)
WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE leave_types ENABLE ROW LEVEL SECURITY;
ALTER TABLE leave_types FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS leave_types_tenant_isolation ON leave_types;
CREATE POLICY leave_types_tenant_isolation ON leave_types
USING (tenant_id = current_setting('app.tenant_id')::uuid)
WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE leave_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE leave_requests FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS leave_requests_tenant_isolation ON leave_requests;
CREATE POLICY leave_requests_tenant_isolation ON leave_requests
USING (tenant_id = current_setting('app.tenant_id')::uuid)
WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE shifts ENABLE ROW LEVEL SECURITY;
ALTER TABLE shifts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS shifts_tenant_isolation ON shifts;
CREATE POLICY shifts_tenant_isolation ON shifts
USING (tenant_id = current_setting('app.tenant_id')::uuid)
WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS audit_log_tenant_isolation ON audit_log;
CREATE POLICY audit_log_tenant_isolation ON audit_log
USING (tenant_id = current_setting('app.tenant_id')::uuid)
WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
