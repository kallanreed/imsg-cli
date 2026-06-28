#!/usr/bin/env python3
import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
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


def _noop(*_args: object) -> None:
    return None


def open_connection(db_path: Path, *, write: bool = False) -> closing[sqlite3.Connection]:
    if write:
        connection = sqlite3.connect(db_path)
        connection.create_function("after_delete_message_plugin", 2, _noop)
        connection.create_function("before_delete_attachment_path", 2, _noop)
        connection.create_function("delete_attachment_path", 1, _noop)
    else:
        uri = f"file:{db_path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return closing(connection)


def detect_date_scale(connection: sqlite3.Connection) -> int:
    raw_value = connection.execute(
        "SELECT MAX(ABS(date)) FROM message WHERE date IS NOT NULL"
    ).fetchone()[0]
    if raw_value is None:
        return 1_000_000_000
    return 1_000_000_000 if raw_value > 1_000_000_000_000 else 1


def extract_body_text(blob: bytes | None) -> str | None:
    if not blob:
        return None
    marker = b"NSString"
    idx = blob.rfind(marker)
    if idx == -1:
        return None
    search_start = idx + len(marker)
    plus = blob.find(b"\x2b", search_start, search_start + 20)
    if plus == -1:
        return None
    pos = plus + 1
    length_byte = blob[pos]
    pos += 1
    if length_byte < 0x80:
        text_len = length_byte
    elif length_byte == 0x81:
        text_len = int.from_bytes(blob[pos : pos + 2], "little")
        pos += 2
    elif length_byte == 0x82:
        text_len = int.from_bytes(blob[pos : pos + 4], "little")
        pos += 4
    else:
        return None
    try:
        return blob[pos : pos + text_len].decode("utf-8")
    except (UnicodeDecodeError, IndexError):
        return None


def apple_timestamp_to_datetime(raw_value: int | None, scale: int) -> datetime | None:
    if raw_value is None:
        return None
    return APPLE_EPOCH + timedelta(seconds=raw_value / scale)


def datetime_to_apple_timestamp(value: datetime, scale: int) -> int:
    return int((value - APPLE_EPOCH).total_seconds() * scale)


def _like_to_regex(pattern: str) -> re.Pattern[str]:
    parts = []
    for char in pattern:
        if char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.compile(f"^{''.join(parts)}$", re.DOTALL | re.IGNORECASE)


def build_where_clause(
    filters: MessageFilters, *, scale: int
) -> tuple[str, list[str | int]]:
    clauses: list[str] = []
    params: list[str | int] = []

    if filters.sender_like:
        clauses.append("COALESCE(h.id, '') LIKE ?")
        params.append(filters.sender_like)
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


def matches_text_filter(row: sqlite3.Row, filters: MessageFilters) -> bool:
    if not filters.text_like:
        return True
    text = message_text(row)
    return bool(_like_to_regex(filters.text_like).search(text))


def _query_light(
    connection: sqlite3.Connection,
    filters: MessageFilters,
    *,
    scale: int,
) -> list[sqlite3.Row]:
    where_sql, params = build_where_clause(filters, scale=scale)
    sql = f"""
        SELECT
            m.ROWID AS message_id,
            COALESCE(h.id, '') AS sender,
            m.date AS raw_date,
            COALESCE(m.is_from_me, 0) AS is_from_me
        FROM message AS m
        LEFT JOIN handle AS h ON h.ROWID = m.handle_id
        {where_sql}
        ORDER BY m.date ASC
    """
    return connection.execute(sql, params).fetchall()


def _query_full(
    connection: sqlite3.Connection,
    filters: MessageFilters,
    *,
    scale: int,
) -> list[sqlite3.Row]:
    where_sql, params = build_where_clause(filters, scale=scale)
    sql = f"""
        SELECT
            m.ROWID AS message_id,
            COALESCE(h.id, '') AS sender,
            m.text AS text,
            m.attributedBody AS attributed_body,
            m.date AS raw_date,
            COALESCE(m.is_from_me, 0) AS is_from_me
        FROM message AS m
        LEFT JOIN handle AS h ON h.ROWID = m.handle_id
        {where_sql}
        ORDER BY m.date ASC
    """
    rows = connection.execute(sql, params).fetchall()
    if filters.text_like:
        rows = [r for r in rows if matches_text_filter(r, filters)]
    return rows


