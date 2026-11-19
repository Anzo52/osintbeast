from data.utils.database import setup_database


import sqlite3


def init_db():
    try:
        setup_database()
    except (OSError, sqlite3.Error, UnicodeError) as exc:
        print(f"Database initialization failed: {exc}")
        return
    print("Database initialized successfully.")