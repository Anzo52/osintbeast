import os
import sqlite3

DB_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "osintbeast.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS Platforms (
    platformID INTEGER PRIMARY KEY,
    platform TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS Names (
    nameID INTEGER PRIMARY KEY,
    firstName TEXT NOT NULL,
    middleName TEXT,
    lastName TEXT,
    other TEXT
);

CREATE TABLE IF NOT EXISTS Locations (
    locationID INTEGER PRIMARY KEY,
    country TEXT,
    usState TEXT,
    county TEXT,
    city TEXT,
    street TEXT,
    addressNum INTEGER,
    apt INTEGER,
    zip INTEGER
);

CREATE TABLE IF NOT EXISTS Domains (
    domainID INTEGER PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS Services (
    serviceID INTEGER PRIMARY KEY,
    serviceName TEXT NOT NULL,
    domainID INTEGER,
    platformID INTEGER,
    FOREIGN KEY (domainID) REFERENCES Domains (domainID),
    FOREIGN KEY (platformID) REFERENCES Platforms (platformID)
);

CREATE TABLE IF NOT EXISTS Urls (
    urlID INTEGER PRIMARY KEY,
    urlAddress TEXT NOT NULL,
    domainID INTEGER,
    FOREIGN KEY (domainID) REFERENCES Domains (domainID)
);

CREATE TABLE IF NOT EXISTS Usernames (
    usernameID INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    nameID INTEGER,
    serviceID INTEGER,
    FOREIGN KEY (nameID) REFERENCES Names (nameID),
    FOREIGN KEY (serviceID) REFERENCES Services (serviceID)
);

CREATE TABLE IF NOT EXISTS ToolCatalog (
    toolID INTEGER PRIMARY KEY,
    toolName TEXT NOT NULL UNIQUE,
    category TEXT,
    repositoryURL TEXT,
    installMethod TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_services_domain_platform
    ON Services (domainID, platformID);
CREATE INDEX IF NOT EXISTS idx_urls_domain
    ON Urls (domainID);
CREATE INDEX IF NOT EXISTS idx_usernames_service
    ON Usernames (serviceID);
CREATE INDEX IF NOT EXISTS idx_usernames_name
    ON Usernames (nameID);
"""


def connect_db():
    """Create a database connection."""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    except sqlite3.Error as exc:
        print(f"Error connecting to database: {exc}")
    return None


def setup_database():
    """Set up the database schema."""
    conn = connect_db()
    if conn is not None:
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        except sqlite3.Error as exc:
            print(f"Error setting up database: {exc}")
        finally:
            conn.close()
    else:
        print("Error! Cannot create the database connection.")


def insert_data(table, data):
    """Insert data into a table using a generic column mapping."""
    conn = connect_db()
    if conn is not None:
        try:
            columns = ", ".join(data.keys())
            placeholders = ", ".join(["?"] * len(data))
            values = tuple(data.values())
            conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)
            conn.commit()
        except sqlite3.Error as exc:
            print(f"Error inserting data: {exc}")
        finally:
            conn.close()
    else:
        print("Error! Cannot create the database connection.")


def query_database(query):
    """Query the database and return the fetched rows."""
    conn = connect_db()
    results = []
    if conn is not None:
        try:
            cursor = conn.cursor()
            cursor.execute(query)
            results = cursor.fetchall()
        except sqlite3.Error as exc:
            print(f"Error querying database: {exc}")
        finally:
            conn.close()
    else:
        print("Error! Cannot create the database connection.")
    return results
