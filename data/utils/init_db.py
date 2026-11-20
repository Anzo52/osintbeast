from data.utils.database import setup_database


def init_db():
    setup_database()
    print("Database initialized successfully.")