import argparse
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from imsg_cli import (
    APPLE_EPOCH,
    MessageFilters,
    _existing_tables,
    backup_database,
    command_delete,
    delete_matching_messages,
    detect_date_scale,
    datetime_to_apple_timestamp,
    extract_body_text,
    open_connection,
    query_messages,
    query_spans,
)


class IMessageCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "chat.db"
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE handle (
                ROWID INTEGER PRIMARY KEY,
                id TEXT
            );
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                handle_id INTEGER,
                text TEXT,
                attributedBody BLOB,
                date INTEGER,
                is_from_me INTEGER DEFAULT 0
            );
            CREATE TABLE chat_message_join (
                chat_id INTEGER,
                message_id INTEGER
            );
            CREATE TABLE message_attachment_join (
                message_id INTEGER,
                attachment_id INTEGER
            );
            """
        )

        scale = 1_000_000_000
        base = APPLE_EPOCH
        messages = [
            (1, 1, "Vote for Alice", None, datetime_to_apple_timestamp(base, scale), 0),
            (
                2, 1, "Reminder to donate", None,
                datetime_to_apple_timestamp(base.replace(day=2), scale), 0,
            ),
            (
                3, 2, "Family message", None,
                datetime_to_apple_timestamp(base.replace(day=3), scale), 0,
            ),
            (
                4, 1, "Sent reply", None,
                datetime_to_apple_timestamp(base.replace(day=4), scale), 1,
            ),
        ]
        connection.executemany("INSERT INTO handle (ROWID, id) VALUES (?, ?)", [
            (1, "campaign"),
            (2, "friend"),
        ])
        connection.executemany(
            "INSERT INTO message (ROWID, handle_id, text, attributedBody, date, is_from_me)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            messages,
        )
        connection.executemany(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
            [(1, 1), (1, 2), (2, 3), (1, 4)],
        )
        connection.executemany(
            "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (?, ?)",
            [(1, 100), (2, 101), (3, 102), (4, 103)],
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_query_messages_filters_by_sender_text_and_direction(self) -> None:
        filters = MessageFilters(
            sender_like="campaign",
            text_like="%Vote%",
            direction="incoming",
        )
        with open_connection(self.db_path) as connection:
            scale = detect_date_scale(connection)
            rows = query_messages(connection, filters, scale=scale, limit=10)

        self.assertEqual([row["message_id"] for row in rows], [1])
        self.assertEqual(rows[0]["text"], "Vote for Alice")

    def test_query_spans_groups_matches_by_sender(self) -> None:
        filters = MessageFilters(sender_like="campaign", direction="incoming")
        with open_connection(self.db_path) as connection:
            scale = detect_date_scale(connection)
            spans = query_spans(connection, filters, scale=scale)

        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["sender"], "campaign")
        self.assertEqual(spans[0]["message_count"], 2)
        self.assertEqual(scale, 1_000_000_000)

    def test_delete_matching_messages_removes_join_rows(self) -> None:
        filters = MessageFilters(sender_like="campaign", direction="incoming")
        with open_connection(self.db_path, write=True) as connection:
            scale = detect_date_scale(connection)
            deleted, ids = delete_matching_messages(connection, filters, scale=scale)

        self.assertEqual(deleted, 2)
        self.assertEqual(ids, [1, 2])

        connection = sqlite3.connect(self.db_path)
        remaining_messages = connection.execute(
            "SELECT ROWID FROM message ORDER BY ROWID"
        ).fetchall()
        remaining_joins = connection.execute(
            "SELECT message_id FROM chat_message_join ORDER BY message_id"
        ).fetchall()
        remaining_attachments = connection.execute(
            "SELECT message_id FROM message_attachment_join ORDER BY message_id"
        ).fetchall()
        connection.close()

        self.assertEqual(remaining_messages, [(3,), (4,)])
        self.assertEqual(remaining_joins, [(3,), (4,)])
        self.assertEqual(remaining_attachments, [(3,), (4,)])

    def test_extract_body_text_parses_attributed_string(self) -> None:
        blob = (
            b"\x04\x0bstreamtyped\x81\xe8\x03"
            b"\x84\x01\x40\x84\x84\x84\x12NSAttributedString\x00"
            b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01"
            b"\x94\x84\x01\x2b\x0bHello world"
        )
        self.assertEqual(extract_body_text(blob), "Hello world")

    def test_extract_body_text_returns_none_for_missing(self) -> None:
        self.assertIsNone(extract_body_text(None))
        self.assertIsNone(extract_body_text(b""))
        self.assertIsNone(extract_body_text(b"garbage data"))

    def test_text_like_filters_against_attributed_body(self) -> None:
        blob = (
            b"\x04\x0bstreamtyped\x81\xe8\x03"
            b"\x84\x01\x40\x84\x84\x84\x12NSAttributedString\x00"
            b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01"
            b"\x94\x84\x01\x2b\x12Reply to campaign!"
        )
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "UPDATE message SET text = NULL, attributedBody = ? WHERE ROWID = 1",
            (blob,),
        )
        connection.commit()
        connection.close()

        filters = MessageFilters(text_like="%campaign%")
        with open_connection(self.db_path) as conn:
            scale = detect_date_scale(conn)
            rows = query_messages(conn, filters, scale=scale)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], 1)

    def test_delete_skips_missing_join_tables(self) -> None:
        with open_connection(self.db_path) as connection:
            existing = _existing_tables(
                connection,
                ["chat_message_join", "chat_recoverable_message_join", "nonexistent"],
            )
        self.assertEqual(existing, {"chat_message_join"})

    def test_delete_rolls_back_on_error(self) -> None:
        filters = MessageFilters(sender_like="campaign", direction="incoming")
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TRIGGER fail_msg_delete BEFORE DELETE ON message "
            "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
        )
        connection.commit()

        scale = detect_date_scale(connection)
        with self.assertRaises(sqlite3.IntegrityError):
            delete_matching_messages(connection, filters, scale=scale)
        connection.close()

        verify = sqlite3.connect(self.db_path)
        msg_count = verify.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        join_count = verify.execute(
            "SELECT COUNT(*) FROM chat_message_join"
        ).fetchone()[0]
        verify.close()
        self.assertEqual(msg_count, 4)
        self.assertEqual(join_count, 4)

    def test_delete_command_requires_a_narrowing_filter(self) -> None:
        args = argparse.Namespace(
            db=str(self.db_path),
            sender_like=None,
            text_like=None,
            after=None,
            before=None,
            direction="incoming",
            json=False,
            apply=True,
        )

        with self.assertRaises(SystemExit) as context:
            command_delete(args)

        self.assertIn("Refusing to use delete", str(context.exception.code))

    @patch("imsg_cli.is_messages_running", return_value=True)
    def test_delete_command_refuses_while_messages_running(self, _mock: object) -> None:
        args = argparse.Namespace(
            db=str(self.db_path),
            sender_like="campaign",
            text_like=None,
            after=None,
            before=None,
            direction="incoming",
            json=False,
            apply=True,
        )

        with self.assertRaises(SystemExit) as context:
            command_delete(args)

        self.assertEqual(context.exception.code, 1)

    def test_backup_database_creates_copy(self) -> None:
        backup_path = backup_database(self.db_path)
        self.assertTrue(backup_path.exists())
        self.assertIn("chat.db.backup_", backup_path.name)

        original_size = self.db_path.stat().st_size
        backup_size = backup_path.stat().st_size
        self.assertEqual(original_size, backup_size)

    def test_backup_database_cleans_up_on_failure(self) -> None:
        with patch("imsg_cli.shutil.copy2", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                backup_database(self.db_path)

        backups = list(self.db_path.parent.glob("*.backup_*"))
        self.assertEqual(backups, [])


if __name__ == "__main__":
    unittest.main()
