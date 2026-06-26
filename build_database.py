#!/usr/bin/env python3
"""Build a local SQLite database for MICCAI 2025 open-access papers."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from lxml import html


BASE_URL = "https://papers.miccai.org/miccai-2025/"
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / "cache" / "html"
DB_PATH = DATA_DIR / "miccai2025.sqlite"


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def fetch_text(session: requests.Session, url: str, cache_path: Path, refresh: bool = False) -> str:
    if cache_path.exists() and not refresh:
        return cache_path.read_text(encoding="utf-8")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    response = session.get(url, timeout=45)
    response.raise_for_status()
    response.encoding = "utf-8"
    text = response.text
    cache_path.write_text(text, encoding="utf-8")
    return text


def section_after(root: html.HtmlElement, heading_id: str, stop_tags: tuple[str, ...] = ("h1",)) -> list[html.HtmlElement]:
    heading = root.xpath(f'//*[@id="{heading_id}"]')
    if not heading:
        return []
    nodes: list[html.HtmlElement] = []
    for sibling in heading[0].itersiblings():
        if sibling.tag in stop_tags:
            break
        nodes.append(sibling)
    return nodes


def section_text(root: html.HtmlElement, heading_id: str, stop_tags: tuple[str, ...] = ("h1",)) -> str:
    return "\n\n".join(clean_text(node.text_content()) for node in section_after(root, heading_id, stop_tags) if clean_text(node.text_content()))


def links_in(nodes: Iterable[html.HtmlElement]) -> list[str]:
    found: list[str] = []
    for node in nodes:
        for href in node.xpath(".//a/@href"):
            if href and href not in found:
                found.append(href)
    return found


def parse_bibtex_value(bibtex: str, key: str) -> str:
    match = re.search(rf"{re.escape(key)}\s*=\s*\{{\s*(.*?)\s*\}}\s*,?", bibtex, flags=re.I | re.S)
    if not match:
        return ""
    value = match.group(1)
    if key.lower() == "title":
        value = re.sub(r"^\{\s*|\s*\}$", "", value.strip())
    return clean_text(value)


def parse_main_page(page: str) -> list[dict]:
    root = html.fromstring(page)
    papers: list[dict] = []
    paper_items = root.xpath('//li[.//pre and .//a[normalize-space(.)="PDF"] and not(.//li//pre)]')
    for idx, li in enumerate(paper_items, start=1):
        links = li.xpath('.//a[normalize-space(.)="PDF"]/@href')
        info_links = li.xpath('.//a[contains(normalize-space(.), "Paper Information")]/@href')
        pre_nodes = li.xpath(".//pre")
        if not links or not info_links or not pre_nodes:
            continue
        bibtex = clean_text(pre_nodes[0].text_content())
        title = parse_bibtex_value(bibtex, "title")
        authors_text = parse_bibtex_value(bibtex, "author")
        authors = [clean_text(part) for part in authors_text.split(" AND ") if clean_text(part)]
        info_url = urljoin(BASE_URL, info_links[0])
        pdf_url = urljoin(BASE_URL, links[0])
        paper_id_match = re.search(r"/paper/([^/_]+)_paper\.pdf", pdf_url)
        paper_id = paper_id_match.group(1) if paper_id_match else f"{idx:04d}"
        papers.append(
            {
                "ordinal": idx,
                "paper_id": paper_id,
                "title": title,
                "authors": authors,
                "authors_text": "; ".join(authors),
                "pdf_url": pdf_url,
                "info_url": info_url,
                "bibtex": bibtex,
                "booktitle": parse_bibtex_value(bibtex, "booktitle"),
                "year": parse_bibtex_value(bibtex, "year"),
                "publisher": parse_bibtex_value(bibtex, "publisher"),
                "volume": parse_bibtex_value(bibtex, "volume"),
                "month": parse_bibtex_value(bibtex, "month"),
                "pages": parse_bibtex_value(bibtex, "pages"),
            }
        )
    return papers


def parse_detail_page(page: str) -> dict:
    root = html.fromstring(page)
    link_nodes = section_after(root, "link-id")
    code_nodes = section_after(root, "code-id")
    dataset_nodes = section_after(root, "dataset-id")
    categories = [
        clean_text(a.text_content())
        for a in root.xpath('//a[contains(@href, "/miccai-2025/categories#")]')
        if clean_text(a.text_content())
    ]
    link_text = section_text(root, "link-id")
    supplementary = ""
    for line in link_text.split("\n\n"):
        if line.lower().startswith("supplementary material:"):
            supplementary = clean_text(line.split(":", 1)[1])
    doi_urls = [u for u in links_in(link_nodes) if "doi.org" in u]
    sharedit_urls = [u for u in links_in(link_nodes) if "rdcu.be" in u]
    return {
        "abstract": section_text(root, "abstract-id"),
        "links_text": link_text,
        "sharedit_url": sharedit_urls[0] if sharedit_urls else "",
        "doi_url": doi_urls[0] if doi_urls else "",
        "supplementary": supplementary,
        "code_urls": links_in(code_nodes),
        "dataset_urls": links_in(dataset_nodes),
        "categories": categories,
        "reviews_text": section_text(root, "review-id"),
        "author_feedback": section_text(root, "authorFeedback-id"),
        "meta_review": section_text(root, "metareview-id"),
    }


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS papers (
  id INTEGER PRIMARY KEY,
  ordinal INTEGER NOT NULL,
  paper_id TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  authors_text TEXT,
  abstract TEXT,
  abstract_zh TEXT,
  pdf_url TEXT,
  info_url TEXT,
  sharedit_url TEXT,
  doi_url TEXT,
  supplementary TEXT,
  code_urls TEXT,
  dataset_urls TEXT,
  categories TEXT,
  links_text TEXT,
  bibtex TEXT,
  booktitle TEXT,
  year TEXT,
  publisher TEXT,
  volume TEXT,
  month TEXT,
  pages TEXT,
  reviews_text TEXT,
  author_feedback TEXT,
  meta_review TEXT,
  meta_review_zh TEXT,
  local_pdf_path TEXT,
  downloaded INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS authors (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_authors (
  paper_id TEXT NOT NULL,
  author_id INTEGER NOT NULL,
  author_order INTEGER NOT NULL,
  PRIMARY KEY (paper_id, author_id),
  FOREIGN KEY (author_id) REFERENCES authors(id)
);
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_categories (
  paper_id TEXT NOT NULL,
  category_id INTEGER NOT NULL,
  PRIMARY KEY (paper_id, category_id),
  FOREIGN KEY (category_id) REFERENCES categories(id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
  paper_id UNINDEXED, title, authors_text, abstract, categories
);
"""


