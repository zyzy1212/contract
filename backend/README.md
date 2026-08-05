# Backend development

Install the locked dependency set with `uv sync --locked --extra dev` from this directory.
Update `uv.lock` with `uv lock` only when intentionally changing `pyproject.toml` dependencies.

Run the mandatory PostgreSQL security suite in CI with a disposable database:

```powershell
$env:TEST_DATABASE_URL = "postgresql+asyncpg://contract:contract@localhost:5432/contract_review_test"
$env:TEST_DATABASE_DISPOSABLE = "1"
uv run --locked --extra dev pytest -m postgresql -v
```

The suite refuses destructive extension-ownership checks unless the explicit
disposable marker is set and the configured database name ends in `_test`.
It also verifies negative public/customer and cross-tenant writes plus
transaction-local authorization reset on a reused pooled connection.
