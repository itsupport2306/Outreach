import os
import sys
from pathlib import Path
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides access to the values within
# the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Signal to application code that we're running under Alembic (avoid side effects)
os.environ["ALEMBIC_RUNNING"] = "1"

# Ensure project root is on sys.path so `import app.*` works when running Alembic
# from the repository root on Windows.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Import your application's metadata.
# IMPORTANT: models live across app.models and app.main, so import both.
from app.database import Base  # noqa: E402
from app.database import SQLALCHEMY_DATABASE_URL  # noqa: E402

# Prefer the normalized application URL so Render's postgresql:// URL uses
# psycopg 3 for both the web service and Alembic commands.
if (SQLALCHEMY_DATABASE_URL or "").strip():
    config.set_main_option("sqlalchemy.url", SQLALCHEMY_DATABASE_URL.replace("%", "%%"))
import app.models  # noqa: F401,E402
import app.main  # noqa: F401,E402

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    if not section.get("sqlalchemy.url"):
        section["sqlalchemy.url"] = config.get_main_option("sqlalchemy.url")
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
