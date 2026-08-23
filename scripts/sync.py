"""
Sync script: pulls submission data from Google Sheets (via Apps Script GET
endpoints) and groups/sorts it by problem.

Environment variables expected:
    SHEET_URLS   - comma-separated list of Apps Script GET endpoint URLs
                   (one per sheet). Stored as a GitHub Actions secret.

Each sheet's GET endpoint is expected to return JSON: a list of rows,
where each row has ONE field containing a JSON *string* like:

    {
        "slug": "two-sum",
        "code": "...",
        "timestamp": "2026-08-23T10:15:00Z",
        "email": "user@example.com",
        "language": "python",
        "status": "accepted"
    }

This script does not yet know your exact response shape (e.g. whether
each row is {"data": "<json-string>"} or a raw list of json strings), so
`extract_json_string` below has a couple of common shapes handled and a
clear place to adjust once you confirm your endpoint's real shape.
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone
from collections import defaultdict

# This copy of sync.py lives in the PROBLEMS repo, so this is hardcoded here.
# The USERS repo will have its own separate copy of this file with this
# constant set to "users" instead - that's the only difference between them.
REPO_NAME = "problems"


def get_sheet_urls():
    """Read the comma-separated sheet endpoint URLs from the secret env var."""
    raw = os.environ.get("SHEET_URLS", "")
    urls = [u.strip() for u in raw.split(",") if u.strip()]
    if not urls:
        print("ERROR: No sheet URLs found in SHEET_URLS env var.", file=sys.stderr)
        sys.exit(1)
    return urls


def fetch_sheet_rows(url):
    """
    Hit one Apps Script GET endpoint and return its raw rows.
    Retries once on failure since Apps Script endpoints can be flaky/cold.
    """
    for attempt in (1, 2):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  Attempt {attempt} failed for {url}: {e}", file=sys.stderr)
            if attempt == 2:
                print(f"  Skipping this sheet after 2 failed attempts.", file=sys.stderr)
                return []


def extract_json_string(row):
    """
    Pull the actual JSON string out of one raw row returned by the sheet.

    Adjust this function once you tell me the exact shape your Apps
    Script endpoint returns. Common shapes handled for now:
      - row is already the JSON string itself
      - row is a dict like {"data": "<json-string>"}
      - row is a list/tuple where the JSON string is the first cell
    """
    if isinstance(row, str):
        return row
    if isinstance(row, dict):
        # try a couple of likely key names
        for key in ("data", "value", "json", "submission"):
            if key in row:
                return row[key]
        # fallback: take the first value in the dict
        return next(iter(row.values()), None)
    if isinstance(row, (list, tuple)) and row:
        return row[0]
    return None


def parse_submission(raw_json_string):
    """Parse one submission's JSON string into a dict, or None if invalid."""
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
        print(f"  Skipping row - missing fields {missing}: {submission}", file=sys.stderr)
        return None

    return submission


def fetch_all_submissions(sheet_urls):
    """Fetch and parse submissions from every sheet, returning a flat list."""
    all_submissions = []
    for url in sheet_urls:
        print(f"Fetching sheet: {url[:40]}...")
        rows = fetch_sheet_rows(url)
        print(f"  Got {len(rows)} raw rows")
        for row in rows:
            raw_json_string = extract_json_string(row)
            submission = parse_submission(raw_json_string)
            if submission:
                all_submissions.append(submission)
    return all_submissions


def group_by_problem(submissions):
    """
    Group submissions by their 'slug' (problem identifier), then sort each
    group's submissions by timestamp ascending (oldest first, latest last).
    """
    grouped = defaultdict(list)
    for submission in submissions:
        grouped[submission["slug"]].append(submission)

    for slug, subs in grouped.items():
        subs.sort(key=lambda s: int(s["timestamp"]))

    return grouped


def is_accepted(submission):
    """True if this submission's status counts as 'Accepted'."""
    return str(submission.get("status", "")).strip().lower() == "accepted"


def split_accepted_errors(submissions):
    """Split a list of submissions into (accepted_list, error_list)."""
    accepted, errors = [], []
    for s in submissions:
        (accepted if is_accepted(s) else errors).append(s)
    return accepted, errors


def to_stored_format(submission):
    """
    Convert a raw sheet submission (which includes 'slug') into the shape
    that actually gets stored in data/<n>.json (slug is dropped, since the
    folder name already encodes it).
    """
    return {
        "email": submission["email"],
        "timestamp": int(submission["timestamp"]),
        "language": submission["language"],
        "status": submission["status"],
        "code": submission["code"],
    }


