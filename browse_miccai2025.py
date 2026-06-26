#!/usr/bin/env python3
"""Local browser/API for the MICCAI 2025 SQLite database."""

from __future__ import annotations

import json
import mimetypes
import sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "miccai2025.sqlite"
WEB_DIR = ROOT / "web"


def rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api(parsed.path, parse_qs(parsed.query))
            return
        if parsed.path.startswith("/pdfs/"):
            local = (ROOT / unquote(parsed.path.lstrip("/"))).resolve()
            if ROOT not in local.parents:
                self.send_error(403)
                return
            self.send_file(local)
            return
        path = WEB_DIR / ("index.html" if parsed.path in ("/", "/index.html") else parsed.path.lstrip("/"))
        self.send_file(path)

    def handle_api(self, path: str, query: dict[str, list[str]]) -> None:
        if not DB_PATH.exists():
            self.send_json({"error": f"Database not found: {DB_PATH}"}, status=500)
            return
        with connect() as conn:
            if path == "/api/papers":
                q = query.get("q", [""])[0].strip()
                category = query.get("category", [""])[0].strip()
                volume = query.get("volume", [""])[0].strip()
                limit = min(int(query.get("limit", ["100"])[0]), 500)
                offset = max(int(query.get("offset", ["0"])[0]), 0)
                clauses: list[str] = []
                params: list[object] = []
                if q:
                    clauses.append(
                        "("
                        "title LIKE ? OR authors_text LIKE ? OR abstract LIKE ? OR abstract_zh LIKE ? "
                        "OR meta_review LIKE ? OR meta_review_zh LIKE ? OR reviews_text LIKE ? OR categories LIKE ?"
                        ")"
                    )
                    like = f"%{q}%"
                    params.extend([like, like, like, like, like, like, like, like])
                if category:
                    clauses.append("categories LIKE ?")
                    params.append(f"%{category}%")
                if volume:
                    clauses.append("volume = ?")
                    params.append(volume)
                where = "WHERE " + " AND ".join(clauses) if clauses else ""
                total = conn.execute(f"SELECT COUNT(*) FROM papers {where}", params).fetchone()[0]
                rows = rows_to_dicts(
                    conn.execute(
                        f"""
                        SELECT id, ordinal, paper_id, title, authors_text, abstract, abstract_zh, pdf_url, info_url,
                               doi_url, code_urls, dataset_urls, categories, volume, pages,
                               local_pdf_path, downloaded
                        FROM papers {where}
                        ORDER BY ordinal LIMIT ? OFFSET ?
                        """,
                        [*params, limit, offset],
                    )
                )
                self.send_json({"total": total, "rows": rows})
                return
            if path == "/api/paper":
                paper_id = query.get("paper_id", [""])[0]
                row = conn.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
                self.send_json(dict(row) if row else {"error": "not found"}, status=200 if row else 404)
                return
            if path == "/api/facets":
                self.send_json(
                    {
                        "volumes": rows_to_dicts(conn.execute("SELECT volume, COUNT(*) AS count FROM papers GROUP BY volume ORDER BY volume")),
                        "categories": rows_to_dicts(
                            conn.execute(
                                """
                                SELECT c.name, COUNT(*) AS count
                                FROM categories c JOIN paper_categories pc ON pc.category_id = c.id
                                GROUP BY c.name ORDER BY count DESC, c.name
                                """
                            )
                        ),
                    }
                )
                return
            if path == "/api/stats":
                top_words = rows_to_dicts(
                    conn.execute(
                        """
                        WITH RECURSIVE words(word, rest) AS (
                          SELECT '', lower(group_concat(title, ' ')) || ' ' FROM papers
                          UNION ALL
                          SELECT substr(rest, 0, instr(rest, ' ')), substr(rest, instr(rest, ' ') + 1)
                          FROM words WHERE rest <> ''
                        )
                        SELECT word, COUNT(*) AS count FROM words
                        WHERE length(word) > 4
                          AND word NOT IN ('using','based','model','models','medical','image','images','learning','framework')
                        GROUP BY word ORDER BY count DESC LIMIT 40
                        """
                    )
                )
                self.send_json(
                    {
                        "totals": dict(conn.execute("SELECT COUNT(*) AS papers, SUM(downloaded) AS downloaded FROM papers").fetchone()),
                        "byVolume": rows_to_dicts(conn.execute("SELECT volume, COUNT(*) AS count FROM papers GROUP BY volume ORDER BY volume")),
                        "topCategories": rows_to_dicts(
                            conn.execute(
                                """
                                SELECT c.name, COUNT(*) AS count
                                FROM categories c JOIN paper_categories pc ON pc.category_id = c.id
                                GROUP BY c.name ORDER BY count DESC, c.name LIMIT 30
                                """
                            )
                        ),
                        "topWords": top_words,
                    }
                )
                return
        self.send_json({"error": "unknown endpoint"}, status=404)


def main() -> None:
    import argparse
    import webbrowser

    parser = argparse.ArgumentParser(description="Browse MICCAI 2025 SQLite database.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"MICCAI 2025 browser running at {url}")
    if not args.no_open:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
