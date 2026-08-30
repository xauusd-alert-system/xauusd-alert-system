"""Versioned DB schema migrations (ТЗ §9.3).

Each migration module in this package must expose:

* ``VERSION: int``  — monotonically increasing migration number;
* ``NAME: str``     — short human-readable slug;
* ``apply(conn)``   — function receiving an open ``sqlite3.Connection``.
  The runner wraps it in a ``BEGIN IMMEDIATE`` transaction and records the
  migration in ``schema_migrations`` only after a successful commit.

Discovery is done by :func:`data.migrate.load_builtin_migrations` — modules
whose name does not start with ``_`` are imported in ``VERSION`` order.
"""
