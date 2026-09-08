import sqlite3
import os
from .config import get_db_path


def init_db(db_path: str | None = None) -> None:
    # No DB tables required for the AccessToken/EventNumber receive flow.
    # Keep function as no-op for backward compatibility.
    path = db_path or get_db_path()
    dirpath = os.path.dirname(path)
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)
    # Ensure empty file exists but no tables required
    conn = sqlite3.connect(path)
    try:
        conn.commit()
    finally:
        conn.close()


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn
