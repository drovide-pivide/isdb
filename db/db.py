"""
db.py
-----
Thin Postgres helper shared by every pipeline script. It exists so that
match_stats_fetch.py, pl_xg_timeline_fetch.py, wc_xg_timeline_fetch.py,
update_imdb.py, build_pl_frontend.py and predict_excitingness.py can swap
their "open a file, check if it exists, read/write JSON or CSV" calls for
one-line equivalents that hit the database instead, without changing any of
the parsing/feature logic around them.

Connects using the DATABASE_URL environment variable (works with Supabase,
Neon, Railway, RDS, a local Postgres — anything). Never hardcode a connection
string; set DATABASE_URL in your shell, a local .env file (gitignored), or
as a GitHub Actions secret.

    export DATABASE_URL="postgresql://user:password@host:5432/dbname"

Two kinds of storage:

  1. raw_cache   — a generic (source, cache_key) -> JSON payload cache.
                   Replaces the per-match JSON files under data/match_stats/raw/
                   and data/xg_timeline/{pl,wc}/. Used for resumability: a
                   fetcher checks cache_exists() before hitting the network,
                   same as it used to check os.path.exists().

  2. everything else — plain tables, one row per match/episode/etc. Read and
                   written with the generic upsert_rows() / read_df() /
                   read_rows() helpers below, which work against ANY table in
                   db/schema.sql. Column names with a hyphen in the original
                   CSVs (e.g. "xg_home_0-15") are auto-translated to
                   underscores ("xg_home_0_15") on the way in and out, since
                   Postgres won't accept a hyphen in an unquoted identifier —
                   callers keep using the original hyphenated names.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from typing import Any, Iterable

import psycopg2
import psycopg2.extras


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Export it, put it in a local .env "
            "(see .env.example), or set it as a GitHub Actions secret."
        )
    return dsn


def _sqlalchemy_dsn() -> str:
    """Same as _dsn(), but with the driver pinned explicitly to psycopg2.

    _cursor() connects with a raw psycopg2.connect(_dsn()) call, which never
    goes through SQLAlchemy's dialect resolution and is unaffected by this.
    read_df() is the one place that hands the DSN to SQLAlchemy's
    create_engine(), and a bare "postgresql://..." URL leaves SQLAlchemy to
    pick a default DBAPI driver for the postgresql dialect — which package
    it picks for a driver-less URL is version-dependent, and on newer
    SQLAlchemy releases (2.x) it can resolve to `psycopg` (v3, a different
    package from psycopg2 that this project never installs) instead of
    `psycopg2`, raising ModuleNotFoundError: No module named 'psycopg'.
    Rewriting the scheme to "postgresql+psycopg2://..." pins the dialect
    explicitly so this never depends on SQLAlchemy's default resolution."""
    dsn = _dsn()
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg2://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+psycopg2://", 1)
    return dsn


@contextmanager
def _cursor(commit: bool = False):
    conn = psycopg2.connect(_dsn())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        if commit:
            conn.commit()
    finally:
        conn.close()


# ===========================================================================
# raw_cache — resumable per-match JSON cache
# ===========================================================================

def cache_exists(source: str, cache_key: str) -> bool:
    with _cursor() as cur:
        cur.execute(
            "select 1 from raw_cache where source = %s and cache_key = %s",
            (source, cache_key),
        )
        return cur.fetchone() is not None


def cache_get(source: str, cache_key: str) -> dict | None:
    with _cursor() as cur:
        cur.execute(
            "select payload from raw_cache where source = %s and cache_key = %s",
            (source, cache_key),
        )
        row = cur.fetchone()
        return row["payload"] if row else None


def cache_get_by_match(source: str, match_id: int, season_year: int | None = None) -> dict | None:
    """Look a cached payload up by match_id instead of its exact cache_key —
    what predict_excitingness.py needs, since it only has the match_id."""
    with _cursor() as cur:
        if season_year is not None:
            cur.execute(
                "select payload from raw_cache "
                "where source = %s and match_id = %s and season_year = %s "
                "order by fetched_at desc limit 1",
                (source, match_id, season_year),
            )
        else:
            cur.execute(
                "select payload from raw_cache "
                "where source = %s and match_id = %s "
                "order by fetched_at desc limit 1",
                (source, match_id),
            )
        row = cur.fetchone()
        return row["payload"] if row else None


def cache_put(source: str, cache_key: str, payload: dict,
              match_id: int | None = None, season_year: int | None = None) -> None:
    with _cursor(commit=True) as cur:
        cur.execute(
            """
            insert into raw_cache (source, cache_key, match_id, season_year, payload, fetched_at)
            values (%s, %s, %s, %s, %s, now())
            on conflict (source, cache_key) do update
                set payload = excluded.payload,
                    match_id = excluded.match_id,
                    season_year = excluded.season_year,
                    fetched_at = now()
            """,
            (source, cache_key, match_id, season_year, json.dumps(payload)),
        )


def cache_keys(source: str) -> list[str]:
    """Every cache_key currently stored for a source — replaces
    os.listdir(RAW_DIR)."""
    with _cursor() as cur:
        cur.execute("select cache_key from raw_cache where source = %s order by cache_key", (source,))
        return [r["cache_key"] for r in cur.fetchall()]


