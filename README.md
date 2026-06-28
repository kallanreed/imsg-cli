# imsg-cli

Small Python tooling for enumerating iMessage messages on macOS and bulk cleaning junk message runs such as political campaign blasts.

## What it does

The repository now includes `imsg_cli.py`, a dependency-free CLI that works directly against the local Messages SQLite database at `~/Library/Messages/chat.db`.

It supports three workflows:

- `messages`: enumerate matching messages
- `spans`: summarize matching senders with the first/last message timestamps and count
- `delete`: dry-run or delete matching messages in bulk

## Usage

List likely campaign messages:

```bash
python imsg_cli.py messages \
  --sender-like '%campaign%' \
  --text-like '%vote%' \
  --direction incoming \
  --limit 50
```

Report the span of a sender's message run before deleting:

```bash
python imsg_cli.py spans \
  --sender-like '+1888%' \
  --direction incoming
```

Dry-run a delete to review the affected senders first:

```bash
python imsg_cli.py delete \
  --sender-like '+1888%' \
  --text-like '%stop to end%' \
  --direction incoming
```

Apply the delete once you've reviewed the span output:

```bash
python imsg_cli.py delete \
  --sender-like '+1888%' \
  --text-like '%stop to end%' \
  --direction incoming \
  --apply
```

## Notes

- Close Messages and back up `~/Library/Messages/chat.db` before running `delete --apply`.
- `delete` refuses to run unless you provide at least one narrowing filter such as `--sender-like`, `--text-like`, `--after`, or `--before`.
- `--after` and `--before` accept either `YYYY-MM-DD` or ISO-8601 timestamps.
- Use `--json` on any command for machine-readable output.
