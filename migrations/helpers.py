"""Helpers for migrations on top of TimescaleDB.

Verified on a live DB 2026-08-07 (TimescaleDB 2.17.2, PG16).

| operation         | on a compressed hypertable |
|-------------------|----------------------------|
| ADD COLUMN        | works                      |
| DROP COLUMN       | works                      |
| RENAME COLUMN     | works                      |
| ALTER COLUMN TYPE | ERROR                      |

A type change fails with "operation not supported on hypertables that have
compression enabled".

**Decompressing the chunks is not enough.** The check is on the
`timescaledb.compress` setting itself, not on the presence of compressed
chunks: with decompressed chunks but compression enabled, a type change is
still rejected. The workaround must switch compression off entirely.

The whole procedure runs inside a single transaction; this matters because
that is exactly how Alembic wraps a migration.
"""

from __future__ import annotations

from alembic import op


def _scalar(sql: str, *params: object) -> object | None:
    row = op.get_bind().exec_driver_sql(sql, params or None).fetchone()
    return row[0] if row else None


def compression_settings(table: str) -> dict[str, str] | None:
    """Current compression settings of a hypertable.

    Returns None if compression is not enabled: then no workaround is
    needed and the DDL is applied directly.
    """
    row = (
        op.get_bind()
        .exec_driver_sql(
            """
            SELECT segmentby, orderby
            FROM timescaledb_information.compression_settings
            WHERE hypertable_name = %s
            LIMIT 1
            """,
            (table,),
        )
        .fetchone()
    )
    if row is None:
        return None
    return {"segmentby": row[0] or "", "orderby": row[1] or ""}


def compression_policy_interval(table: str) -> str | None:
    """Interval of the compression policy, if one is set."""
    return _scalar(
        """
        SELECT config->>'compress_after'
        FROM timescaledb_information.jobs
        WHERE hypertable_name = %s AND proc_name = 'policy_compression'
        LIMIT 1
        """,
        table,
    )


def alter_compressed_table(table: str, statements: list[str]) -> None:
    """Runs DDL that is not allowed while compression is enabled.

    The order was verified on a live DB: remove the policy, decompress the
    chunks, TURN OFF compression, apply the changes, turn compression back
    on, restore the policy.

    Needed only for a column type change. Do not call it for
    ADD/DROP/RENAME: they work directly, and decompressing a large table
    costs minutes and disk space.
    """
    settings = compression_settings(table)
    if settings is None:
        for statement in statements:
            op.execute(statement)
        return

    interval = compression_policy_interval(table)

    if interval is not None:
        op.execute(f"SELECT remove_compression_policy('{table}')")

    # if_compressed => true: otherwise decompressing an already decompressed
    # chunk aborts the migration with an error.
    op.execute(
        f"SELECT decompress_chunk(c, if_compressed => true) "
        f"FROM show_chunks('{table}') c"
    )
    op.execute(f"ALTER TABLE {table} SET (timescaledb.compress = false)")

    for statement in statements:
        op.execute(statement)

    options = ["timescaledb.compress"]
    if settings["segmentby"]:
        options.append(f"timescaledb.compress_segmentby = '{settings['segmentby']}'")
    if settings["orderby"]:
        options.append(f"timescaledb.compress_orderby = '{settings['orderby']}'")
    op.execute(f"ALTER TABLE {table} SET ({', '.join(options)})")

    if interval is not None:
        op.execute(f"SELECT add_compression_policy('{table}', INTERVAL '{interval}')")
