#!/usr/bin/env python3
"""Translate MICCAI 2025 abstracts and meta-reviews into Simplified Chinese."""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "miccai2025.sqlite"
API_BASE = "http://10.10.70.124:8082"
DEFAULT_MODEL = "Hy-MT2-1.8B-Q8_0.gguf"


def ensure_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(papers)").fetchall()}
    if "abstract_zh" not in columns:
        conn.execute("ALTER TABLE papers ADD COLUMN abstract_zh TEXT")
    if "meta_review_zh" not in columns:
        conn.execute("ALTER TABLE papers ADD COLUMN meta_review_zh TEXT")
    conn.commit()


def translate_text(
    session: requests.Session,
    api_base: str,
    model: str,
    text: str,
    field_label: str,
    timeout: int,
    transport: str,
) -> str:
    if not text.strip():
        return ""
    source_text = normalize_source_text(text)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": f"Translate to Chinese:\n{source_text}"}],
        "temperature": 0,
        "stream": False,
    }
    if transport == "powershell":
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
                "$OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
                "$b64=[Console]::In.ReadToEnd(); "
                "$body=[System.Text.Encoding]::ASCII.GetString([System.Convert]::FromBase64String($b64)); "
                "$bytes=[System.Text.Encoding]::UTF8.GetBytes($body); "
                f"(Invoke-WebRequest -UseBasicParsing -Uri '{api_base.rstrip('/')}/v1/chat/completions' "
                "-Method POST -Body $bytes -ContentType 'application/json; charset=utf-8' "
                f"-TimeoutSec {timeout}).Content"
            ),
        ]
        body_b64 = base64.b64encode(json.dumps(payload, ensure_ascii=True).encode("ascii")).decode("ascii")
        result = subprocess.run(
            command,
            input=body_b64,
            text=True,
            capture_output=True,
            timeout=timeout + 30,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "PowerShell request failed")
        data = json.loads(result.stdout)
    else:
        response = session.post(f"{api_base.rstrip('/')}/v1/chat/completions", json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    return clean_translation(data["choices"][0]["message"]["content"].strip())


def normalize_source_text(text: str) -> str:
    return (
        text.replace("\ufeff", "")
        .replace("\ufffd", "")
        .replace("\x00", "")
        .replace('"', "'")
        .replace("\u201c", "'")
        .replace("\u201d", "'")
    )


def clean_translation(text: str) -> str:
    cleaned = text.strip()
    for prefix in ("Chinese:", "Translation:", "Translate to Chinese:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].lstrip()
    return cleaned

def main() -> None:
    parser = argparse.ArgumentParser(description="Translate abstracts and meta-reviews in data/miccai2025.sqlite.")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--api-base", default=API_BASE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, help="Translate at most N papers.")
    parser.add_argument("--paper-id", help="Translate a single paper id.")
    parser.add_argument("--overwrite", action="store_true", help="Retranslate existing Chinese fields.")
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--transport",
        choices=("powershell", "requests"),
        default="powershell",
        help="HTTP transport. This local API currently works reliably with PowerShell on this machine.",
    )
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"Database not found: {args.db}. Run build_database.py first.")

    session = requests.Session()
    with sqlite3.connect(args.db) as conn:
        conn.row_factory = sqlite3.Row
        ensure_columns(conn)
        where = []
        params: list[object] = []
        if args.paper_id:
            where.append("paper_id = ?")
            params.append(args.paper_id)
        if not args.overwrite:
            where.append(
                "((length(coalesce(abstract,'')) > 0 AND length(coalesce(abstract_zh,'')) = 0) "
                "OR (length(coalesce(meta_review,'')) > 0 AND length(coalesce(meta_review_zh,'')) = 0))"
            )
        sql = "SELECT paper_id, ordinal, title, abstract, abstract_zh, meta_review, meta_review_zh FROM papers"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ordinal"
        if args.limit:
            sql += " LIMIT ?"
            params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
        print(f"Need translation for {len(rows)} papers.", flush=True)

        for index, row in enumerate(rows, start=1):
            updates: dict[str, str] = {}
            try:
                if args.overwrite or (row["abstract"] and not row["abstract_zh"]):
                    updates["abstract_zh"] = translate_text(
                        session, args.api_base, args.model, row["abstract"], "Abstract", args.timeout, args.transport
                    )
                if args.overwrite or (row["meta_review"] and not row["meta_review_zh"]):
                    updates["meta_review_zh"] = translate_text(
                        session, args.api_base, args.model, row["meta_review"], "Meta-Review", args.timeout, args.transport
                    )
                if updates:
                    set_sql = ", ".join(f"{key} = ?" for key in updates)
                    conn.execute(
                        f"UPDATE papers SET {set_sql}, updated_at = CURRENT_TIMESTAMP WHERE paper_id = ?",
                        [*updates.values(), row["paper_id"]],
                    )
                    conn.commit()
                print(f"[{index}/{len(rows)}] translated {row['ordinal']:04d} {row['paper_id']} {row['title'][:80]}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[{index}/{len(rows)}] failed {row['paper_id']}: {exc}", flush=True)
            if args.delay:
                time.sleep(args.delay)


if __name__ == "__main__":
    main()