def query_messages(
    connection: sqlite3.Connection,
    filters: MessageFilters,
    *,
    scale: int,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    rows = _query_full(connection, filters, scale=scale)
    if limit is not None:
        rows = rows[:limit]
    return rows


def query_spans(
    connection: sqlite3.Connection, filters: MessageFilters, *, scale: int
) -> list[dict[str, object]]:
    if filters.text_like:
        rows = _query_full(connection, filters, scale=scale)
    else:
        rows = _query_light(connection, filters, scale=scale)
    senders: dict[str, dict[str, object]] = {}
    for row in rows:
        sender = row["sender"]
        if sender not in senders:
            senders[sender] = {
                "sender": sender,
                "message_count": 0,
                "first_raw_date": row["raw_date"],
                "last_raw_date": row["raw_date"],
            }
        entry = senders[sender]
        entry["message_count"] += 1
        if row["raw_date"] is not None:
            if entry["first_raw_date"] is None or row["raw_date"] < entry["first_raw_date"]:
                entry["first_raw_date"] = row["raw_date"]
            if entry["last_raw_date"] is None or row["raw_date"] > entry["last_raw_date"]:
                entry["last_raw_date"] = row["raw_date"]
    return sorted(
        senders.values(), key=lambda s: (-s["message_count"], -(s["last_raw_date"] or 0))
    )


JOIN_TABLES = (
    ("chat_message_join", "message_id"),
    ("chat_recoverable_message_join", "message_id"),
    ("message_attachment_join", "message_id"),
)


def _existing_tables(connection: sqlite3.Connection, candidates: Iterable[str]) -> set[str]:
    all_tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    return all_tables & set(candidates)


def delete_matching_messages(
    connection: sqlite3.Connection, filters: MessageFilters, *, scale: int
) -> tuple[int, list[int]]:
    if filters.text_like:
        rows = _query_full(connection, filters, scale=scale)
    else:
        rows = _query_light(connection, filters, scale=scale)
    ids = [row["message_id"] for row in rows]
    if not ids:
        return 0, []

    existing = _existing_tables(connection, [t for t, _ in JOIN_TABLES])
    placeholders = ",".join("?" for _ in ids)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        for table, column in JOIN_TABLES:
            if table in existing:
                connection.execute(
                    f"DELETE FROM {table} WHERE {column} IN ({placeholders})", ids
                )
        deleted = connection.execute(
            f"DELETE FROM message WHERE ROWID IN ({placeholders})", ids
        ).rowcount
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return deleted, ids


def mark_all_read(connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        "UPDATE message SET is_read = 1 WHERE is_read = 0 AND is_from_me = 0"
    )
    connection.commit()
    return cursor.rowcount


def delete_dead_chats(connection: sqlite3.Connection) -> int:
    real_contacts = {
        row[0]
        for row in connection.execute(
            "SELECT h.id FROM message m JOIN handle h ON h.ROWID = m.handle_id "
            "GROUP BY h.id HAVING COUNT(*) > 10"
        ).fetchall()
    }
    dead_chat_ids = []
    for row in connection.execute("""
        SELECT c.ROWID, c.chat_identifier,
               COUNT(cmj.message_id) AS msg_count,
               SUM(CASE WHEN m.is_from_me = 0 THEN 1 ELSE 0 END) AS incoming
        FROM chat c
        LEFT JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        LEFT JOIN message m ON m.ROWID = cmj.message_id
        GROUP BY c.ROWID
        HAVING msg_count = 0 OR (msg_count <= 3 AND incoming = 0)
    """).fetchall():
        identifier = row[1].split("(")[0].lstrip("+")
        if any(c.lstrip("+") == identifier for c in real_contacts):
            continue
        dead_chat_ids.append(row[0])
    if dead_chat_ids:
        placeholders = ",".join("?" for _ in dead_chat_ids)
        connection.execute(
            f"DELETE FROM chat_message_join WHERE chat_id IN ({placeholders})",
            dead_chat_ids,
        )
        connection.execute(
            f"DELETE FROM chat_handle_join WHERE chat_id IN ({placeholders})",
            dead_chat_ids,
        )
        connection.execute(
            f"DELETE FROM chat WHERE ROWID IN ({placeholders})", dead_chat_ids
        )
    connection.execute("""
        DELETE FROM chat_handle_join WHERE chat_id NOT IN (
            SELECT ROWID FROM chat
        )
    """)
    connection.execute("""
        DELETE FROM handle WHERE ROWID NOT IN (
            SELECT handle_id FROM message WHERE handle_id IS NOT NULL
            UNION SELECT handle_id FROM chat_handle_join WHERE handle_id IS NOT NULL
        )
    """)
    connection.execute("""
        DELETE FROM chat_recoverable_message_join WHERE chat_id NOT IN (
            SELECT ROWID FROM chat
        )
    """)
    connection.execute("""
        DELETE FROM deleted_messages WHERE ROWID NOT IN (
            SELECT ROWID FROM message
        )
    """)
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return len(dead_chat_ids)


def message_text(row: sqlite3.Row) -> str:
    if row["text"]:
        return row["text"]
    body = extract_body_text(row["attributed_body"])
    return body if body else ""


def serialize_message(row: sqlite3.Row, *, scale: int) -> dict[str, object]:
    timestamp = apple_timestamp_to_datetime(row["raw_date"], scale)
    return {
        "message_id": row["message_id"],
        "sender": row["sender"],
        "direction": "outgoing" if row["is_from_me"] else "incoming",
        "timestamp": timestamp.isoformat() if timestamp else None,
        "text": message_text(row),
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


def print_spans(
    spans: Iterable[dict[str, object]], *, scale: int, as_json: bool
) -> None:
    payload = []
    for span in spans:
        first_message = apple_timestamp_to_datetime(span["first_raw_date"], scale)
        last_message = apple_timestamp_to_datetime(span["last_raw_date"], scale)
        payload.append(
            {
                "sender": span["sender"],
                "message_count": span["message_count"],
                "first_message": first_message.isoformat() if first_message else None,
                "last_message": last_message.isoformat() if last_message else None,
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
        help="SQL LIKE pattern for sender/handle, for example a contains-match or a +1888 prefix match",
    )
    parser.add_argument(
        "--text-like",
        help="SQL LIKE pattern for message text, for example a contains-match for vote",
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


def is_messages_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Messages"],
            capture_output=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


DEFAULT_BACKUP_DIR = Path("~/temp").expanduser()


def backup_database(db_path: Path, backup_dir: Path = DEFAULT_BACKUP_DIR) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{db_path.name}.backup_{timestamp}"
    try:
        shutil.copy2(db_path, backup_path)
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def command_messages(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser()
    with open_connection(db_path) as connection:
        scale = detect_date_scale(connection)
        rows = query_messages(
            connection, filters_from_args(args), scale=scale, limit=args.limit
        )
        print_messages(rows, scale=scale, as_json=args.json)
    return 0


def command_spans(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser()
    with open_connection(db_path) as connection:
        scale = detect_date_scale(connection)
        rows = query_spans(connection, filters_from_args(args), scale=scale)
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

    if is_messages_running():
        print(
            "Warning: Messages.app is currently running. Quit Messages before "
            "deleting to avoid database corruption.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    backup_path = backup_database(db_path)
    print(f"Backed up database to {backup_path}", file=sys.stderr)

    with open_connection(db_path, write=True) as connection:
        scale = detect_date_scale(connection)
        deleted, ids = delete_matching_messages(connection, filters, scale=scale)
        dead_chats = delete_dead_chats(connection)
    if args.json:
        print(json.dumps({"deleted": deleted, "message_ids": ids, "dead_chats_removed": dead_chats}, indent=2))
    else:
        print(f"Deleted {deleted} messages.")
        if dead_chats:
            print(f"Removed {dead_chats} dead conversations.")
    return 0


def command_mark_read(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser()
    with open_connection(db_path, write=True) as connection:
        updated = mark_all_read(connection)
    if args.json:
        print(json.dumps({"marked_read": updated}))
    else:
        print(f"Marked {updated} messages as read.")
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

    mark_read_parser = subparsers.add_parser(
        "mark-read", help="Mark all incoming messages as read"
    )
    mark_read_parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="Path to the macOS Messages chat.db file",
    )
    mark_read_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON output"
    )
    mark_read_parser.set_defaults(func=command_mark_read)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
