#!/usr/bin/env python3
import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence


APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
DEFAULT_DB_PATH = Path("~/Library/Messages/chat.db").expanduser()


@dataclass(frozen=True)
class MessageFilters:
    sender_like: str | None = None
    text_like: str | None = None
    after: datetime | None = None
    before: datetime | None = None
    direction: str = "any"


def parse_datetime(value: str) -> datetime:
    if "T" in value:
        parsed = datetime.fromisoformat(value)
    else:
        parsed = datetime.fromisoformat(f"{value}T00:00:00")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def open_connection(db_path: Path, *, write: bool = False) -> sqlite3.Connection:
    if write:
        connection = sqlite3.connect(db_path)
    else:
        uri = f"file:{db_path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def detect_date_scale(connection: sqlite3.Connection) -> int:
    raw_value = connection.execute(
        "SELECT MAX(ABS(date)) FROM message WHERE date IS NOT NULL"
    ).fetchone()[0]
    if raw_value is None:
        return 1_000_000_000
    return 1_000_000_000 if raw_value > 1_000_000_000_000 else 1


def apple_timestamp_to_datetime(raw_value: int | None, scale: int) -> datetime | None:
    if raw_value is None:
        return None
    return APPLE_EPOCH + timedelta(seconds=raw_value / scale)


def datetime_to_apple_timestamp(value: datetime, scale: int) -> int:
    return int((value - APPLE_EPOCH).total_seconds() * scale)


def build_where_clause(
    filters: MessageFilters, *, scale: int
) -> tuple[str, list[str | int]]:
    clauses: list[str] = []
    params: list[str | int] = []

    if filters.sender_like:
        clauses.append("COALESCE(h.id, '') LIKE ?")
        params.append(filters.sender_like)
    if filters.text_like:
        clauses.append("COALESCE(m.text, '') LIKE ?")
        params.append(filters.text_like)
    if filters.after:
        clauses.append("m.date >= ?")
        params.append(datetime_to_apple_timestamp(filters.after, scale))
    if filters.before:
        clauses.append("m.date <= ?")
        params.append(datetime_to_apple_timestamp(filters.before, scale))
    if filters.direction == "incoming":
        clauses.append("COALESCE(m.is_from_me, 0) = 0")
    elif filters.direction == "outgoing":
        clauses.append("COALESCE(m.is_from_me, 0) = 1")

    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def query_messages(
    connection: sqlite3.Connection, filters: MessageFilters, *, limit: int | None = None
) -> list[sqlite3.Row]:
    scale = detect_date_scale(connection)
    where_sql, params = build_where_clause(filters, scale=scale)
    limit_sql = f" LIMIT {int(limit)}" if limit else ""
    sql = f"""
        SELECT
            m.ROWID AS message_id,
            COALESCE(h.id, '') AS sender,
            COALESCE(m.text, '') AS text,
            m.date AS raw_date,
            COALESCE(m.is_from_me, 0) AS is_from_me
        FROM message AS m
        LEFT JOIN handle AS h ON h.ROWID = m.handle_id
        {where_sql}
        ORDER BY m.date ASC
        {limit_sql}
    """
    rows = connection.execute(sql, params).fetchall()
    return rows


def query_spans(connection: sqlite3.Connection, filters: MessageFilters) -> list[sqlite3.Row]:
    scale = detect_date_scale(connection)
    where_sql, params = build_where_clause(filters, scale=scale)
    sql = f"""
        SELECT
            COALESCE(h.id, '') AS sender,
            COUNT(*) AS message_count,
            MIN(m.date) AS first_raw_date,
            MAX(m.date) AS last_raw_date
        FROM message AS m
        LEFT JOIN handle AS h ON h.ROWID = m.handle_id
        {where_sql}
        GROUP BY COALESCE(h.id, '')
        ORDER BY message_count DESC, last_raw_date DESC
    """
    return connection.execute(sql, params).fetchall()


def delete_matching_messages(
    connection: sqlite3.Connection, filters: MessageFilters
) -> tuple[int, list[int]]:
    scale = detect_date_scale(connection)
    where_sql, params = build_where_clause(filters, scale=scale)
    ids = [
        row[0]
        for row in connection.execute(
            f"""
            SELECT m.ROWID
            FROM message AS m
            LEFT JOIN handle AS h ON h.ROWID = m.handle_id
            {where_sql}
            """,
            params,
        ).fetchall()
    ]
    if not ids:
        return 0, []

    placeholders = ",".join("?" for _ in ids)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        f"DELETE FROM chat_message_join WHERE message_id IN ({placeholders})", ids
    )
    connection.execute(
        f"DELETE FROM message_attachment_join WHERE message_id IN ({placeholders})", ids
    )
    deleted = connection.execute(
        f"DELETE FROM message WHERE ROWID IN ({placeholders})", ids
    ).rowcount
    connection.commit()
    return deleted, ids


