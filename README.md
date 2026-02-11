# Control Horario SaaS Skeleton

Production-ready Flask + HTMX + PostgreSQL starter for multi-tenant employee time tracking with strict PostgreSQL Row Level Security (RLS).

Target deployment: Dokploy (`https://fichar.bluetime.cloud`)

## Stack

- Python 3.12
- Flask app factory
- Server-rendered Jinja + HTMX
- SQLAlchemy + Alembic
- PostgreSQL with RLS
- Flask-Login (session auth)
- Flask-WTF CSRF protection

## Required env vars

- `DATABASE_URL`
- `SECRET_KEY`
- `ENV` (`development` or `production`)
- `SESSION_COOKIE_NAME`
- `APP_URL`

## Local development

1. Create and activate a virtualenv.
2. Install dependencies.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

3. Export env vars.

```bash
export DATABASE_URL="postgresql+psycopg://control_horario:control_horario@localhost:5432/control_horario"
export SECRET_KEY="change-me"
export ENV="development"
export SESSION_COOKIE_NAME="control_horario_session"
export APP_URL="http://localhost:5000"
```

4. Run migrations and enable RLS policies.

```bash
alembic upgrade head
psql "postgresql://control_horario:control_horario@localhost:5432/control_horario" -f scripts/rls.sql
```

5. Run the app locally.

```bash
export FLASK_APP=wsgi:app
flask run --debug
```

## Docker run

```bash
docker build -t control-horario .
docker run --rm -p 8000:8000 \
  -e DATABASE_URL="postgresql+psycopg://control_horario:control_horario@db:5432/control_horario" \
  -e SECRET_KEY="change-me" \
  -e ENV="production" \
  -e SESSION_COOKIE_NAME="control_horario_session" \
  -e APP_URL="https://fichar.bluetime.cloud" \
  control-horario
```

Gunicorn entrypoint:

```bash
gunicorn -c gunicorn.conf.py wsgi:app
```

## RLS and tenant isolation

- All tenant-scoped tables include `tenant_id`.
- Application sets request context with:
  - `SET LOCAL app.tenant_id = '<uuid>'`
  - `SET LOCAL app.actor_user_id = '<uuid>'` (optional)
- RLS policies live in `scripts/rls.sql`.
- `memberships` includes an additional SELECT policy by `app.actor_user_id` so users can reach `/select-tenant` before choosing an active tenant.

Important:

- Do not run the app with a PostgreSQL superuser.
- Do not grant `BYPASSRLS` to the application role.
- `FORCE ROW LEVEL SECURITY` is enabled on tenant tables.

## Tests

Run unit tests:

```bash
pytest
```

Run integration RLS test (requires dedicated PostgreSQL test DB and non-superuser role):

```bash
export TEST_DATABASE_URL="postgresql+psycopg://app_user:password@localhost:5432/control_horario_test"
pytest -m integration
```