def recreate_database(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS papers_fts;
        DROP TABLE IF EXISTS paper_categories;
        DROP TABLE IF EXISTS categories;
        DROP TABLE IF EXISTS paper_authors;
        DROP TABLE IF EXISTS authors;
        DROP TABLE IF EXISTS papers;
        """
    )
    conn.executescript(SCHEMA)


def upsert_lookup(conn: sqlite3.Connection, table: str, name: str) -> int:
    conn.execute(f"INSERT OR IGNORE INTO {table}(name) VALUES (?)", (name,))
    return int(conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()[0])


def save_papers(conn: sqlite3.Connection, papers: list[dict]) -> None:
    for paper in papers:
        conn.execute(
            """
            INSERT INTO papers (
              ordinal, paper_id, title, authors_text, abstract, pdf_url, info_url,
              sharedit_url, doi_url, supplementary, code_urls, dataset_urls, categories,
              links_text, bibtex, booktitle, year, publisher, volume, month, pages,
              reviews_text, author_feedback, meta_review, updated_at
            ) VALUES (
              :ordinal, :paper_id, :title, :authors_text, :abstract, :pdf_url, :info_url,
              :sharedit_url, :doi_url, :supplementary, :code_urls, :dataset_urls, :categories,
              :links_text, :bibtex, :booktitle, :year, :publisher, :volume, :month, :pages,
              :reviews_text, :author_feedback, :meta_review, CURRENT_TIMESTAMP
            )
            ON CONFLICT(paper_id) DO UPDATE SET
              title=excluded.title,
              authors_text=excluded.authors_text,
              abstract=excluded.abstract,
              pdf_url=excluded.pdf_url,
              info_url=excluded.info_url,
              sharedit_url=excluded.sharedit_url,
              doi_url=excluded.doi_url,
              supplementary=excluded.supplementary,
              code_urls=excluded.code_urls,
              dataset_urls=excluded.dataset_urls,
              categories=excluded.categories,
              links_text=excluded.links_text,
              bibtex=excluded.bibtex,
              booktitle=excluded.booktitle,
              year=excluded.year,
              publisher=excluded.publisher,
              volume=excluded.volume,
              month=excluded.month,
              pages=excluded.pages,
              reviews_text=excluded.reviews_text,
              author_feedback=excluded.author_feedback,
              meta_review=excluded.meta_review,
              updated_at=CURRENT_TIMESTAMP
            """,
            paper,
        )
        conn.execute("DELETE FROM paper_authors WHERE paper_id = ?", (paper["paper_id"],))
        for order, author in enumerate(paper["authors"], start=1):
            author_id = upsert_lookup(conn, "authors", author)
            conn.execute(
                "INSERT OR IGNORE INTO paper_authors(paper_id, author_id, author_order) VALUES (?, ?, ?)",
                (paper["paper_id"], author_id, order),
            )
        conn.execute("DELETE FROM paper_categories WHERE paper_id = ?", (paper["paper_id"],))
        for category in paper["category_list"]:
            category_id = upsert_lookup(conn, "categories", category)
            conn.execute(
                "INSERT OR IGNORE INTO paper_categories(paper_id, category_id) VALUES (?, ?)",
                (paper["paper_id"], category_id),
            )
        conn.execute("DELETE FROM papers_fts WHERE paper_id = ?", (paper["paper_id"],))
        conn.execute(
            "INSERT INTO papers_fts(paper_id, title, authors_text, abstract, categories) VALUES (?, ?, ?, ?, ?)",
            (paper["paper_id"], paper["title"], paper["authors_text"], paper["abstract"], paper["categories"]),
        )
    conn.commit()


def build_database(refresh: bool = False, limit: int | None = None, delay: float = 0.05) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "MICCAI2025-local-database-builder/1.0"})
    main_html = fetch_text(session, BASE_URL, CACHE_DIR / "index.html", refresh=refresh)
    papers = parse_main_page(main_html)
    if limit:
        papers = papers[:limit]
    print(f"Found {len(papers)} papers on main page.")
    for idx, paper in enumerate(papers, start=1):
        cache_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", paper["info_url"].rstrip("/").split("/")[-1])
        detail_html = fetch_text(session, paper["info_url"], CACHE_DIR / cache_name, refresh=refresh)
        detail = parse_detail_page(detail_html)
        paper.update(detail)
        paper["code_urls"] = json.dumps(detail["code_urls"], ensure_ascii=False)
        paper["dataset_urls"] = json.dumps(detail["dataset_urls"], ensure_ascii=False)
        paper["category_list"] = detail["categories"]
        paper["categories"] = json.dumps(detail["categories"], ensure_ascii=False)
        if idx % 25 == 0 or idx == len(papers):
            print(f"Parsed {idx}/{len(papers)}")
        if delay:
            time.sleep(delay)
    with sqlite3.connect(DB_PATH) as conn:
        recreate_database(conn)
        save_papers(conn, papers)
        count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        authors = conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
        categories = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    print(f"Saved {count} papers, {authors} authors, {categories} categories to {DB_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build MICCAI 2025 paper SQLite database.")
    parser.add_argument("--refresh", action="store_true", help="Ignore cached HTML and re-download pages.")
    parser.add_argument("--limit", type=int, help="Only parse the first N papers, useful for testing.")
    parser.add_argument("--delay", type=float, default=0.05, help="Delay between detail-page requests.")
    args = parser.parse_args()
    build_database(refresh=args.refresh, limit=args.limit, delay=args.delay)


if __name__ == "__main__":
    main()