def cache_all(source: str) -> list[tuple[str, dict]]:
    """Every (cache_key, payload) pair for a source — replaces the
    'listdir then open+json.load every file' loop used to rebuild a
    features CSV from the cache with --features-only."""
    with _cursor() as cur:
        cur.execute(
            "select cache_key, payload from raw_cache where source = %s order by cache_key",
            (source,),
        )
        return [(r["cache_key"], r["payload"]) for r in cur.fetchall()]


# ===========================================================================
# Generic table read/write — used for every features/output/reference table
# ===========================================================================

def _sanitize(col: str) -> str:
    """'xg_home_0-15' -> 'xg_home_0_15'. Idempotent."""
    return re.sub(r"-", "_", col)


def upsert_rows(table: str, rows: Iterable[dict], key_cols: list[str]) -> int:
    """Upsert a list of dicts into `table`, matching on key_cols. Column
    names are sanitized (hyphens -> underscores) automatically. Any dict
    key not present as a column in the table is silently ignored, the same
    way csv.DictWriter(..., extrasaction='ignore') behaved."""
    rows = list(rows)
    if not rows:
        return 0

    with _cursor() as cur:
        cur.execute(
            "select column_name, data_type from information_schema.columns where table_name = %s",
            (table,),
        )
        col_types = {r["column_name"]: r["data_type"] for r in cur.fetchall()}
    valid_cols = set(col_types)
    if not valid_cols:
        raise RuntimeError(f"table '{table}' not found — did you run db/schema.sql?")

    sanitized_rows = []
    for row in rows:
        clean = {_sanitize(k): v for k, v in row.items()}
        clean = {k: v for k, v in clean.items() if k in valid_cols}
        sanitized_rows.append(clean)

    cols = sorted({k for row in sanitized_rows for k in row})
    key_cols = [_sanitize(k) for k in key_cols]
    update_cols = [c for c in cols if c not in key_cols]

    col_list = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))
    conflict = ", ".join(key_cols)
    if update_cols:
        set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
        # keep updated_at fresh if the table has one, without requiring
        # every caller to pass it explicitly
        if "updated_at" in valid_cols and "updated_at" not in update_cols:
            set_clause += ", updated_at = now()"
        conflict_clause = f"on conflict ({conflict}) do update set {set_clause}"
    else:
        conflict_clause = f"on conflict ({conflict}) do nothing"

    sql = f"insert into {table} ({col_list}) values ({placeholders}) {conflict_clause}"

    int_types = {"integer", "bigint", "smallint"}
    bool_types = {"boolean"}

    def _row_values(row: dict) -> tuple:
        vals = []
        for c in cols:
            v = row.get(c)
            # a blank CSV cell (csv.DictReader gives "") always means NULL
            # for the numeric/date/boolean columns this pipeline writes
            if v == "":
                v = None
            elif isinstance(v, (dict, list)):
                # jsonb columns need an explicit json.dumps
                v = json.dumps(v)
            elif isinstance(v, str) and col_types.get(c) in int_types:
                # a value that passed through pandas (e.g. an int column
                # that had a NaN somewhere) round-trips through CSV as
                # "4.0" instead of "4" — an integer column needs the int
                v = int(float(v))
            elif isinstance(v, str) and col_types.get(c) in bool_types:
                v = v.strip().lower() in ("true", "t", "1", "yes")
            vals.append(v)
        return tuple(vals)

    values = [_row_values(row) for row in sanitized_rows]

    with _cursor(commit=True) as cur:
        psycopg2.extras.execute_batch(cur, sql, values, page_size=200)

    return len(values)


def read_rows(table: str, where: str | None = None, params: tuple = ()) -> list[dict]:
    """SELECT * FROM table [WHERE where], as a list of plain dicts —
    the drop-in replacement for csv.DictReader(open(path))."""
    sql = f"select * from {table}"
    if where:
        sql += f" where {where}"
    with _cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def read_df(table_or_sql: str, params: tuple | dict | None = None):
    """SELECT * FROM table (if given a bare table name) or an arbitrary
    query, as a pandas DataFrame — the drop-in replacement for
    pd.read_csv(path). Hyphenated CSV column names come back with
    underscores, same as they're stored; rename on the caller's side if a
    script still refers to them with a hyphen internally (e.g. FEATURE_SET)."""
    import pandas as pd  # local import: db.py itself has no hard pandas dependency
    from sqlalchemy import create_engine

    sql = table_or_sql
    if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table_or_sql):
        sql = f"select * from {table_or_sql}"
    engine = create_engine(_sqlalchemy_dsn())
    try:
        return pd.read_sql(sql, engine, params=params)
    finally:
        engine.dispose()


def table_exists_and_has_rows(table: str) -> bool:
    with _cursor() as cur:
        cur.execute(f"select 1 from {table} limit 1")
        return cur.fetchone() is not None


# ===========================================================================
# Observability — optional, used by the GitHub Actions workflow wrapper
# ===========================================================================

def log_run_start(job: str) -> int:
    with _cursor(commit=True) as cur:
        cur.execute(
            "insert into pipeline_runs (job, status) values (%s, 'running') returning id",
            (job,),
        )
        return cur.fetchone()["id"]


def log_run_end(run_id: int, status: str, detail: str = "") -> None:
    with _cursor(commit=True) as cur:
        cur.execute(
            "update pipeline_runs set finished_at = now(), status = %s, detail = %s where id = %s",
            (status, detail[:2000], run_id),
        )
