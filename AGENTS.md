# Ocenka 360 Backend

- This repository is Python/Django only. Mobile and Next.js code live separately.
- Follow `docs/PROJECT_CONTEXT.md` and the current Part scope. Do not invent business tariffs, legal details or integration credentials.
- The owner prefers Russian explanations and ZIP handoffs. Do not push or deploy unless requested.
- Django 5.2 LTS; Python 3.12; dependencies pinned in requirements. Read the matching official documentation before changing version-dependent behavior.
- Custom user model already exists in the initial migration. Once applied, add new migrations; do not rewrite history.
- Client and appraiser roles never grant Django Admin access. Only superusers may manage accounts in Django Admin.
- Public API is opt-in (`AllowAny`); default is authenticated. Every future order/document endpoint also needs ownership/object-level checks.
- Do not put client data, .env, real keys, databases or uploaded documents in Git/ZIP. Do not serve private_media as static files.
- Required checks: `ruff check .`, `ruff format --check .`, `python manage.py check --settings=config.settings.test`, `python manage.py makemigrations --check --dry-run --settings=config.settings.test`, `python manage.py test --settings=config.settings.test`, `python manage.py spectacular --settings=config.settings.test --file docs/openapi.yaml --validate --fail-on-warn`.
- Test role boundaries, authentication and transactional behavior when changing them. Keep API docs and README accurate; distinguish implemented features from planned ones.
