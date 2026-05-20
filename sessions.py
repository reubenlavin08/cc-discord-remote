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
        # Idempotent migrations: add columns introduced after the original schema.
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(sessions)")}
        if "attached_pid" not in cols:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN attached_pid INTEGER")
        if "last_msg_id" not in cols:
            # Discord snowflake of the most recent message we processed in this channel.
            # Used at bot startup to replay anything that arrived while we were offline.
            self.conn.execute("ALTER TABLE sessions ADD COLUMN last_msg_id INTEGER")
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

    # ---- per-channel terminal attachment (persistent across bot restarts) ----

    def set_attached_pid(self, channel_id: int, pid: Optional[int]) -> None:
        existing = self.conn.execute(
            "SELECT 1 FROM sessions WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE sessions SET attached_pid = ? WHERE channel_id = ?",
                (pid, channel_id),
            )
        else:
            self.conn.execute(
                "INSERT INTO sessions (channel_id, attached_pid, cwd) VALUES (?, ?, ?)",
                (channel_id, pid, self.default_cwd),
            )
        self.conn.commit()

    def get_attached_pid(self, channel_id: int) -> Optional[int]:
        row = self.conn.execute(
            "SELECT attached_pid FROM sessions WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row[0] if row else None

    def all_attached(self) -> list:
        return [
            (r[0], r[1])
            for r in self.conn.execute(
                "SELECT channel_id, attached_pid FROM sessions WHERE attached_pid IS NOT NULL"
            ).fetchall()
        ]

    # ---- offline-message replay --------------------------------------------

    def get_last_msg_id(self, channel_id: int) -> Optional[int]:
        row = self.conn.execute(
            "SELECT last_msg_id FROM sessions WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row[0] if row else None

    def set_last_msg_id(self, channel_id: int, msg_id: int) -> None:
        existing = self.conn.execute(
            "SELECT 1 FROM sessions WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE sessions SET last_msg_id = ? WHERE channel_id = ?",
                (msg_id, channel_id),
            )
        else:
            self.conn.execute(
                "INSERT INTO sessions (channel_id, last_msg_id, cwd) VALUES (?, ?, ?)",
                (channel_id, msg_id, self.default_cwd),
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
