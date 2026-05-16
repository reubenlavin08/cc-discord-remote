import sqlite3
import time
from typing import Optional, Tuple


class SessionStore:
    def __init__(self, path: str, default_cwd: str):
        self.conn = sqlite3.connect(path)
        self.default_cwd = default_cwd
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                channel_id INTEGER PRIMARY KEY,
                session_id TEXT,
                cwd TEXT
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                channel_id INTEGER,
                user_id INTEGER,
                kind TEXT NOT NULL,
                payload TEXT
            );
            """
        )
        self.conn.commit()

    # ---- session state ---------------------------------------------------

    def get(self, channel_id: int) -> Tuple[Optional[str], str]:
        row = self.conn.execute(
            "SELECT session_id, cwd FROM sessions WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        if row is None:
            return (None, self.default_cwd)
        return (row[0], row[1] or self.default_cwd)

    def set_session(self, channel_id: int, session_id: str) -> None:
        _, cwd = self.get(channel_id)
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions (channel_id, session_id, cwd) VALUES (?, ?, ?)",
            (channel_id, session_id, cwd),
        )
        self.conn.commit()

    def set_cwd(self, channel_id: int, cwd: str) -> None:
        # Changing cwd invalidates the resume ID because sessions are filed under cwd on disk.
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions (channel_id, session_id, cwd) VALUES (?, ?, ?)",
            (channel_id, None, cwd),
        )
        self.conn.commit()

    def set_both(self, channel_id: int, session_id: str, cwd: str) -> None:
        """Set session_id and cwd atomically — used when resuming an external session."""
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions (channel_id, session_id, cwd) VALUES (?, ?, ?)",
            (channel_id, session_id, cwd),
        )
        self.conn.commit()

    def reset(self, channel_id: int) -> None:
        _, cwd = self.get(channel_id)
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions (channel_id, session_id, cwd) VALUES (?, ?, ?)",
            (channel_id, None, cwd),
        )
        self.conn.commit()

    # ---- audit log -------------------------------------------------------

    def audit(self, channel_id: int, user_id: int, kind: str, payload: str = "") -> None:
        self.conn.execute(
            "INSERT INTO audit (ts, channel_id, user_id, kind, payload) VALUES (?, ?, ?, ?, ?)",
            (time.time(), channel_id, user_id, kind, payload),
        )
        self.conn.commit()