def get_default_limit():
    """
    Limit used ONLY when creating config.json for a brand-new problem that
    doesn't have one yet. Read from the DEFAULT_LIMIT env var (settable as
    a GitHub secret/variable or local env var) so nothing is hardcoded in
    the script itself. Falls back to 100 only if that env var is unset.
    """
    return int(os.environ.get("DEFAULT_LIMIT", "100"))


def load_config(config_path):
    """Read config.json, or create a fresh default one if it doesn't exist."""
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return json.load(f)
    return {"current": 1, "limit": get_default_limit()}


def save_config(config_path, config):
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)


def load_data_file(file_path):
    """Read a data/<n>.json array file, or [] if it doesn't exist yet."""
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            return json.load(f)
    return []


def save_data_file(file_path, submissions):
    with open(file_path, "w") as f:
        json.dump(submissions, f, indent=2)


def append_submissions_to_problem(problem_dir, submissions):
    """
    Append `submissions` (already in stored format, oldest-first) into the
    numbered data files under `problem_dir/data/`, respecting config.json's
    "limit" per file, filling partially-full files first and rolling over
    into new files as needed. Updates config.json's "current" at the end.
    """
    if not submissions:
        return

    data_dir = os.path.join(problem_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    config_path = os.path.join(problem_dir, "config.json")
    config = load_config(config_path)

    current = config.get("current", 1)
    limit = config.get("limit", get_default_limit())

    remaining = list(submissions)  # copy, oldest-first order preserved
    while remaining:
        file_path = os.path.join(data_dir, f"{current}.json")
        existing = load_data_file(file_path)
        space = limit - len(existing)

        if space <= 0:
            # this file is already full, move to the next one
            current += 1
            continue

        chunk = remaining[:space]
        remaining = remaining[space:]
        existing.extend(chunk)
        save_data_file(file_path, existing)

        if remaining:
            # still more to place, this file is now full, advance
            current += 1

    config["current"] = current
    config["limit"] = limit
    save_config(config_path, config)


def write_all_submissions(grouped, base_dir="problems"):
    """
    For every problem (slug), split its submissions into accepted/errors,
    convert to stored format, and append into the correct folder structure:
        problems/accepted/<slug>/data/<n>.json
        problems/errors/<slug>/data/<n>.json
    """
    for slug, submissions in grouped.items():
        accepted, errors = split_accepted_errors(submissions)

        if accepted:
            accepted_dir = os.path.join(base_dir, "accepted", slug)
            append_submissions_to_problem(
                accepted_dir, [to_stored_format(s) for s in accepted]
            )
            print(f"  [{slug}] appended {len(accepted)} accepted submission(s)")

        if errors:
            errors_dir = os.path.join(base_dir, "errors", slug)
            append_submissions_to_problem(
                errors_dir, [to_stored_format(s) for s in errors]
            )
            print(f"  [{slug}] appended {len(errors)} error submission(s)")


def get_today_date_str():
    """Today's date in UTC, as YYYY-MM-DD (must match what the Apps Script
    status sheet uses, so both sides agree on 'today')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def log_sync_status():
    """
    Notify the status-tracking Google Sheet that this repo's sync finished
    successfully today. REPO_NAME (top of file) identifies which repo this
    is - hardcoded to "problems" in this copy, since this file lives in the
    problems repo. The future users-repo copy of this same file will just
    have that constant set to "users" instead.

    STATUS_SHEET_URL is read from an env var / GitHub secret since it's a
    write-capable endpoint.
    """
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
        # Don't fail the whole run over the status log - the actual data
        # sync already succeeded by this point, this is just bookkeeping.
        print(f"WARNING: failed to log sync status: {e}", file=sys.stderr)


def main():
    sheet_urls = get_sheet_urls()
    print(f"Found {len(sheet_urls)} sheet URL(s) to sync.\n")

    submissions = fetch_all_submissions(sheet_urls)
    print(f"\nTotal valid submissions collected: {len(submissions)}")

    grouped = group_by_problem(submissions)
    print(f"Grouped into {len(grouped)} distinct problem(s):")
    for slug, subs in grouped.items():
        print(f"  - {slug}: {len(subs)} submission(s), "
              f"oldest={subs[0]['timestamp']}, latest={subs[-1]['timestamp']}")

    print()
    write_all_submissions(grouped, base_dir="problems")

    # Only reached if everything above succeeded without raising
    log_sync_status()

    return grouped


if __name__ == "__main__":
    main()