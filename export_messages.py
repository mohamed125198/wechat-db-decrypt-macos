#!/usr/bin/env python3
"""
Export WeChat chat messages from decrypted databases.

Usage:
    python3 decrypt_db.py                          # first, decrypt databases
    python3 export_messages.py                     # list all conversations
    python3 export_messages.py -c wxid_xxx         # export a specific chat
    python3 export_messages.py -c 12345@chatroom   # export a group chat
    python3 export_messages.py --all               # export all chats
    python3 export_messages.py -c wxid_xxx -n 50   # last 50 messages
"""

import sqlite3
import os
import sys
import hashlib
import argparse
from datetime import datetime


DECRYPTED_DIR = "decrypted"
MSG_TYPE_MAP = {
    1: "text",
    3: "image",
    34: "voice",
    42: "card",
    43: "video",
    47: "emoji",
    48: "location",
    49: "link/file",
    10000: "system",
    10002: "revoke",
}


def get_msg_db_path():
    msg_dir = os.path.join(DECRYPTED_DIR, "message")
    if not os.path.isdir(msg_dir):
        return None
    for f in os.listdir(msg_dir):
        if f.startswith("message_") and f.endswith(".db"):
            return os.path.join(msg_dir, f)
    return None


def get_session_db_path():
    return os.path.join(DECRYPTED_DIR, "session", "session.db")


def username_to_table(username):
    """Convert username to Msg_<md5hash> table name."""
    h = hashlib.md5(username.encode()).hexdigest()
    return f"Msg_{h}"


def list_conversations(msg_db, session_db):
    """List all conversations with their last message preview."""
    sessions = {}
    if os.path.isfile(session_db):
        conn = sqlite3.connect(session_db)
        try:
            rows = conn.execute(
                "SELECT username, type, summary, last_sender_display_name, "
                "last_timestamp FROM SessionTable ORDER BY sort_timestamp DESC"
            ).fetchall()
            for username, stype, summary, sender, ts in rows:
                sessions[username] = {
                    "type": "group" if "@chatroom" in username else "private",
                    "summary": (summary or "")[:60],
                    "sender": sender or "",
                    "time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "",
                }
        finally:
            conn.close()

    # Check which sessions have message tables
    conn = sqlite3.connect(msg_db)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
        }
        name2id = conn.execute("SELECT user_name FROM Name2Id WHERE user_name != ''").fetchall()
    finally:
        conn.close()

    results = []
    for (username,) in name2id:
        table = username_to_table(username)
        has_msgs = table in tables
        info = sessions.get(username, {})
        results.append({
            "username": username,
            "table": table,
            "has_msgs": has_msgs,
            **info,
        })

    # Sort: sessions with recent timestamps first, then others
    results.sort(key=lambda x: x.get("time", ""), reverse=True)
    return results


def format_message(row, is_group):
    """Format a single message row for display."""
    local_id, local_type, create_time, sender_id, content, source = row

    ts = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S") if create_time else "?"
    type_name = MSG_TYPE_MAP.get(local_type, f"type:{local_type}")

    sender = ""
    body = content or ""

    # message_content may be stored as BLOB (bytes) in some rows
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            body = repr(body)

    if is_group and body and ":\n" in body:
        parts = body.split(":\n", 1)
        sender = parts[0]
        body = parts[1]

    if local_type != 1:
        body = f"[{type_name}] {body[:100]}" if body else f"[{type_name}]"

    if sender:
        return f"[{ts}] {sender}: {body}"
    return f"[{ts}] {body}"


