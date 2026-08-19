#!/usr/bin/env python3
"""Detect new accounts followed by a target Twitter/X user.

Strategy (this is what keeps it cheap):

  twitterapi.io's followings endpoint returns results sorted by follow date,
  newest first, 200 per page. So to find new follows we only need page 1 --
  not a crawl of the entire following list.

  The catch: if the target *unfollows* someone in the top 200, an older follow
  slides up from rank 201 into page 1 and would look "new". To avoid that we
  keep a complete baseline of every account they follow (built once, on the
  first run) and only report IDs that are absent from that baseline.

  If a page comes back entirely new, we keep paging -- that means they followed
  more than 200 accounts since the last check and page 1 alone would truncate.

Stdlib only, so CI needs no pip install.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://api.twitterapi.io"
FOLLOWINGS_PATH = "/twitter/user/followings"
PAGE_SIZE = 200  # fixed by the endpoint

# Cap incremental crawls so a bug or an API change can never run up a bill.
MAX_PAGES_INCREMENTAL = int(os.environ.get("MAX_PAGES_INCREMENTAL", "5"))
MAX_PAGES_FULL = int(os.environ.get("MAX_PAGES_FULL", "250"))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def http_json(url, headers, method="GET", payload=None, retries=4):
    """GET/POST JSON with exponential backoff on transient failures."""
    body = json.dumps(payload).encode() if payload is not None else None
    last_err = None

    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:800]
            # 4xx other than rate-limit is our fault -- retrying won't help.
            if e.code < 500 and e.code != 429:
                raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e
            last_err = RuntimeError(f"HTTP {e.code} from {url}: {detail}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = RuntimeError(f"request to {url} failed: {e}")

        if attempt < retries - 1:
            delay = 2 ** (attempt + 1)
            log(f"  retrying in {delay}s ({last_err})")
            time.sleep(delay)

    raise last_err


# --------------------------------------------------------------------------
# twitterapi.io
# --------------------------------------------------------------------------


def fetch_page(api_key, username, user_id, cursor):
    params = {"cursor": cursor or ""}
    if user_id:
        params["userId"] = user_id
    else:
        params["userName"] = username

    url = f"{API_BASE}{FOLLOWINGS_PATH}?{urllib.parse.urlencode(params)}"
    data = http_json(url, {"X-API-Key": api_key, "Accept": "application/json"})

    status = str(data.get("status", "")).lower()
    if status and status not in ("success", "ok"):
        raise RuntimeError(f"API returned status={status!r} msg={data.get('msg')!r}")

    # The field name has moved around between doc revisions; accept any of them.
    users = None
    for key in ("followings", "following", "users", "data"):
        val = data.get(key)
        if isinstance(val, list):
            users = val
            break
    if users is None:
        raise RuntimeError(
            "could not find a list of users in the response. Top-level keys: "
            f"{list(data)}. Raw response (truncated): {json.dumps(data)[:600]}"
        )

    cursor = data.get("next_cursor") or data.get("nextCursor") or ""
    has_next = data.get("has_next_page")
    if has_next is None:
        has_next = bool(cursor) and len(users) >= PAGE_SIZE

    return [normalize(u) for u in users], cursor, bool(has_next)


def probe(api_key, username, user_id):
    """Dump the raw shape of one page, to confirm the field names we rely on."""
    params = {"cursor": "", **({"userId": user_id} if user_id else {"userName": username})}
    url = f"{API_BASE}{FOLLOWINGS_PATH}?{urllib.parse.urlencode(params)}"
    data = http_json(url, {"X-API-Key": api_key, "Accept": "application/json"})

    log(f"top-level keys: {list(data)}")
    log(f"status={data.get('status')!r} msg={data.get('msg')!r}")
    for key in ("followings", "following", "users", "data"):
        if isinstance(data.get(key), list):
            entries = data[key]
            log(f"user list found under {key!r}: {len(entries)} entries")
            if entries:
                log(f"first entry fields: {sorted(entries[0])}")
                log("normalized to:\n" + json.dumps(normalize(entries[0]), indent=2))
            break
    else:
        log("NO user list found under any expected key. Raw response:")
        log(json.dumps(data, indent=2)[:2000])
        sys.exit("probe failed: response shape is not what track.py expects")

    cursor = data.get("next_cursor") or data.get("nextCursor") or ""
    log(f"has_next_page={data.get('has_next_page')} next_cursor={'set' if cursor else 'empty'}")
    log("\nProbe OK — the parser matches the live response.")


def normalize(u):
    """Flatten one user object down to the fields we care about."""

    def pick(*names):
        for n in names:
            v = u.get(n)
            if v not in (None, ""):
                return v
        return None

    uid = pick("id", "userId", "id_str", "rest_id")
    handle = pick("userName", "screen_name", "username")
    return {
        "id": str(uid) if uid is not None else None,
        "username": handle,
        "name": pick("name") or "",
        "bio": (pick("description", "bio") or "").strip(),
        "followers": pick("followers", "followers_count") or 0,
        # This endpoint returns only the deprecated `verified` flag, which is
        # false for almost every account -- it is not the blue check. Recorded
        # for completeness; deliberately not rendered as a badge.
        "verified_legacy": bool(pick("verified", "isVerified") or False),
        "account_created": pick("createdAt", "created_at") or "",
        "url": f"https://x.com/{handle}" if handle else None,
    }


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def load_state(path):
    if not path.exists():
        return {"target": None, "seeded_at": None, "last_checked": None, "ids": []}
    with path.open() as f:
        return json.load(f)


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")


def append_log(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for e in entries:
            f.write(json.dumps(e, sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# Crawl
# --------------------------------------------------------------------------


def crawl(api_key, username, user_id, known, full):
    """Walk the followings list, newest first, collecting accounts not in `known`.

    Returns (new_users, all_seen_ids, exhausted) where `exhausted` is True only
    if we paged all the way to the end of the list.
    """
    max_pages = MAX_PAGES_FULL if full else MAX_PAGES_INCREMENTAL
    new_users, seen_ids = [], set()
    cursor, exhausted = "", False

    for page in range(1, max_pages + 1):
        users, cursor, has_next = fetch_page(api_key, username, user_id, cursor)
        if not users:
            exhausted = True
            break

        page_new = [u for u in users if u["id"] and u["id"] not in known]
        seen_ids.update(u["id"] for u in users if u["id"])
        new_users.extend(page_new)
        log(f"  page {page}: {len(users)} accounts, {len(page_new)} not seen before")

        if not has_next or not cursor:
            exhausted = True
            break

        # Incremental mode: the moment a page contains something we already
        # know about, everything below it is older. Stop -- that's the savings.
        if not full and len(page_new) < len(users):
            break
    else:
        log(f"  hit the {max_pages}-page cap; stopping")

    return new_users, seen_ids, exhausted


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def md_escape(text, limit=180):
    text = " ".join((text or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text.replace("|", "\\|")


def build_issue(target, new_users):
    n = len(new_users)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = f"@{target} followed {n} new account{'s' if n != 1 else ''} — {date}"

    lines = [
        f"[@{target}](https://x.com/{target}) followed "
        f"{n} new account{'s' if n != 1 else ''}, newest first.",
        "",
    ]
    for u in new_users:
        handle = u["username"] or u["id"]
        followers = u["followers"]
        followers = f"{followers:,}" if isinstance(followers, int) else followers
        lines.append(f"### [@{handle}]({u['url']})")
        if u["name"]:
            lines.append(f"**{md_escape(u['name'], 80)}** · {followers} followers")
        else:
            lines.append(f"{followers} followers")
        if u["bio"]:
            lines.append("")
            lines.append(f"> {md_escape(u['bio'], 400)}")
        lines.append("")

    lines.append("---")
    lines.append(f"_Checked {now_iso()}._")
    return title, "\n".join(lines)


def create_issue(title, body):
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        log("! GITHUB_TOKEN/GITHUB_REPOSITORY unset — printing instead of filing")
        log(f"\n=== {title} ===\n{body}\n")
        return None

    # An issue opened by github-actions[bot] is somebody else's activity. Under
    # the default "Participating and @mentions" subscription that produces no
    # notification at all, which is why the first detection went unseen. Being
    # assigned or @mentioned both count as participating, so do both rather
    # than depending on the reader's watch settings.
    notify = os.environ.get("NOTIFY_USER") or repo.split("/")[0]
    payload = {"title": title, "body": body}
    if notify:
        payload["body"] = f"{body}\n\ncc @{notify}"
        payload["assignees"] = [notify]

    data = http_json(
        f"https://api.github.com/repos/{repo}/issues",
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "follow-tracker",
        },
        method="POST",
        payload=payload,
    )
    return data.get("html_url")


# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", default=os.environ.get("TARGET_USERNAME", "eladgil"))
    ap.add_argument("--target-id", default=os.environ.get("TARGET_USER_ID") or None)
    ap.add_argument("--state", default=os.environ.get("STATE_PATH", "state/following.json"))
    ap.add_argument("--log", default=os.environ.get("LOG_PATH", "state/new_follows.jsonl"))
    ap.add_argument(
        "--full",
        action="store_true",
        default=os.environ.get("FULL_RESYNC", "").lower() in ("1", "true", "yes"),
        help="crawl the entire following list instead of just the newest pages",
    )
    ap.add_argument("--dry-run", action="store_true", help="don't file an issue or write state")
    ap.add_argument(
        "--probe",
        action="store_true",
        help="fetch one page, report the raw API shape, and exit (1 API call)",
    )
    args = ap.parse_args()

    api_key = os.environ.get("TWITTERAPI_IO_KEY")
    if not api_key:
        sys.exit("TWITTERAPI_IO_KEY is not set. Get a key at https://twitterapi.io")

    if args.probe:
        probe(api_key, args.target, args.target_id)
        return

    state_path, log_path = Path(args.state), Path(args.log)
    state = load_state(state_path)
    known = set(state.get("ids") or [])
    seeding = not state.get("seeded_at") or not known
    full = args.full or seeding

    log(f"Target: @{args.target} · baseline: {len(known)} accounts")
    log(f"Mode: {'full crawl' if full else 'incremental (newest pages only)'}")

    new_users, seen_ids, exhausted = crawl(
        api_key, args.target, args.target_id, known, full
    )

    if seeding:
        # First run establishes the baseline. Reporting 5,000 "new" follows on
        # day one would be noise, so we stay quiet.
        state.update(
            target=args.target,
            seeded_at=now_iso(),
            last_checked=now_iso(),
            ids=sorted(seen_ids),
        )
        if not args.dry_run:
            save_state(state_path, state)
        log(f"\nSeeded baseline with {len(seen_ids)} accounts. No issue filed.")
        log("Future runs will report only follows added after this point.")
        return

    if full and exhausted:
        # A complete crawl is authoritative: drop unfollowed accounts so a
        # re-follow later still registers as new.
        combined = seen_ids
        dropped = len(known - seen_ids)
        if dropped:
            log(f"  {dropped} account(s) no longer followed — pruned from baseline")
    else:
        combined = known | seen_ids

    state.update(
        target=args.target,
        last_checked=now_iso(),
        ids=sorted(combined),
    )

    if not new_users:
        log("\nNo new follows.")
        if not args.dry_run:
            save_state(state_path, state)
        return

    log(f"\n{len(new_users)} new follow(s):")
    for u in new_users:
        log(f"  @{u['username']} — {u['name']}")

    stamp = now_iso()
    entries = [dict(u, detected_at=stamp, target=args.target) for u in new_users]
    title, body = build_issue(args.target, new_users)

    if args.dry_run:
        log(f"\n(dry run)\n\n=== {title} ===\n{body}")
        return

    url = create_issue(title, body)
    if url:
        log(f"Filed {url}")

    append_log(log_path, entries)
    save_state(state_path, state)


if __name__ == "__main__":
    main()
