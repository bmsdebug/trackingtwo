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
FOLLOWERS_PATH = "/twitter/user/followers"
PAGE_SIZE = 200  # fixed by the endpoint

# Cap incremental crawls so a bug or an API change can never run up a bill.
MAX_PAGES_INCREMENTAL = int(os.environ.get("MAX_PAGES_INCREMENTAL", "5"))
MAX_PAGES_FULL = int(os.environ.get("MAX_PAGES_FULL", "250"))

# twitterapi.io's free tier allows one request every 5 seconds and answers 429
# otherwise. Retries absorbed that, but pacing the calls avoids the churn --
# and with more than one target there are more back-to-back calls to pace.
_last_api_call = [0.0]


def _throttle():
    interval = float(os.environ.get("API_MIN_INTERVAL", "5.2"))
    wait = interval - (time.monotonic() - _last_api_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_api_call[0] = time.monotonic()


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


def fetch_page(api_key, username, user_id, cursor, path=FOLLOWINGS_PATH):
    params = {"cursor": cursor or ""}
    if user_id:
        params["userId"] = user_id
    else:
        params["userName"] = username

    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    _throttle()
    data = http_json(url, {"X-API-Key": api_key, "Accept": "application/json"})

    status = str(data.get("status", "")).lower()
    if status and status not in ("success", "ok"):
        raise RuntimeError(f"API returned status={status!r} msg={data.get('msg')!r}")

    # The field name has moved around between doc revisions; accept any of them.
    users = None
    for key in ("followings", "following", "followers", "users", "data"):
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
                # The head of page 1 is what the incremental fast path relies on
                # being the newest follows. Print it so ordering can be checked
                # against reality rather than assumed.
                log("first 8 handles on page 1 (newest first, if ordering holds):")
                for i, e in enumerate(entries[:8], 1):
                    n = normalize(e)
                    log(f"  {i:2d}. @{n['username']}  ({n['name']})")
                log("normalized first entry:\n" + json.dumps(normalize(entries[0]), indent=2))
            break
    else:
        log("NO user list found under any expected key. Raw response:")
        log(json.dumps(data, indent=2)[:2000])
        sys.exit("probe failed: response shape is not what track.py expects")

    cursor = data.get("next_cursor") or data.get("nextCursor") or ""
    log(f"has_next_page={data.get('has_next_page')} next_cursor={'set' if cursor else 'empty'}")
    log("\nProbe OK — the parser matches the live response.")


def check_follows(api_key, source, target, max_pages=50):
    """Answer 'does @source follow @target?' by scanning source's followings.

    Walks pages newest-first and stops at the first match, so a recent follow
    costs one call. A negative answer costs a full crawl -- that is inherent:
    proving absence means looking everywhere.
    """
    src = source.lstrip("@")
    want = target.lstrip("@").lower()
    cursor, scanned = "", 0

    for page in range(1, max_pages + 1):
        users, cursor, has_next = fetch_page(api_key, src, None, cursor)
        if not users:
            break
        for i, u in enumerate(users, 1):
            if (u["username"] or "").lower() == want:
                log(f"\nYES — @{src} follows @{u['username']}.")
                log(f"Position {scanned + i} in the following list (newest first), "
                    f"so it is follow #{scanned + i} counting back from most recent.")
                log(json.dumps(u, indent=2))
                return True
        scanned += len(users)
        log(f"  page {page}: {scanned} accounts scanned, no match yet")
        if not has_next or not cursor:
            break
    else:
        log(f"! stopped at the {max_pages}-page cap — answer is inconclusive")
        return None

    log(f"\nNO — @{want} is not in @{src}'s following list ({scanned} accounts, "
        f"complete list scanned).")
    return False


def dump_connections(api_key, handle, kind="following", limit=100000,
                     out_path=None, max_pages=None):
    """Page out a handle's complete following or follower list.

    Returns (rows, truncated). `truncated` is True when a cap stopped the crawl
    before the end -- a partial list reported as complete would be worse than
    no list at all, so the caller must surface it.
    """
    src = handle.lstrip("@")
    path = FOLLOWINGS_PATH if kind == "following" else FOLLOWERS_PATH
    cap = max_pages or int(os.environ.get("MAX_DUMP_PAGES", "100"))
    rows, cursor, pages, truncated = [], "", 0, False

    while len(rows) < limit:
        if pages >= cap:
            truncated = True
            log(f"! hit the {cap}-page cap ({len(rows)} rows) — list is incomplete")
            break
        users, cursor, has_next = fetch_page(api_key, src, None, cursor, path)
        pages += 1
        if not users:
            break
        rows.extend(users)
        if pages % 5 == 0 or not has_next:
            log(f"  page {pages}: {len(rows)} rows")
        if not has_next or not cursor:
            break
    else:
        truncated = len(rows) >= limit

    rows = rows[:limit]
    label = "following" if kind == "following" else "followers"
    log(f"@{src}: {len(rows)} {label}{' (TRUNCATED)' if truncated else ' (complete)'}")

    if out_path:
        import csv
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["rank", "handle", "name", "followers", "bio", "url"])
            for i, u in enumerate(rows, 1):
                wr.writerow([i, u["username"], u["name"], u["followers"],
                             " ".join((u["bio"] or "").split()), u["url"]])
        log(f"wrote {p}")
    return rows, truncated