def export_chat(msg_db, username, limit=None):
    """Export messages for a specific conversation."""
    table = username_to_table(username)
    is_group = "@chatroom" in username

    conn = sqlite3.connect(msg_db)
    try:
        # Check table exists
        exists = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()[0]
        if not exists:
            return None, f"No message table found for {username} (expected {table})"

        total = conn.execute(f"SELECT count(*) FROM [{table}]").fetchone()[0]

        query = (
            f"SELECT local_id, local_type, create_time, real_sender_id, "
            f"message_content, source FROM [{table}] ORDER BY create_time ASC"
        )
        if limit:
            # Get last N messages
            query = (
                f"SELECT * FROM (SELECT local_id, local_type, create_time, "
                f"real_sender_id, message_content, source FROM [{table}] "
                f"ORDER BY create_time DESC LIMIT {limit}) ORDER BY create_time ASC"
            )

        rows = conn.execute(query).fetchall()
        lines = [format_message(r, is_group) for r in rows]
        return lines, f"total: {total}, showing: {len(lines)}"
    finally:
        conn.close()


def export_to_file(msg_db, username, output_path, limit=None):
    """Export messages to a text file."""
    lines, info = export_chat(msg_db, username, limit)
    if lines is None:
        return False, info

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"# Chat: {username}\n")
        f.write(f"# {info}\n")
        f.write(f"# Exported: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("\n".join(lines))
        f.write("\n")

    return True, info


def main():
    parser = argparse.ArgumentParser(description="Export WeChat chat messages")
    parser.add_argument(
        "-d",
        "--dir",
        default=DECRYPTED_DIR,
        help=f"Decrypted database directory (default: {DECRYPTED_DIR})",
    )
    parser.add_argument("-c", "--chat", help="Username or chatroom ID to export")
    parser.add_argument("--all", action="store_true", help="Export all conversations")
    parser.add_argument(
        "-n", "--limit", type=int, default=None, help="Number of recent messages"
    )
    parser.add_argument(
        "-o", "--output", default="exported", help="Output directory (default: exported)"
    )
    args = parser.parse_args()

    msg_db = get_msg_db_path()
    if not msg_db or not os.path.isfile(msg_db):
        print(f"[-] Message database not found in {args.dir}/message/")
        print(f"    Run 'python3 decrypt_db.py' first to decrypt databases.")
        sys.exit(1)

    session_db = get_session_db_path()

    if args.chat:
        # Export specific chat
        lines, info = export_chat(msg_db, args.chat, args.limit)
        if lines is None:
            print(f"[-] {info}")
            sys.exit(1)

        print(f"[*] Chat: {args.chat} ({info})\n")
        for line in lines:
            print(line)

        # Also save to file
        safe_name = args.chat.replace("@", "_at_")
        out_path = os.path.join(args.output, f"{safe_name}.txt")
        export_to_file(msg_db, args.chat, out_path, args.limit)
        print(f"\n[*] Saved to {out_path}")

    elif args.all:
        # Export all conversations
        convos = list_conversations(msg_db, session_db)
        os.makedirs(args.output, exist_ok=True)
        exported = 0
        for c in convos:
            if not c["has_msgs"]:
                continue
            safe_name = c["username"].replace("@", "_at_")
            out_path = os.path.join(args.output, f"{safe_name}.txt")
            success, info = export_to_file(msg_db, c["username"], out_path, args.limit)
            if success:
                print(f"  ✅ {c['username']} ({info})")
                exported += 1
        print(f"\n[*] Exported {exported} conversations to {args.output}/")

    else:
        # List conversations
        convos = list_conversations(msg_db, session_db)
        print(f"[*] Found {len(convos)} conversations\n")
        print(f"{'Username':<35} {'Type':<8} {'Time':<18} {'Last Message'}")
        print("-" * 100)
        for c in convos:
            if not c.get("time") and not c["has_msgs"]:
                continue
            marker = "💬" if c.get("type") == "private" else "👥"
            summary = c.get("summary", "")
            time_str = c.get("time", "")
            print(f"{marker} {c['username']:<33} {c.get('type', '?'):<8} {time_str:<18} {summary}")

        print(f"\n[*] To export a chat: python3 export_messages.py -c <username>")
        print(f"[*] To export all:    python3 export_messages.py --all")


if __name__ == "__main__":
    main()
