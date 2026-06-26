#!/usr/bin/env python3
"""Download MICCAI 2025 PDFs listed in the local SQLite database."""

from __future__ import annotations

import argparse
import re
import sqlite3
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "miccai2025.sqlite"
PDF_DIR = ROOT / "pdfs"


def safe_filename(text: str, max_len: int = 120) -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "_", text)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:max_len].rstrip(" .")


def download_one(session: requests.Session, url: str, target: Path, overwrite: bool = False) -> bool:
    if target.exists() and target.stat().st_size > 10_000 and not overwrite:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    with session.get(url, stream=True, timeout=90) as response:
        response.raise_for_status()
        with tmp.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if chunk:
                    handle.write(chunk)
    tmp.replace(target)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Download PDFs from data/miccai2025.sqlite.")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--out", type=Path, default=PDF_DIR)
    parser.add_argument("--limit", type=int, help="Download at most N PDFs.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--delay", type=float, default=0.1)
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"Database not found: {args.db}. Run build_database.py first.")

    session = requests.Session()
    session.headers.update({"User-Agent": "MICCAI2025-pdf-downloader/1.0"})
    with sqlite3.connect(args.db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT paper_id, ordinal, title, pdf_url FROM papers ORDER BY ordinal"
        ).fetchall()
        if args.limit:
            rows = rows[: args.limit]
        for index, row in enumerate(rows, start=1):
            filename = f"{int(row['ordinal']):04d}_{row['paper_id']}_{safe_filename(row['title'])}.pdf"
            target = args.out / filename
            try:
                changed = download_one(session, row["pdf_url"], target, overwrite=args.overwrite)
                conn.execute(
                    "UPDATE papers SET local_pdf_path = ?, downloaded = 1, updated_at = CURRENT_TIMESTAMP WHERE paper_id = ?",
                    (str(target.relative_to(ROOT)), row["paper_id"]),
                )
                conn.commit()
                status = "downloaded" if changed else "exists"
                print(f"[{index}/{len(rows)}] {status}: {target.name}")
            except Exception as exc:  # noqa: BLE001
                print(f"[{index}/{len(rows)}] failed {row['paper_id']}: {exc}")
            if args.delay:
                time.sleep(args.delay)


if __name__ == "__main__":
    main()
