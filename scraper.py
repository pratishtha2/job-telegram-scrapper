#!/usr/bin/env python3
"""
Fetch job postings from an RSS feed and send matching roles to Telegram.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# Default: Remotive remote software jobs RSS (override with JOB_RSS_URL)
DEFAULT_RSS_URL = "https://remotive.com/remote-jobs/rss?category=software-dev"

# Case-insensitive keywords used to decide which postings to forward
DEFAULT_KEYWORDS = (
    "sde 2",
    "sde2",
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
    return {line.strip() for line in SEEN_FILE.read_text(encoding="utf-8").splitlines() if line.strip()}


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


def fetch_jobs(rss_url: str) -> list[dict[str, str]]:
    response = requests.get(rss_url, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.content, "xml")
    jobs: list[dict[str, str]] = []

    for item in soup.find_all("item"):
        title = (item.title.get_text(strip=True) if item.title else "").strip()
        link = (item.link.get_text(strip=True) if item.link else "").strip()
        summary = ""
        if item.description:
            summary = item.description.get_text(" ", strip=True)
        elif item.find("content:encoded"):
            summary = item.find("content:encoded").get_text(" ", strip=True)

        company = ""
        # Remotive and similar feeds often put "Title - Company" in the title
        if " - " in title:
            maybe_title, maybe_company = title.rsplit(" - ", 1)
            title, company = maybe_title.strip(), maybe_company.strip()

        if not title or not link:
            continue

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
    return (
        f"New SDE 2 Role: {job['title']} at {job['company']}\n"
        f"{job['link']}"
    )


def main() -> None:
    token = require_env("TELEGRAM_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")
    rss_url = os.environ.get("JOB_RSS_URL", DEFAULT_RSS_URL).strip() or DEFAULT_RSS_URL

    print(f"Fetching jobs from: {rss_url}")
    jobs = fetch_jobs(rss_url)
    print(f"Found {len(jobs)} postings in feed")

    seen = load_seen()
    sent = 0

    for job in jobs:
        if job["id"] in seen:
            continue
        if not matches_keywords(job["title"], job["summary"]):
            seen.add(job["id"])
            continue

        message = format_message(job)
        print(f"Sending: {job['title']} at {job['company']}")
        send_telegram(token, chat_id, message)
        seen.add(job["id"])
        sent += 1

    save_seen(seen)
    print(f"Done. Sent {sent} new matching job(s).")


if __name__ == "__main__":
    main()
