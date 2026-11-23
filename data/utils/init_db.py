import data.utils.database as database

setup_database = database.setup_database


def init_db():
    try:
        setup_database()
    except database.DATABASE_ERRORS as exc:
        print(f"Database initialization failed: {exc}")
        return
    except (OSError, UnicodeError) as exc:
        print(f"Database initialization failed: {exc}")
        return
    print("Database initialized successfully.")