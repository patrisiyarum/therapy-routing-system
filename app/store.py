"""
Storage layer.

This is a small FHIR-shaped store backed by SQLite. It stands in for Medplum
so the app runs with no account. The method names (create / read / update /
search) line up with Medplum's REST API, so swapping in a real Medplum client
later is a contained change: replace this one class, keep everything else.

We store three resource types, exactly as we would in Medplum:
  Patient        the person seeking therapy
  Practitioner   the provider
  Task           a suggestion linking a patient to a provider, with a status
                 of requested (pending), completed (accepted), or rejected
                 (declined). Caseload is just the count of completed Tasks.
"""
import json
import sqlite3
import threading
import uuid

from . import config


class LocalFhirStore:
    def __init__(self, path=None):
        self.path = str(path or config.DB_PATH)
        config.DATA_DIR.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS resources(
                resource_type TEXT, id TEXT, json TEXT,
                PRIMARY KEY(resource_type, id))""")

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def create(self, resource_type: str, resource: dict) -> dict:
        rid = resource.get("id") or uuid.uuid4().hex[:12]
        resource["id"] = rid
        resource["resourceType"] = resource_type
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO resources VALUES (?,?,?)",
                      (resource_type, rid, json.dumps(resource)))
        return resource

    def read(self, resource_type: str, rid: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT json FROM resources WHERE resource_type=? AND id=?",
                (resource_type, rid)).fetchone()
        return json.loads(row["json"]) if row else None

    def update(self, resource: dict) -> dict:
        return self.create(resource["resourceType"], resource)

    def search(self, resource_type: str, predicate=None) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT json FROM resources WHERE resource_type=?",
                (resource_type,)).fetchall()
        out = [json.loads(r["json"]) for r in rows]
        return [r for r in out if predicate(r)] if predicate else out

    def clear(self):
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM resources")


# single shared instance
store = LocalFhirStore()
