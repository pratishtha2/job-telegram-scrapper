#!/usr/bin/env python3
"""
Fetch job postings from multiple public APIs/RSS feeds and send matches to Telegram.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-telegram-scraper/1.0)",
    "Accept": "application/json, application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

# Public feeds only. LinkedIn / Naukri / Indeed are not included (no public API).
DEFAULT_SOURCES = (
    "https://remotive.com/api/remote-jobs",
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://remoteok.com/api",
    "https://jobicy.com/api/v2/remote-jobs?count=50",
)

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
    "digital nomad",
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
    haystack = f"{location} {title} {summary}".lower()
    if not haystack.strip():
        return False
    return any(
        re.search(r"\b" + re.escape(token) + r"\b", haystack)
        for token in INDIA_FRIENDLY_TOKENS
    )


def source_name(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(":")[0] or url


def html_text(value: str) -> str:
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def job_record(
    *,
    job_id: str,
    title: str,
    company: str,
    link: str,
    summary: str,
    location: str,
    published: str,
    source: str,
) -> dict[str, str]:
    return {
        "id": job_id or link,
        "title": title,
        "company": company or "Unknown",
        "link": link,
        "summary": (summary or "")[:400],
        "location": location or "Unknown",
        "published": published or "",
        "source": source,
    }


def parse_remotive(payload: dict, source: str) -> list[dict[str, str]]:
    jobs: list[dict[str, str]] = []
    for entry in payload.get("jobs", []):
        title = str(entry.get("title", "")).strip()
        link = str(entry.get("url", "")).strip()
        if not title or not link:
            continue
        jobs.append(
            job_record(
                job_id=str(entry.get("id") or link),
                title=title,
                company=str(entry.get("company_name", "")).strip(),
                link=link,
                summary=html_text(str(entry.get("description", ""))),
                location=str(entry.get("candidate_required_location", "")).strip(),
                published=str(entry.get("publication_date", "")).strip(),
                source=source,
            )
        )
    return jobs


def parse_jobicy(payload: dict, source: str) -> list[dict[str, str]]:
    jobs: list[dict[str, str]] = []
    for entry in payload.get("jobs", []):
        title = str(entry.get("jobTitle", "")).strip()
        link = str(entry.get("url", "")).strip()
        if not title or not link:
            continue
        jobs.append(
            job_record(
                job_id=str(entry.get("id") or link),
                title=title,
                company=str(entry.get("companyName", "")).strip(),
                link=link,
                summary=html_text(
                    str(entry.get("jobExcerpt") or entry.get("jobDescription") or "")
                ),
                location=str(entry.get("jobGeo", "")).strip(),
                published=str(entry.get("pubDate", "")).strip(),
                source=source,
            )
        )
    return jobs


def parse_remoteok(payload: list, source: str) -> list[dict[str, str]]:
    jobs: list[dict[str, str]] = []
    for entry in payload:
        if not isinstance(entry, dict) or "position" not in entry:
            continue
        title = str(entry.get("position", "")).strip()
        link = str(entry.get("url") or entry.get("apply_url") or "").strip()
        if not title or not link:
            continue
        tags = " ".join(str(t) for t in (entry.get("tags") or []))
        location = str(entry.get("location") or "").strip()
        if not location:
            location = tags or "Remote"
        jobs.append(
            job_record(
                job_id=str(entry.get("id") or link),
                title=title,
                company=str(entry.get("company", "")).strip(),
                link=link,
                summary=html_text(str(entry.get("description", ""))) + " " + tags,
                location=location,
                published=str(entry.get("date", "")).strip(),
                source=source,
            )
        )
    return jobs


def parse_rss_jobs(content: bytes, source: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(content, "xml")
    jobs: list[dict[str, str]] = []

    for item in soup.find_all("item"):
        raw_title = item.title.get_text(strip=True) if item.title else ""
        link = item.link.get_text(strip=True) if item.link else ""
        if not raw_title or not link:
            continue

        summary = ""
        if item.description:
            summary = html_text(item.description.get_text())

        title, company = raw_title, ""
        if ": " in raw_title:
            maybe_company, maybe_title = raw_title.split(": ", 1)
            company, title = maybe_company.strip(), maybe_title.strip()
        elif " - " in raw_title:
            maybe_title, maybe_company = raw_title.rsplit(" - ", 1)
            title, company = maybe_title.strip(), maybe_company.strip()

        location = item.region.get_text(strip=True) if item.region else ""
        published_raw = item.pubDate.get_text(strip=True) if item.pubDate else ""

        jobs.append(
            job_record(
                job_id=link,
                title=title,
                company=company,
                link=link,
                summary=summary,
                location=location,
                published=published_raw,
                source=source,
            )
        )

    return jobs


def parse_payload(payload: object, content: bytes, source: str) -> list[dict[str, str]]:
    if isinstance(payload, list):
        return parse_remoteok(payload, source)
    if isinstance(payload, dict):
        jobs = payload.get("jobs") or []
        if jobs and isinstance(jobs[0], dict) and "jobTitle" in jobs[0]:
            return parse_jobicy(payload, source)
        if jobs and isinstance(jobs[0], dict) and (
            "company_name" in jobs[0] or "title" in jobs[0]
        ):
            return parse_remotive(payload, source)
    return parse_rss_jobs(content, source)


def fetch_source(source_url: str) -> list[dict[str, str]]:
    source = source_name(source_url)
    response = requests.get(source_url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    payload: object | None = None
    if "json" in content_type:
        payload = response.json()
    return parse_payload(payload, response.content, source)


def source_urls() -> list[str]:
    extra = os.environ.get("JOB_SOURCE_URL", "").strip() or os.environ.get(
        "JOB_RSS_URL", ""
    ).strip()
    if extra:
        return [u.strip() for u in extra.split(",") if u.strip()]
    return list(DEFAULT_SOURCES)


def dedupe_jobs(jobs: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for job in jobs:
        key = job["link"].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


def fetch_all_jobs() -> list[dict[str, str]]:
    collected: list[dict[str, str]] = []
    for url in source_urls():
        print(f"Fetching jobs from: {url}")
        try:
            batch = fetch_source(url)
        except Exception as exc:  # keep other sources running if one feed is down
            print(f"  skipped ({exc})")
            continue
        print(f"  parsed {len(batch)} postings")
        collected.extend(batch)
    return dedupe_jobs(collected)


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
    source = job.get("source") or "unknown"
    return (
        f"New role: {job['title']} at {job['company']}\n"
        f"Source: {source}\n"
        f"Location: {location}\n"
        f"Posted: {published}\n"
        f"{job['link']}"
    )


def main() -> None:
    token = require_env("TELEGRAM_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")
    max_send = int(os.environ.get("MAX_SEND", "10"))

    jobs = fetch_all_jobs()
    print(f"Found {len(jobs)} unique postings across sources")

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

        print(
            f"Sending: {job['title']} at {job['company']} "
            f"[{job.get('location')}] via {job.get('source')}"
        )
        send_telegram(token, chat_id, format_message(job))
        seen.add(job["id"])
        sent += 1

    save_seen(seen)
    print(
        f"Done. Sent {sent}. "
        f"Skipped keyword={skipped_keyword} location={skipped_location} "
        f"older_than_{max_age_days()}d={skipped_old}."
    )


if __name__ == "__main__":
    main()
