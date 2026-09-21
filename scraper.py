#!/usr/bin/env python3
"""
Fetch job postings from a JSON API or RSS feed and send matching roles to Telegram.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# Remotive's JSON API. Their old /remote-jobs/rss endpoint now returns 403/404.
DEFAULT_SOURCE_URL = "https://remotive.com/api/remote-jobs?category=software-dev"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-telegram-scraper/1.0)",
    "Accept": "application/json, application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

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

# Locations that can include candidates in India.
INDIA_FRIENDLY_TOKENS = (
    "india",
    "indian",
    "worldwide",
    "world wide",
    "anywhere",
    "unrestricted",
    "global",
    "apac",
    "asia-pacific",
    "asia pacific",
    "asia",
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


def max_age_days() -> int:
    return int(os.environ.get("MAX_AGE_DAYS", "14"))


def parse_published(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def is_recent(published: datetime | None) -> bool:
    if published is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days())
    return published >= cutoff


def allows_india(location: str, title: str = "", summary: str = "") -> bool:
    """Keep Worldwide / APAC / India. Drop US-, Europe-, or Americas-only roles."""
    haystack = f"{location} {title} {summary}".lower()
    if not haystack.strip():
        return False
    return any(token in haystack for token in INDIA_FRIENDLY_TOKENS)


def parse_json_jobs(payload: dict) -> list[dict[str, str]]:
    jobs: list[dict[str, str]] = []

    for entry in payload.get("jobs", []):
        title = str(entry.get("title", "")).strip()
        link = str(entry.get("url", "")).strip()
        if not title or not link:
            continue

        summary = BeautifulSoup(entry.get("description", ""), "html.parser").get_text(
            " ", strip=True
        )
        published_raw = str(entry.get("publication_date", "")).strip()

        jobs.append(
            {
                "id": str(entry.get("id") or link),
                "title": title,
                "company": str(entry.get("company_name", "")).strip() or "Unknown",
                "link": link,
                "summary": summary[:400],
                "location": str(entry.get("candidate_required_location", "")).strip()
                or "Unknown",
                "published": published_raw,
            }
        )

    return jobs


def parse_rss_jobs(content: bytes) -> list[dict[str, str]]:
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
        if ": " in raw_title:
            maybe_company, maybe_title = raw_title.split(": ", 1)
            company, title = maybe_company.strip(), maybe_title.strip()
        elif " - " in raw_title:
            maybe_title, maybe_company = raw_title.rsplit(" - ", 1)
            title, company = maybe_title.strip(), maybe_company.strip()

        location = ""
        if item.region:
            location = item.region.get_text(strip=True)
        published_raw = item.pubDate.get_text(strip=True) if item.pubDate else ""

        jobs.append(
            {
                "id": link,
                "title": title,
                "company": company or "Unknown",
                "link": link,
                "summary": summary[:400],
                "location": location or "Unknown",
                "published": published_raw,
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
    published = job.get("published") or "unknown date"
    location = job.get("location") or "Unknown"
    return (
        f"New role: {job['title']} at {job['company']}\n"
        f"Location: {location}\n"
        f"Posted: {published}\n"
        f"{job['link']}"
    )


def is_eligible(job: dict[str, str]) -> bool:
    if not matches_keywords(job["title"], job["summary"]):
        return False
    if not allows_india(job.get("location", ""), job["title"], job["summary"]):
        return False
    published = parse_published(job.get("published", ""))
    if not is_recent(published):
        return False
    return True


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
    skipped_location = 0
    skipped_old = 0
    skipped_keyword = 0

    for job in jobs:
        if job["id"] in seen:
            continue
        if not matches_keywords(job["title"], job["summary"]):
            skipped_keyword += 1
            seen.add(job["id"])
            continue
        if not allows_india(job.get("location", ""), job["title"], job["summary"]):
            skipped_location += 1
            seen.add(job["id"])
            continue
        if not is_recent(parse_published(job.get("published", ""))):
            skipped_old += 1
            seen.add(job["id"])
            continue
        if sent >= max_send:
            continue

        print(f"Sending: {job['title']} at {job['company']} [{job.get('location')}]")
        send_telegram(token, chat_id, format_message(job))
        seen.add(job["id"])
        sent += 1

    save_seen(seen)
    print(
        f"Done. Sent {sent}. "
        f"Skipped keyword={skipped_keyword} location={skipped_location} older_than_{max_age_days()}d={skipped_old}."
    )


if __name__ == "__main__":
    main()
