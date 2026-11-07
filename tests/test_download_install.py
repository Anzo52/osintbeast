import os

import database


def test_setup_database_creates_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "osintbeast_test.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))

    database.setup_database()

    assert db_path.exists()

    conn = database.connect_db()
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {row[0] for row in tables}
        assert "Platforms" in names
        assert "ToolCatalog" in names
        assert "Usernames" in names
    finally:
        conn.close()


def test_query_database_executes_select(tmp_path, monkeypatch):
    db_path = tmp_path / "osintbeast_query.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))

    database.setup_database()
    database.insert_data("Platforms", {"platform": "Twitter"})

    rows = database.query_database("SELECT platform FROM Platforms")
    assert rows == [("Twitter",)]
