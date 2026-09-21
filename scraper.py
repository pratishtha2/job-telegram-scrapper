#!/usr/bin/env python3
"""
Fetch job postings from a JSON API or RSS feed and send matching roles to Telegram.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# Remotive's JSON API. Their old /remote-jobs/rss endpoint now returns 403/404.
DEFAULT_SOURCE_URL = "https://remotive.com/api/remote-jobs?category=software-dev"

# Some job boards reject the default python-requests user agent.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-telegram-scraper/1.0)",
    "Accept": "application/json, application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

# Case-insensitive keywords used to decide which postings to forward
DEFAULT_KEYWORDS = (
    "sde 2",
    "sde2",
    "sde ii",
    "software engineer",
    "software developer",
    "backend engineer",
    "full stack",
    "fullstack",
)

SEEN_FILE = Path(__file__).resolve().parent / ".seen_jobs.txt"


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return value


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    return {
        line.strip()
        for line in SEEN_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text("\n".join(sorted(seen)) + "\n", encoding="utf-8")


def keywords() -> tuple[str, ...]:
    raw = os.environ.get("JOB_KEYWORDS", "").strip()
    if not raw:
        return DEFAULT_KEYWORDS
    return tuple(k.strip().lower() for k in raw.split(",") if k.strip())


def matches_keywords(title: str, summary: str = "") -> bool:
    haystack = f"{title} {summary}".lower()
    return any(keyword in haystack for keyword in keywords())


def parse_json_jobs(payload: dict) -> list[dict[str, str]]:
    """Parse a Remotive-style JSON response."""
    jobs: list[dict[str, str]] = []

    for entry in payload.get("jobs", []):
        title = str(entry.get("title", "")).strip()
        link = str(entry.get("url", "")).strip()
        if not title or not link:
            continue

        summary = BeautifulSoup(entry.get("description", ""), "html.parser").get_text(
            " ", strip=True
        )

        jobs.append(
            {
                "id": str(entry.get("id") or link),
                "title": title,
                "company": str(entry.get("company_name", "")).strip() or "Unknown",
                "link": link,
                "summary": summary[:280],
            }
        )

    return jobs


def parse_rss_jobs(content: bytes) -> list[dict[str, str]]:
    """Parse a generic RSS feed. Handles 'Company: Role' and 'Role - Company' titles."""
    soup = BeautifulSoup(content, "xml")
    jobs: list[dict[str, str]] = []

    for item in soup.find_all("item"):
        raw_title = item.title.get_text(strip=True) if item.title else ""
        link = item.link.get_text(strip=True) if item.link else ""
        if not raw_title or not link:
            continue

        summary = ""
        if item.description:
            summary = BeautifulSoup(
                item.description.get_text(), "html.parser"
            ).get_text(" ", strip=True)

        title, company = raw_title, ""
        # WeWorkRemotely uses "Company: Role"
        if ": " in raw_title:
            maybe_company, maybe_title = raw_title.split(": ", 1)
            company, title = maybe_company.strip(), maybe_title.strip()
        # Other feeds use "Role - Company"
        elif " - " in raw_title:
            maybe_title, maybe_company = raw_title.rsplit(" - ", 1)
            title, company = maybe_title.strip(), maybe_company.strip()

        jobs.append(
            {
                "id": link,
                "title": title,
                "company": company or "Unknown",
                "link": link,
                "summary": summary[:280],
            }
        )

    return jobs


def fetch_jobs(source_url: str) -> list[dict[str, str]]:
    response = requests.get(source_url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "json" in content_type:
        return parse_json_jobs(response.json())
    return parse_rss_jobs(response.content)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = (
        f"https://api.telegram.org/bot{token}/sendMessage"
        f"?chat_id={quote(chat_id)}&text={quote(text)}"
    )
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")


def format_message(job: dict[str, str]) -> str:
    return f"New SDE 2 Role: {job['title']} at {job['company']}\n{job['link']}"


def main() -> None:
    token = require_env("TELEGRAM_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")
    source_url = (
        os.environ.get("JOB_SOURCE_URL", "").strip()
        or os.environ.get("JOB_RSS_URL", "").strip()
        or DEFAULT_SOURCE_URL
    )
    max_send = int(os.environ.get("MAX_SEND", "10"))

    print(f"Fetching jobs from: {source_url}")
    jobs = fetch_jobs(source_url)
    print(f"Found {len(jobs)} postings in feed")

    seen = load_seen()
    sent = 0

    for job in jobs:
        if job["id"] in seen:
            continue
        if not matches_keywords(job["title"], job["summary"]):
            seen.add(job["id"])
            continue
        if sent >= max_send:
            continue

        print(f"Sending: {job['title']} at {job['company']}")
        send_telegram(token, chat_id, format_message(job))
        seen.add(job["id"])
        sent += 1

    save_seen(seen)
    print(f"Done. Sent {sent} new matching job(s).")


if __name__ == "__main__":
    main()
