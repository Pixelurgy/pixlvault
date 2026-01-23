# PixlVault

A REST API server for PixlVault

## Development

- Install dependencies: `pip install -e .`
- Run server: `python -m pixlvault.server`

## Database Migrations (Alembic)

PixlVault uses Alembic for schema changes. The server runs migrations on startup.

- Set the database URL with `PIXLVAULT_DB_URL` (defaults to `sqlite:///vault.db`).
- Create a new migration after model changes:
	- `python -m alembic revision --autogenerate -m "describe change"`
- Apply migrations manually if needed:
	- `python -m alembic upgrade head`

## Publishing

- Build: `python setup.py sdist bdist_wheel`
- Upload: `twine upload dist/*`
