import argparse
import sqlite3
import tempfile
import unittest
from pathlib import Path

from imsg_cli import (
    APPLE_EPOCH,
    MessageFilters,
    command_delete,
    delete_matching_messages,
    detect_date_scale,
    datetime_to_apple_timestamp,
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
            (1, 1, "Vote for Alice", datetime_to_apple_timestamp(base, scale), 0),
            (
                2,
                1,
                "Reminder to donate",
                datetime_to_apple_timestamp(base.replace(day=2), scale),
                0,
            ),
            (
                3,
                2,
                "Family message",
                datetime_to_apple_timestamp(base.replace(day=3), scale),
                0,
            ),
            (
                4,
                1,
                "Sent reply",
                datetime_to_apple_timestamp(base.replace(day=4), scale),
                1,
            ),
        ]
        connection.executemany("INSERT INTO handle (ROWID, id) VALUES (?, ?)", [
            (1, "campaign"),
            (2, "friend"),
        ])
        connection.executemany(
            "INSERT INTO message (ROWID, handle_id, text, date, is_from_me) VALUES (?, ?, ?, ?, ?)",
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
            rows = query_messages(connection, filters, limit=10)

        self.assertEqual([row["message_id"] for row in rows], [1])
        self.assertEqual(rows[0]["text"], "Vote for Alice")

    def test_query_spans_groups_matches_by_sender(self) -> None:
        filters = MessageFilters(sender_like="campaign", direction="incoming")
        with open_connection(self.db_path) as connection:
            rows = query_spans(connection, filters)
            scale = detect_date_scale(connection)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sender"], "campaign")
        self.assertEqual(rows[0]["message_count"], 2)
        self.assertEqual(scale, 1_000_000_000)

    def test_delete_matching_messages_removes_join_rows(self) -> None:
        filters = MessageFilters(sender_like="campaign", direction="incoming")
        with open_connection(self.db_path, write=True) as connection:
            deleted, ids = delete_matching_messages(connection, filters)

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


if __name__ == "__main__":
    unittest.main()