def recent_follows(api_key, source, limit=30, out_path=None):
    """List a handle's most recent follows, newest first, paging up to `limit`.

    The API exposes order but no follow timestamps -- the only date on a user
    object is when that account joined X. So this answers "the last N accounts
    they followed", never "the ones they followed since <date>". Converting N
    into a time window needs an assumed follow rate, which is the caller's
    judgement, not something the data supports.
    """
    src = source.lstrip("@")
    collected, cursor = [], ""
    pages = 0
    while len(collected) < limit:
        users, cursor, has_next = fetch_page(api_key, src, None, cursor)
        pages += 1
        if not users:
            break
        collected.extend(users)
        log(f"  page {pages}: {len(collected)} collected")
        if not has_next or not cursor:
            log(f"  reached the end of @{src}'s following list")
            break
    collected = collected[:limit]

    log(f"\n@{src}: {len(collected)} most recent follows, newest first "
        f"({pages} page(s)). Order only -- the API gives no follow dates.\n")

    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w") as f:
            f.write(f"# @{src} — {len(collected)} most recent follows\n\n")
            f.write(f"Newest first, captured {now_iso()}.\n\n")
            f.write("The API returns follow **order**, not follow dates, so these "
                    "cannot be attributed to a specific day.\n\n")
            f.write("| # | Handle | Name | Followers | Bio |\n")
            f.write("|---:|---|---|---:|---|\n")
            for i, u in enumerate(collected, 1):
                n = u["followers"]
                n = f"{n:,}" if isinstance(n, int) else str(n)
                bio = " ".join((u["bio"] or "").split()).replace("|", "\\|")[:160]
                name = (u["name"] or "").replace("|", "\\|")
                f.write(f"| {i} | [@{u['username']}](https://x.com/{u['username']}) "
                        f"| {name} | {n} | {bio} |\n")
        log(f"wrote {p} ({len(collected)} rows)")
    else:
        for i, u in enumerate(collected, 1):
            n = u["followers"]
            n = f"{n:,}" if isinstance(n, int) else str(n)
            log(f"{i:3d}. @{u['username']}  —  {u['name']}  ·  {n} followers")
    return collected


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

    Returns (new_users, all_seen_ids, exhausted, ordered) where `exhausted` is
    True only if we paged all the way to the end of the list, and `ordered` is
    every account seen, in list order, newest first.
    """
    max_pages = MAX_PAGES_FULL if full else MAX_PAGES_INCREMENTAL
    new_users, seen_ids, ordered = [], set(), []
    cursor, exhausted = "", False

    for page in range(1, max_pages + 1):
        users, cursor, has_next = fetch_page(api_key, username, user_id, cursor)
        if not users:
            exhausted = True
            break

        page_new = [u for u in users if u["id"] and u["id"] not in known]
        seen_ids.update(u["id"] for u in users if u["id"])
        ordered.extend(users)
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

    return new_users, seen_ids, exhausted, ordered


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


def target_paths(target, args):
    """Where this target's state lives. Explicit --state/--log win (tests)."""
    if args.state and args.log:
        return Path(args.state), Path(args.log)
    d = Path(args.state_dir) / target
    return d / "following.json", d / "new_follows.jsonl"


