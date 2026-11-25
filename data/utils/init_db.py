import data.utils.database as database

setup_database = database.setup_database


def init_db():
    setup_database()
    print("Database initialized successfully.")