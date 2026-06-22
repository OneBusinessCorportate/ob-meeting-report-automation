"""Reusable in-memory fakes for offline tests (no network / no secrets)."""
from __future__ import annotations

import copy
import uuid


class _Result:
    def __init__(self, data):
        self.data = data


class FakeTable:
    """A tiny chainable query builder over a list of dict rows."""

    def __init__(self, store, name):
        self._store = store
        self._name = name
        self._rows = store.setdefault(name, [])
        self._op = "select"
        self._payload = None
        self._on_conflict = None
        self._filters = []
        self._order = None
        self._desc = False
        self._limit = None

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def upsert(self, payload, on_conflict=None, ignore_duplicates=False):
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        self._ignore_duplicates = ignore_duplicates
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, value):
        self._filters.append(("eq", col, value))
        return self

    def gte(self, col, value):
        self._filters.append(("gte", col, value))
        return self

    def lt(self, col, value):
        self._filters.append(("lt", col, value))
        return self

    def order(self, col, desc=False):
        self._order = col
        self._desc = desc
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, row):
        for op, col, value in self._filters:
            cell = row.get(col)
            if op == "eq" and cell != value:
                return False
            if op == "gte" and not (cell is not None and str(cell) >= str(value)):
                return False
            if op == "lt" and not (cell is not None and str(cell) < str(value)):
                return False
        return True

    def execute(self):
        if self._op in ("insert", "upsert"):
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            inserted = []
            for payload in payloads:
                row = copy.deepcopy(payload)
                if self._op == "upsert" and self._on_conflict:
                    keys = [k.strip() for k in self._on_conflict.split(",")]
                    existing = next(
                        (r for r in self._rows if all(r.get(k) == row.get(k) for k in keys)),
                        None,
                    )
                    if existing:
                        if getattr(self, "_ignore_duplicates", False):
                            continue  # PostgREST returns no row for ignored dupes
                        existing.update(row)
                        inserted.append(copy.deepcopy(existing))
                        continue
                row.setdefault("id", str(uuid.uuid4()))
                self._rows.append(row)
                inserted.append(copy.deepcopy(row))
            return _Result(inserted)

        if self._op == "delete":
            deleted = [copy.deepcopy(r) for r in self._rows if self._match(r)]
            self._rows[:] = [r for r in self._rows if not self._match(r)]
            return _Result(deleted)

        if self._op == "update":
            updated = []
            for row in self._rows:
                if self._match(row):
                    row.update(self._payload)
                    updated.append(copy.deepcopy(row))
            return _Result(updated)

        rows = [copy.deepcopy(r) for r in self._rows if self._match(r)]
        if self._order:
            rows.sort(key=lambda r: r.get(self._order) or 0, reverse=self._desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Result(rows)


class FakeSupabaseClient:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return FakeTable(self.store, name)
