from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Database URL - use environment variable or default to SQLite (for local dev)
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./interview_scheduler.db")

# Create SQLAlchemy engine
# Use SQLite-specific connect_args only when using SQLite
engine_kwargs = {}
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

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
