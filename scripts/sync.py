"""
Sync script (problems repo): pull submissions from Google Sheets, append into
problems/accepted|errors/<slug>/, and write sync-batch.json for the users repo.

sync-batch.json maps "sheetNumber-rowIndexInGetResponse" -> {url, index}.
It is overwritten every run (not appended).

Environment variables:
    SHEET_URLS        - comma-separated Apps Script GET endpoint URLs
    STATUS_SHEET_URL  - Apps Script POST endpoint for the status logger
    DEFAULT_LIMIT     - limit for new problem config.json (default 100)
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone
from collections import defaultdict

REPO_NAME = "problems"
SYNC_BATCH_PATH = "sync-batch.json"


def get_sheet_urls():
    raw = os.environ.get("SHEET_URLS", "")
    urls = [u.strip() for u in raw.split(",") if u.strip()]
    if not urls:
        print("ERROR: No sheet URLs found in SHEET_URLS env var.", file=sys.stderr)
        sys.exit(1)
    return urls


def fetch_sheet_rows(url):
    for attempt in (1, 2):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  Attempt {attempt} failed for {url}: {e}", file=sys.stderr)
            if attempt == 2:
                print("  Skipping this sheet after 2 failed attempts.", file=sys.stderr)
                return []


def extract_json_string(row):
    if isinstance(row, str):
        return row
    if isinstance(row, dict):
        for key in ("data", "value", "json", "submission"):
            if key in row:
                return row[key]
        return next(iter(row.values()), None)
    if isinstance(row, (list, tuple)) and row:
        return row[0]
    return None


def parse_submission(raw_json_string):
    if not raw_json_string:
        return None
    try:
        submission = json.loads(raw_json_string)
    except (json.JSONDecodeError, TypeError) as e:
        print(f"  Skipping row - could not parse JSON: {e}", file=sys.stderr)
        return None

    required_fields = ("slug", "code", "timestamp", "email", "language", "status")
    missing = [f for f in required_fields if f not in submission]
    if missing:
        print(f"  Skipping row - missing fields {missing}", file=sys.stderr)
        return None

    return submission


def fetch_all_submissions(sheet_urls):
    """
    Fetch every sheet in URL order. sheet_num is 1-based from that order.
    row_index is 0-based index in the GET response array (not spreadsheet row).

    Only valid parsed submissions are returned; each has sheet_key "N-I".
    Invalid response slots get no sheet_key entry (users will skip those keys).
    """
    all_submissions = []
    for sheet_num, url in enumerate(sheet_urls, start=1):
        print(f"Fetching sheet {sheet_num}: {url[:40]}...")
        rows = fetch_sheet_rows(url)
        if not isinstance(rows, list):
            print(f"  Unexpected response type, skipping sheet.", file=sys.stderr)
            continue
        print(f"  Got {len(rows)} raw rows")
        for row_index, row in enumerate(rows):
            submission = parse_submission(extract_json_string(row))
            if not submission:
                continue
            submission["sheet_key"] = f"{sheet_num}-{row_index}"
            all_submissions.append(submission)
    return all_submissions


def group_by_problem(submissions):
    grouped = defaultdict(list)
    for submission in submissions:
        grouped[submission["slug"]].append(submission)

    for slug, subs in grouped.items():
        subs.sort(key=lambda s: int(s["timestamp"]))

    return grouped


def is_accepted(submission):
    return str(submission.get("status", "")).strip().lower() == "accepted"


def split_accepted_errors(submissions):
    accepted, errors = [], []
    for s in submissions:
        (accepted if is_accepted(s) else errors).append(s)
    return accepted, errors


def to_stored_format(submission):
    return {
        "email": submission["email"],
        "timestamp": int(submission["timestamp"]),
        "language": submission["language"],
        "status": submission["status"],
        "code": submission["code"],
    }


def get_default_limit():
    # Empty secret values become "" in Actions and must not crash int().
    raw = os.environ.get("DEFAULT_LIMIT") or "100"
    return int(raw)


def load_config(config_path):
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return json.load(f)
    return {"current": 1, "limit": get_default_limit()}


def save_config(config_path, config):
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)


def load_data_file(file_path):
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            return json.load(f)
    return []


def save_data_file(file_path, submissions):
    with open(file_path, "w") as f:
        json.dump(submissions, f, indent=2)


def append_submissions_to_problem(problem_dir, submissions, batch):
    """
    Append submissions (oldest-first) into data/<n>.json, updating config.
    For each written submission, record batch[sheet_key] = {url, index}.
    """
    if not submissions:
        return

    data_dir = os.path.join(problem_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    config_path = os.path.join(problem_dir, "config.json")
    config = load_config(config_path)

    current = int(config["current"]) if config.get("current") not in (None, "") else 1
    limit = int(config["limit"]) if config.get("limit") not in (None, "") else get_default_limit()

    remaining = list(submissions)
    while remaining:
        file_path = os.path.join(data_dir, f"{current}.json")
        existing = load_data_file(file_path)
        space = limit - len(existing)

        if space <= 0:
            current += 1
            continue

        chunk = remaining[:space]
        remaining = remaining[space:]
        start_index = len(existing)

        for offset, submission in enumerate(chunk):
            existing.append(to_stored_format(submission))
            batch[submission["sheet_key"]] = {
                "url": f"{current}.json",
                "index": start_index + offset,
            }

        save_data_file(file_path, existing)

        if remaining:
            current += 1

    config["current"] = current
    config["limit"] = limit
    save_config(config_path, config)


def write_all_submissions(grouped, batch, base_dir="problems"):
    for slug, submissions in grouped.items():
        accepted, errors = split_accepted_errors(submissions)

        if accepted:
            accepted_dir = os.path.join(base_dir, "accepted", slug)
            append_submissions_to_problem(accepted_dir, accepted, batch)
            print(f"  [{slug}] appended {len(accepted)} accepted submission(s)")

        if errors:
            errors_dir = os.path.join(base_dir, "errors", slug)
            append_submissions_to_problem(errors_dir, errors, batch)
            print(f"  [{slug}] appended {len(errors)} error submission(s)")


def save_sync_batch(batch, path=SYNC_BATCH_PATH):
    """Overwrite the batch file every run (empty object if nothing synced)."""
    with open(path, "w") as f:
        json.dump(batch, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"Wrote {path} with {len(batch)} entr(ies)")


def get_today_date_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def log_sync_status():
    status_url = os.environ.get("STATUS_SHEET_URL")
    if not status_url:
        print("STATUS_SHEET_URL not set - skipping status log.", file=sys.stderr)
        return

    payload = {"date": get_today_date_str(), "repo": REPO_NAME}
    try:
        resp = requests.post(status_url, json=payload, timeout=30)
        resp.raise_for_status()
        print(f"Logged sync status: repo={REPO_NAME}, date={payload['date']}")
    except requests.RequestException as e:
        print(f"WARNING: failed to log sync status: {e}", file=sys.stderr)


def main():
    sheet_urls = get_sheet_urls()
    print(f"Found {len(sheet_urls)} sheet URL(s) to sync.\n")

    submissions = fetch_all_submissions(sheet_urls)
    print(f"\nTotal valid submissions collected: {len(submissions)}")

    grouped = group_by_problem(submissions)
    print(f"Grouped into {len(grouped)} distinct problem(s):")
    for slug, subs in grouped.items():
        print(
            f"  - {slug}: {len(subs)} submission(s), "
            f"oldest={subs[0]['timestamp']}, latest={subs[-1]['timestamp']}"
        )

    batch = {}
    print()
    write_all_submissions(grouped, batch, base_dir="problems")
    save_sync_batch(batch)

    log_sync_status()
    return grouped


if __name__ == "__main__":
    main()
