"""Connect and inspect Telegram alerts and an Alpaca PAPER account; no trading."""

import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

from archive import Archive

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = Path(os.environ.get("PAPER_PRIVATE", ROOT / "work" / "private"))


def private_json(name: str) -> dict:
    path = PRIVATE / name
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f"Restrict permissions on {name} to its owner first.")
    return json.loads(path.read_text())


async def telegram(args):
    from telethon import TelegramClient, errors, utils

    config = private_json("telegram.json")
    client = TelegramClient(
        str(PRIVATE / "telegram"), config["api_id"], config["api_hash"],
        device_model="Telegram Paper Alerts", app_version="0.1.0",
        flood_sleep_threshold=0, request_retries=1, connection_retries=2,
        receive_updates=False,
    )
    await client.connect()
    try:
        authorized = await client.is_user_authorized()
        if args.command == "login":
            if not authorized:
                sent = await client.send_code_request(config["phone"])
                print("Telegram login code requested. Check the verified Telegram service chat.", flush=True)
                code = getpass.getpass("Telegram code (hidden): ")
                try:
                    await client.sign_in(config["phone"], code, phone_code_hash=sent.phone_code_hash)
                except errors.SessionPasswordNeededError:
                    await client.sign_in(password=getpass.getpass("Telegram two-step password (hidden): "))
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram did not authorize the connection.")
            print("Telegram connection authorized. No orders or chat messages sent.")
        elif not authorized:
            raise RuntimeError("Connect Telegram first with the login command.")
        elif args.command == "find":
            matches = []
            async for dialog in client.iter_dialogs():
                if args.query.casefold() in dialog.name.casefold():
                    matches.append({"id": dialog.id, "title": dialog.name,
                                    "channel": dialog.is_channel, "group": dialog.is_group})
            print(json.dumps(matches, ensure_ascii=False, indent=2))
        elif args.command == "capture":
            client.flood_sleep_threshold = 30
            entity = await client.get_entity(args.chat_id)
            if utils.get_peer_id(entity) != args.chat_id:
                raise RuntimeError("Chat identity does not match the requested channel.")
            if not getattr(entity, "title", None):
                raise RuntimeError("Capture is limited to a selected group or channel.")
            archive = Archive(PRIVATE / "messages.sqlite3")
            captured = 0
            try:
                async for msg in client.iter_messages(entity, limit=args.limit or None, offset_id=args.before_id):
                    if not msg.date:
                        continue
                    captured += archive.record(
                        chat_id=args.chat_id, message_id=msg.id,
                        sent_at=msg.date.isoformat(),
                        edited_at=msg.edit_date.isoformat() if msg.edit_date else None,
                        text=msg.raw_text or "", reply_to=msg.reply_to_msg_id,
                        media_type=type(msg.media).__name__ if msg.media else None,
                        historical=True,
                    )
                rows = archive.rows(args.chat_id)
                export = PRIVATE / f"chat-{abs(args.chat_id)}.jsonl"
                with export.open("w") as out:
                    for row in rows:
                        out.write(json.dumps(row, ensure_ascii=False) + "\n")
                export.chmod(0o600)
                print(json.dumps({"chat_id": args.chat_id, "title": entity.title,
                                  "new_revisions": captured, "total_revisions": len(rows),
                                  "historical_only": True, "orders_submitted": 0}))
            finally:
                archive.close()
    except errors.FloodWaitError as exc:
        raise RuntimeError(f"Telegram requested a {exc.seconds}-second cooldown; no retry was made.") from None
    finally:
        await client.disconnect()


def alpaca_check():
    from alpaca.trading.client import TradingClient

    keys = private_json("alpaca-paper.json")
    if not keys.get("key_id") or not keys.get("secret_key"):
        raise RuntimeError("Enter the Alpaca PAPER key ID and secret in the private credentials file first.")
    # The official SDK selects the paper endpoint; no endpoint override is accepted.
    client = TradingClient(keys["key_id"].strip(), keys["secret_key"].strip(), paper=True)
    account = client.get_account()
    print(json.dumps({"environment": "paper", "status": str(account.status),
                      "trading_blocked": account.trading_blocked,
                      "options_trading_level": account.options_trading_level,
                      "orders_submitted": 0}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login")
    find = commands.add_parser("find")
    find.add_argument("--query", default="roor")
    capture = commands.add_parser("capture")
    capture.add_argument("--chat-id", required=True, type=int)
    capture.add_argument("--limit", type=int, default=300, help="Maximum messages, or 0 for all accessible history.")
    capture.add_argument("--before-id", type=int, default=0, help="Resume older history before this message ID.")
    commands.add_parser("alpaca-check")
    args = parser.parse_args()
    if args.command == "capture" and not 0 <= args.limit <= 10000:
        parser.error("Capture limit must be 0 (all history), or between 1 and 10000.")
    try:
        if args.command == "alpaca-check":
            alpaca_check()
        else:
            asyncio.run(telegram(args))
    except (RuntimeError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Avoid dumping network requests, tokens, phone numbers, or secret-bearing objects.
        print(f"Connection failed ({type(exc).__name__}). Credentials were not printed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