def serialize_message(row: sqlite3.Row, *, scale: int) -> dict[str, object]:
    timestamp = apple_timestamp_to_datetime(row["raw_date"], scale)
    return {
        "message_id": row["message_id"],
        "sender": row["sender"],
        "direction": "outgoing" if row["is_from_me"] else "incoming",
        "timestamp": timestamp.isoformat() if timestamp else None,
        "text": row["text"],
    }


def print_messages(rows: Sequence[sqlite3.Row], *, scale: int, as_json: bool) -> None:
    payload = [serialize_message(row, scale=scale) for row in rows]
    if as_json:
        print(json.dumps(payload, indent=2))
        return

    for item in payload:
        print(
            f"{item['message_id']}\t{item['timestamp']}\t{item['direction']}\t"
            f"{item['sender']}\t{item['text']}"
        )


def print_spans(rows: Iterable[sqlite3.Row], *, scale: int, as_json: bool) -> None:
    payload = []
    for row in rows:
        payload.append(
            {
                "sender": row["sender"],
                "message_count": row["message_count"],
                "first_message": apple_timestamp_to_datetime(
                    row["first_raw_date"], scale
                ).isoformat(),
                "last_message": apple_timestamp_to_datetime(
                    row["last_raw_date"], scale
                ).isoformat(),
            }
        )

    if as_json:
        print(json.dumps(payload, indent=2))
        return

    for item in payload:
        print(
            f"{item['sender']}\t{item['message_count']}\t"
            f"{item['first_message']}\t{item['last_message']}"
        )


def add_shared_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="Path to the macOS Messages chat.db file",
    )
    parser.add_argument(
        "--sender-like",
        help="SQL LIKE pattern for sender/handle, e.g. '%%campaign%%' or '+1888%%'",
    )
    parser.add_argument(
        "--text-like",
        help="SQL LIKE pattern for message text, e.g. '%%vote%%'",
    )
    parser.add_argument(
        "--after",
        type=parse_datetime,
        help="Only include messages on or after this UTC timestamp (YYYY-MM-DD or ISO-8601)",
    )
    parser.add_argument(
        "--before",
        type=parse_datetime,
        help="Only include messages on or before this UTC timestamp (YYYY-MM-DD or ISO-8601)",
    )
    parser.add_argument(
        "--direction",
        choices=("any", "incoming", "outgoing"),
        default="any",
        help="Limit matching to incoming or outgoing messages",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON output"
    )


def filters_from_args(args: argparse.Namespace) -> MessageFilters:
    return MessageFilters(
        sender_like=args.sender_like,
        text_like=args.text_like,
        after=args.after,
        before=args.before,
        direction=args.direction,
    )


def has_narrowing_filter(filters: MessageFilters) -> bool:
    return any(
        (
            filters.sender_like,
            filters.text_like,
            filters.after is not None,
            filters.before is not None,
        )
    )


def command_messages(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser()
    with open_connection(db_path) as connection:
        scale = detect_date_scale(connection)
        rows = query_messages(connection, filters_from_args(args), limit=args.limit)
        print_messages(rows, scale=scale, as_json=args.json)
    return 0


def command_spans(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser()
    with open_connection(db_path) as connection:
        scale = detect_date_scale(connection)
        rows = query_spans(connection, filters_from_args(args))
        print_spans(rows, scale=scale, as_json=args.json)
    return 0


def command_delete(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser()
    filters = filters_from_args(args)
    if not has_narrowing_filter(filters):
        raise SystemExit(
            "Refusing to use delete without at least one of --sender-like, --text-like, --after, or --before."
        )
    if not args.apply:
        print("Dry run only. Re-run with --apply to delete matching messages.")
        return command_spans(args)

    with open_connection(db_path, write=True) as connection:
        deleted, ids = delete_matching_messages(connection, filters)
    if args.json:
        print(json.dumps({"deleted": deleted, "message_ids": ids}, indent=2))
    else:
        print(f"Deleted {deleted} messages.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enumerate and bulk clean up iMessage messages from macOS chat.db"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    messages_parser = subparsers.add_parser("messages", help="List matching messages")
    add_shared_filters(messages_parser)
    messages_parser.add_argument(
        "--limit", type=int, default=100, help="Maximum number of messages to return"
    )
    messages_parser.set_defaults(func=command_messages)

    spans_parser = subparsers.add_parser(
        "spans", help="Report first/last message span for each matching sender"
    )
    add_shared_filters(spans_parser)
    spans_parser.set_defaults(func=command_spans)

    delete_parser = subparsers.add_parser(
        "delete",
        help="Delete matching messages after reviewing a dry-run span report",
    )
    add_shared_filters(delete_parser)
    delete_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete matching messages instead of performing a dry run",
    )
    delete_parser.set_defaults(func=command_delete)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
