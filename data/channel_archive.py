"""Quarantined Telegram archive dataset; never a trading-performance ledger."""
from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

from data.storage import get_connection

TABLE = "channel_archive_messages"
SYMBOLS = ("XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD", "GOLD", "SILVER")


def init_channel_archive(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            archive_sha256 TEXT NOT NULL, message_id TEXT NOT NULL,
            timestamp_text TEXT, text TEXT NOT NULL, tags_json TEXT NOT NULL,
            symbols_json TEXT NOT NULL, linkage_status TEXT NOT NULL DEFAULT 'unlinked',
            linked_signal_id TEXT, linked_position_ticket INTEGER,
            PRIMARY KEY(archive_sha256, message_id))""")
        conn.commit()
    finally:
        conn.close()


def parse_telegram_html(path: str) -> list[dict]:
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8", errors="replace")
    archive_hash = hashlib.sha256(raw).hexdigest()
    starts = list(re.finditer(r'<div class="message[^>]*"[^>]*id="message([^\"]+)"', text))
    rows = []
    for i, match in enumerate(starts):
        block = text[match.start(): starts[i + 1].start() if i + 1 < len(starts) else len(text)]
        date = re.search(r'class="date details"[^>]*title="([^"]+)"', block)
        body = re.search(r'<div class="text">(.*?)</div>', block, re.S)
        if not body:
            continue
        clean = re.sub(r'<br\s*/?>', '\n', body.group(1), flags=re.I)
        clean = html.unescape(re.sub(r'<[^>]+>', '', clean)).strip()
        tags = sorted(set(re.findall(r'#[\w_]+', clean, re.UNICODE)))
        symbols = [s for s in SYMBOLS if re.search(rf'\b{re.escape(s)}\b', clean, re.I)]
        rows.append({"archive_sha256": archive_hash, "message_id": match.group(1),
                     "timestamp_text": html.unescape(date.group(1)) if date else None,
                     "text": clean, "tags": tags, "symbols": symbols})
    return rows


def import_archive(db_path: str, path: str) -> int:
    init_channel_archive(db_path)
    rows = parse_telegram_html(path)
    conn = get_connection(db_path)
    try:
        before = conn.total_changes
        conn.executemany(f"""INSERT OR IGNORE INTO {TABLE}
            (archive_sha256,message_id,timestamp_text,text,tags_json,symbols_json)
            VALUES (?,?,?,?,?,?)""", [(r["archive_sha256"], r["message_id"], r["timestamp_text"],
                r["text"], json.dumps(r["tags"], ensure_ascii=False),
                json.dumps(r["symbols"])) for r in rows])
        conn.commit()
        return conn.total_changes - before
    finally:
        conn.close()
