# Contributing to django-icv-search

Practical guide for contributors.

---

## Prerequisites

- Python 3.11 or later
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Django 5.1 or later (installed as part of the dev setup)
- PostgreSQL 14 or later (required by some tests — see below)

---

## Local Development Setup

```bash
git clone https://github.com/icvoss/django-icv-search.git
cd django-icv-search

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Install in editable mode with test dependencies
pip install -e ".[dev]"
pip install "Django~=5.1" pytest pytest-django pytest-cov pytest-mock factory-boy djangorestframework django-filter
pip install psycopg2-binary "psycopg[binary]" packaging
```

---

## Running Tests

Tests live in `tests/`. Run them with pytest, setting `DJANGO_SETTINGS_MODULE` and `PYTHONPATH`.

### SQLite (most tests)

```bash
DJANGO_SETTINGS_MODULE=settings \
PYTHONPATH=src:tests \
pytest tests/ -v --tb=short
```

### PostgreSQL (postgres backend tests)

Some tests require a live PostgreSQL instance. Provide the DB connection via `DB_*` environment variables:

```bash
DJANGO_SETTINGS_MODULE=settings \
PYTHONPATH=src:tests \
DB_NAME=icv_test_db DB_USER=icv_test DB_PASSWORD=icv_test_password \
DB_HOST=localhost DB_PORT=5432 \
pytest tests/ -v --tb=short
```

If no `DB_*` variables are set the test settings fall back to SQLite and the PostgreSQL-specific tests are skipped automatically.

---

## Code Standards

All Python code is linted and formatted with [ruff](https://docs.astral.sh/ruff/), configured in `pyproject.toml`.

| Setting | Value |
|---------|-------|
| Line length | 120 |
| Quote style | Double |
| Target Python | 3.11 |

```bash
# Check (no writes — what CI runs)
ruff check .
ruff format --check .

# Fix in place
ruff check --fix .
ruff format .
```

CI will fail if either check reports errors. Run both before pushing.

---

## Project Structure

This is a single-package repository following the src layout:

```
django-icv-search/
    src/icv_search/         # importable package
    tests/
        settings.py         # Django settings for the test suite
    pyproject.toml          # package metadata, dependencies, tool config
    CHANGELOG.md
    README.md
    RELEASING.md
```

---

## Git Workflow

### Commits

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <description>
```

| Type | When to use |
|------|-------------|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `chore` | Maintenance, version bumps, dependency updates |
| `docs` | Documentation only |
| `test` | Adding or updating tests |
| `style` | Formatting, whitespace — no logic change |
| `refactor` | Code change that is neither a fix nor a feature |

### Branches and PRs

Push feature branches and open a pull request against `main`. CI must pass before merging. Prefer small, focused commits over large ones.

---

## Releasing

See [RELEASING.md](RELEASING.md) for the full release process.

The short version: bump the version in `pyproject.toml` and `src/icv_search/__init__.py`, add a CHANGELOG entry, open a PR, merge to `main`, then push a `v<version>` tag. The tag triggers CI to publish to PyPI automatically.
