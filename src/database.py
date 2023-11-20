import sqlite3

# Database file path
DB_FILE = "osintbeast.db"

def connect_db():
    """Create a database connection."""
    try:
        return sqlite3.connect(DB_FILE)
    except sqlite3.Error as e:
        print(f"Error connecting to database: {e}")
    return None

def setup_database():
    """Set up the database schema."""
    conn = connect_db()
    if conn is not None:
        try:
            cursor = conn.cursor()
            # Add your table creation SQL here
            cursor.execute('''CREATE TABLE IF NOT EXISTS example_table (
                              id INTEGER PRIMARY KEY,
                              column1 TEXT,
                              column2 TEXT
                              );''')
            conn.commit()
        except sqlite3.Error as e:
            print(f"Error setting up database: {e}")
        finally:
            conn.close()
    else:
        print("Error! Cannot create the database connection.")

def insert_data(table, data):
    """Insert data into the database."""
    conn = connect_db()
    if conn is not None:
        try:
            cursor = conn.cursor()
            cursor.execute(f"INSERT INTO {table} (column1, column2) VALUES (?, ?)", (data['column1'], data['column2']))
            conn.commit()
        except sqlite3.Error as e:
            print(f"Error inserting data: {e}")
        finally:
            conn.close()
    else:
        print("Error! Cannot create the database connection.")

def query_database(query):
    """Query the database."""
    conn = connect_db()
    results = []
    if conn is not None:
        try:
            cursor = conn.cursor()
            cursor.execute(query)
            results = cursor.fetchall()
        except sqlite3.Error as e:
            print(f"Error querying database: {e}")
        finally:
            conn.close()
    else:
        print("Error! Cannot create the database connection.")
    return results
