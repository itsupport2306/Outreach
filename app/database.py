from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def normalize_database_url(database_url: str) -> str:
    """Normalize provider PostgreSQL URLs for SQLAlchemy + psycopg 3."""
    database_url = (database_url or "").strip()
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url[len("postgres://"):]
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://"):]
    return database_url


# Use SQLite only as the local-development fallback. Render sets DATABASE_URL
# to its PostgreSQL internal URL, which is normalized to the psycopg 3 driver.
SQLALCHEMY_DATABASE_URL = normalize_database_url(
    os.getenv("DATABASE_URL") or "sqlite:///./interview_scheduler.db"
)

DATABASE_BACKEND = make_url(SQLALCHEMY_DATABASE_URL).get_backend_name()

# Create SQLAlchemy engine
# Use SQLite-specific connect_args only when using SQLite
engine_kwargs = {}
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # Render Postgres may close idle connections during deploys/maintenance.
    engine_kwargs["pool_pre_ping"] = True

engine = create_engine(SQLALCHEMY_DATABASE_URL, **engine_kwargs)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()

def get_db():
    """
    Dependency to get DB session.
    Use this in FastAPI path operations with: db: Session = Depends(get_db)
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """
    Initialize the database by creating all tables.
    Call this function at application startup.
    """
    from . import models  # Import models to register them with SQLAlchemy
    auto_create = (os.getenv("DB_AUTO_CREATE_TABLES") or "").strip().lower() in {"1", "true", "yes", "y"}
    if auto_create:
        Base.metadata.create_all(bind=engine)
        print("Database tables created")
