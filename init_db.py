from app.database import init_db, SQLALCHEMY_DATABASE_URL

if __name__ == "__main__":
    print("Using DB URL:", SQLALCHEMY_DATABASE_URL)
    init_db()