def load_state_migrating(target, state_path, state_dir):
    """Load state, falling back to the pre-multi-target single-file layout.

    Without this a layout change would look like a fresh target and silently
    re-seed, losing the baseline and every follow made in the gap.
    """
    if state_path.exists():
        return load_state(state_path)
    legacy = Path(state_dir) / "following.json"
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text())
        except (OSError, json.JSONDecodeError):
            return load_state(state_path)
        if data.get("target") == target:
            log(f"  adopting legacy baseline from {legacy}")
            return data
    return load_state(state_path)


def process_target(api_key, target, args):
    """Run one target end to end. Returns True if anything was reported."""
    state_path, log_path = target_paths(target, args)
    # Only consult the legacy layout when paths were derived. With an explicit
    # --state the caller means that file and nothing else.
    state = (load_state(state_path) if args.state
             else load_state_migrating(target, state_path, args.state_dir))
    known = set(state.get("ids") or [])
    seeding = not state.get("seeded_at") or not known
    full = args.full or seeding

    log(f"\n=== @{target} ===")
    log(f"baseline: {len(known)} accounts · "
        f"mode: {'full crawl' if full else 'incremental (newest pages only)'}")

    new_users, seen_ids, exhausted, ordered = crawl(api_key, target, None, known, full)

    # A re-follow moves an account back to rank 1 but its id is already in the
    # baseline, so a set difference alone stays silent -- @GuptaRK22 re-followed
    # @jaredkushner on 2026-10-22 and nothing fired. The list is newest-first,
    # so everything above the account that topped it last time is new,
    # membership notwithstanding.
    prev_top = state.get("top_id")
    top_idx = None
    if prev_top and not seeding:
        top_idx = next((i for i, u in enumerate(ordered) if u["id"] == prev_top), None)
        if top_idx is None:
            log("  previous top account is gone (unfollowed); "
                "falling back to baseline diff only")
        elif top_idx:
            known_ids = {u["id"] for u in new_users}
            refollows = [u for u in ordered[:top_idx]
                         if u["id"] and u["id"] not in known_ids]
            if refollows:
                log(f"  {len(refollows)} re-follow(s) above the previous top account")
                # keep newest-first ordering across both kinds of new follow
                new_users = [u for u in ordered[:top_idx]] + [
                    u for u in new_users if u["id"] not in
                    {x["id"] for x in ordered[:top_idx]}]

    if seeding:
        # First run establishes the baseline. Reporting ~1,800 "new" follows on
        # day one would be noise, so we stay quiet.
        state.update(target=target, seeded_at=now_iso(),
                     last_checked=now_iso(), ids=sorted(seen_ids),
                     top_id=(ordered[0]["id"] if ordered else None))
        if not args.dry_run:
            save_state(state_path, state)
        log(f"Seeded baseline with {len(seen_ids)} accounts. No issue filed.")
        log("Future runs will report only follows added after this point.")
        return False

    if full and exhausted:
        # A complete crawl is authoritative: drop unfollowed accounts so a
        # re-follow later still registers as new.
        combined = seen_ids
        dropped = len(known - seen_ids)
        if dropped:
            log(f"  {dropped} account(s) no longer followed — pruned from baseline")
    else:
        combined = known | seen_ids

    state.update(target=target, last_checked=now_iso(), ids=sorted(combined),
                 top_id=(ordered[0]["id"] if ordered else state.get("top_id")))

    if not new_users:
        log("No new follows.")
        if not args.dry_run:
            save_state(state_path, state)
        return False

    log(f"{len(new_users)} new follow(s):")
    for u in new_users:
        log(f"  @{u['username']} — {u['name']}")

    stamp = now_iso()
    entries = [dict(u, detected_at=stamp, target=target) for u in new_users]
    title, body = build_issue(target, new_users)

    if args.dry_run:
        log(f"\n(dry run)\n\n=== {title} ===\n{body}")
        return True

    url = create_issue(title, body)
    if url:
        log(f"Filed {url}")
    append_log(log_path, entries)
    save_state(state_path, state)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--targets",
        default=os.environ.get("TARGETS") or os.environ.get("TARGET_USERNAME", "eladgil"),
        help="comma-separated handles to track",
    )
    ap.add_argument("--state-dir", default=os.environ.get("STATE_DIR", "state"))
    ap.add_argument("--state", default=os.environ.get("STATE_PATH") or None)
    ap.add_argument("--log", default=os.environ.get("LOG_PATH") or None)
    ap.add_argument(
        "--full",
        action="store_true",
        default=os.environ.get("FULL_RESYNC", "").lower() in ("1", "true", "yes"),
        help="crawl the entire following list instead of just the newest pages",
    )
    ap.add_argument("--dry-run", action="store_true", help="don't file an issue or write state")
    ap.add_argument(
        "--recent",
        metavar="HANDLE",
        help="list HANDLE's most recent follows, newest first, then exit",
    )
    ap.add_argument("--limit", type=int, default=0, help="max rows (0 = no limit for --dump)")
    ap.add_argument("--out", default=None, help="write results to this file")
    ap.add_argument("--dump", metavar="HANDLE", help="dump HANDLE's full list to CSV")
    ap.add_argument("--kind", choices=("following", "followers"), default="following",
                    help="which list --dump should fetch")
    ap.add_argument(
        "--check",
        nargs=2,
        metavar=("SOURCE", "TARGET"),
        help="answer whether SOURCE follows TARGET, then exit",
    )
    ap.add_argument(
        "--probe",
        action="store_true",
        help="fetch one page, report the raw API shape, and exit (1 API call)",
    )
    args = ap.parse_args()

    api_key = os.environ.get("TWITTERAPI_IO_KEY")
    if not api_key:
        sys.exit("TWITTERAPI_IO_KEY is not set. Get a key at https://twitterapi.io")

    targets = [t.strip().lstrip("@") for t in args.targets.split(",") if t.strip()]
    if not targets:
        sys.exit("no targets configured — set TARGETS to a comma-separated list")

    if args.dump:
        dump_connections(api_key, args.dump, args.kind, args.limit or 100000, args.out)
        return

    if args.recent:
        recent_follows(api_key, args.recent, args.limit or 30, args.out)
        return

    if args.check:
        check_follows(api_key, args.check[0], args.check[1])
        return

    if args.probe:
        probe(api_key, targets[0], None)
        return

    if args.state and len(targets) > 1:
        sys.exit("--state/--log override a single target's paths; pass one target")

    log(f"Tracking {len(targets)} target(s): {', '.join('@' + t for t in targets)}")

    # One target failing must not stop the others -- a suspended or renamed
    # account should not silence every other report.
    failures = []
    for t in targets:
        try:
            process_target(api_key, t, args)
        except Exception as e:  # noqa: BLE001 - reported and re-raised below
            log(f"! @{t} failed: {e}")
            failures.append(t)

    if failures:
        sys.exit(f"failed for: {', '.join(failures)}")


if __name__ == "__main__":
    main()
