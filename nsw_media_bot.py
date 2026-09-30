import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK"]

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "seen_releases.json"
LOG_FILE = BASE_DIR / "bot_log.txt"

SOURCES = [
    {
        "name": "NSW Government central ministerial releases",
        "url": "https://www.nsw.gov.au/ministerial-releases",
    },
    {
        "name": "DCIThS ministerial media releases",
        "url": "https://www.nsw.gov.au/departments-and-agencies/dciths/ministerial-media-releases",
    },
]

RELEASE_URL_MARKERS = (
    "/ministerial-releases/",
    "/ministerial-media-releases/",
)

REQUEST_HEADERS = {
    "User-Agent": "NSW-media-monitor/1.0"
}


def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{timestamp} - {message}"

    with open(LOG_FILE, "a", encoding="utf-8") as file:
        file.write(line + "\n")

    print(line)


def clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def build_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=options)


def get_releases():
    driver = build_driver()
    all_releases = {}

    try:
        for source in SOURCES:
            log(f"Opening source: {source['name']}")

            driver.get(source["url"])

            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        "a[href*='/ministerial-releases/'], "
                        "a[href*='/ministerial-media-releases/']"
                    )
                )
            )

            time.sleep(2)

            links = driver.find_elements(
                By.CSS_SELECTOR,
                "a[href*='/ministerial-releases/'], "
                "a[href*='/ministerial-media-releases/']"
            )

            source_count = 0

            for link in links:
                href = link.get_attribute("href")
                title = clean_text(link.text)

                if not href or not title:
                    continue

                if not any(marker in href for marker in RELEASE_URL_MARKERS):
                    continue

                if href.rstrip("/") == source["url"].rstrip("/"):
                    continue

                all_releases[href] = {
                    "title": title,
                    "url": href,
                    "source": source["name"],
                }
                source_count += 1

            log(f"Found {source_count} candidate release links from this source.")

        releases = list(all_releases.values())
        log(f"Found {len(releases)} unique release links across all sources.")

        if not releases:
            raise RuntimeError(
                "No ministerial release links were found after the source pages rendered."
            )

        return releases

    finally:
        driver.quit()


def extract_label_value(soup, label_text):
    label_text_lower = label_text.lower()

    for element in soup.find_all(["div", "p", "dt", "span", "strong"]):
        text = clean_text(element.get_text(" ", strip=True))

        if text.lower() == label_text_lower:
            sibling = element.find_next_sibling()

            while sibling:
                value = clean_text(sibling.get_text(" ", strip=True))

                if value:
                    return value

                sibling = sibling.find_next_sibling()

        if text.lower().startswith(label_text_lower + ":"):
            value = clean_text(text[len(label_text) + 1:])

            if value:
                return value

    return ""


def extract_from_page_text(soup, label, next_labels):
    lines = [
        clean_text(line)
        for line in soup.get_text("\n", strip=True).splitlines()
        if clean_text(line)
    ]

    next_labels_lower = [item.lower() for item in next_labels]

    for index, line in enumerate(lines):
        normalised = line.rstrip(":").strip().lower()

        if normalised == label.lower():
            for next_line in lines[index + 1:]:
                next_normalised = next_line.rstrip(":").strip().lower()

                if next_normalised in next_labels_lower:
                    break

                if next_line.lower() in {"listen", "share", "print"}:
                    break

                if next_line:
                    return clean_text(next_line)

    return ""


def get_release_details(release):
    log(f"Reading release details: {release['title']}")

    response = requests.get(
        release["url"],
        timeout=20,
        headers=REQUEST_HEADERS,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    title_element = soup.find("h1")
    if title_element:
        release["title"] = clean_text(title_element.get_text(" ", strip=True))

    published = extract_label_value(soup, "Published")
    released_by = extract_label_value(soup, "Released by")

    if not published:
        published = extract_from_page_text(
            soup,
            "Published",
            ["Released by", "Statement by", "Listen"],
        )

    if not released_by:
        released_by = extract_from_page_text(
            soup,
            "Released by",
            ["Listen", "Share"],
        )

    summary = ""

    meta_description = soup.find(
        "meta",
        attrs={"name": "description"},
    )

    if meta_description and meta_description.get("content"):
        summary = clean_text(meta_description["content"])

    if not summary:
        h1 = soup.find("h1")

        if h1:
            for paragraph in h1.find_all_next("p"):
                candidate = clean_text(paragraph.get_text(" ", strip=True))

                if not candidate:
                    continue

                if candidate.lower().startswith("published"):
                    continue

                if candidate.lower().startswith("released by"):
                    continue

                if len(candidate) >= 40:
                    summary = candidate
                    break

    if len(summary) > 450:
        summary = summary[:447].rstrip() + "..."

    release["published"] = published or "Date not found"
    release["released_by"] = released_by or "Minister details not found"
    release["summary"] = summary or "No summary available"

    return release


def load_seen():
    if not STATE_FILE.exists():
        return set()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return set(json.load(file))

    except (json.JSONDecodeError, OSError) as error:
        log(f"WARNING: Could not read seen_releases.json: {error}")
        return set()


def save_seen(seen):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(sorted(seen), file, indent=2)


def send_to_slack(release):
    log(f"Sending to Slack: {release['title']}")

    payload = {
        "text": f"NSW Govt release: {release['title']}",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "NSW GOVERNMENT MEDIA RELEASE",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{release['title']}*",
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Published:*\n{release['published']}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Released by:*\n{release['released_by']}",
                    },
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": release["summary"],
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Open media release",
                            "emoji": True,
                        },
                        "url": release["url"],
                        "action_id": "open_media_release",
                    }
                ],
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Source: {release['source']}",
                    }
                ],
            },
        ],
    }

    response = requests.post(
        SLACK_WEBHOOK,
        json=payload,
        timeout=20,
    )
    response.raise_for_status()


def main():
    log("Bot run started.")

    releases = get_releases()
    seen = load_seen()

    if not seen:
        current_urls = {release["url"] for release in releases}
        save_seen(current_urls)
        log(f"Initialised with {len(current_urls)} existing releases.")
        log("Bot run completed successfully.")
        return

    new_releases = [
        release
        for release in releases
        if release["url"] not in seen
    ]

    log(f"Found {len(new_releases)} new releases.")

    for release in reversed(new_releases):
        try:
            detailed_release = get_release_details(release)
            send_to_slack(detailed_release)

            seen.add(release["url"])
            save_seen(seen)

        except Exception as error:
            log(f"ERROR processing release {release['url']}: {error}")
            log(traceback.format_exc())
            log("Release left unseen so it will be retried next run.")

    log("Bot run completed successfully.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("ERROR:")
        log(traceback.format_exc())
        sys.exit(1)
