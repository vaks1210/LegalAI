"""
SQLite-backed document store used when Cosmos DB is not configured.

Each "container" is a table of (id, JSON body). It implements the small
subset of the Cosmos container API that CosmosService relies on, plus a
`find()` helper that filters documents with a Python predicate in place of
Cosmos SQL queries.
"""
import json
import logging
import os
import sqlite3
import threading
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)


class LocalNotFoundError(Exception):
    """Raised when a document id does not exist (mirrors CosmosResourceNotFoundError)."""


class LocalContainer:
    def __init__(self, store: "LocalDocumentStore", name: str):
        self._store = store
        self.name   = name

    # ── Cosmos-compatible surface ────────────────────────────

    def create_item(self, body: dict) -> dict:
        with self._store.lock:
            try:
                self._store.conn.execute(
                    f'INSERT INTO "{self.name}" (id, body) VALUES (?, ?)',
                    (body["id"], json.dumps(body)),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"Document {body['id']} already exists in {self.name}")
            self._store.conn.commit()
        return body

    def upsert_item(self, body: dict) -> dict:
        with self._store.lock:
            self._store.conn.execute(
                f'INSERT INTO "{self.name}" (id, body) VALUES (?, ?) '
                f"ON CONFLICT(id) DO UPDATE SET body = excluded.body",
                (body["id"], json.dumps(body)),
            )
            self._store.conn.commit()
        return body

    def replace_item(self, item: str, body: dict) -> dict:
        with self._store.lock:
            cur = self._store.conn.execute(
                f'UPDATE "{self.name}" SET body = ? WHERE id = ?',
                (json.dumps(body), item),
            )
            self._store.conn.commit()
        if cur.rowcount == 0:
            raise LocalNotFoundError(item)
        return body

    def read_item(self, item: str, partition_key: str = None) -> dict:
        with self._store.lock:
            row = self._store.conn.execute(
                f'SELECT body FROM "{self.name}" WHERE id = ?', (item,)
            ).fetchone()
        if not row:
            raise LocalNotFoundError(item)
        return json.loads(row[0])

    def delete_item(self, item: str, partition_key: str = None) -> None:
        with self._store.lock:
            self._store.conn.execute(f'DELETE FROM "{self.name}" WHERE id = ?', (item,))
            self._store.conn.commit()

    # ── Local query helper ───────────────────────────────────

    def all(self) -> Iterable[dict]:
        with self._store.lock:
            rows = self._store.conn.execute(f'SELECT body FROM "{self.name}"').fetchall()
        return [json.loads(r[0]) for r in rows]

    def find(
        self,
        predicate: Callable[[dict], bool],
        sort_key: Optional[str] = None,
        descending: bool = False,
    ) -> list:
        docs = [d for d in self.all() if predicate(d)]
        if sort_key:
            docs.sort(key=lambda d: d.get(sort_key) or "", reverse=descending)
        return docs


class LocalDocumentStore:
    CONTAINERS = ("cases", "users", "negotiations", "documents")

    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None
        for name in self.CONTAINERS:
            self.conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{name}" (id TEXT PRIMARY KEY, body TEXT NOT NULL)'
            )
        self.conn.commit()
        logger.info(f"Local document store ready | path={path}")

    def container(self, name: str) -> LocalContainer:
        return LocalContainer(self, name)

    def ping(self) -> None:
        with self.lock:
            self.conn.execute("SELECT 1").fetchone()
