"""Exercise track.py's diff logic against a fake twitterapi.io.

The mock mirrors the real response shape, as confirmed by `track.py --probe`
against the live endpoint: a `followings` list of 200, with `followers_count`,
`screen_name` and the legacy `verified` flag.

Run: python3 test_track.py
"""
import json, sys, tempfile, io
from pathlib import Path
from contextlib import redirect_stdout

import os
os.environ["API_MIN_INTERVAL"] = "0"   # no throttle against a mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
import track

PAGE = track.PAGE_SIZE
calls = {"n": 0}


def make_api(following_ids):
    """following_ids: newest-first list of ints."""
    def fake(url, headers, method="GET", payload=None, retries=4):
        if "api.github.com" in url:
            return {"html_url": "https://github.com/x/y/issues/1"}
        calls["n"] += 1
        from urllib.parse import urlparse, parse_qs
        cur = parse_qs(urlparse(url).query).get("cursor", [""])[0]
        off = int(cur) if cur else 0
        chunk = following_ids[off:off + PAGE]
        nxt = off + PAGE
        return {
            "status": "success", "msg": "success", "code": 0,
            "followings": [
                {"id": str(i), "userName": f"user{i}", "screen_name": f"user{i}",
                 "name": f"User {i}", "description": f"bio {i}",
                 "followers_count": 1000 + i, "following_count": 10,
                 "verified": False, "protected": False,
                 "created_at": "Mon Jun 12 04:49:35 +0000 2017"}
                for i in chunk
            ],
            "has_next_page": nxt < len(following_ids),
            "next_cursor": str(nxt) if nxt < len(following_ids) else "",
        }
    return fake


def run(following_ids, statedir, argv, env=None):
    calls["n"] = 0
    track.http_json = make_api(following_ids)
    import os
    os.environ["TWITTERAPI_IO_KEY"] = "test"
    os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("GITHUB_REPOSITORY", None)
    for k, v in (env or {}).items():
        os.environ[k] = v
    sys.argv = ["track.py", "--targets", "eladgil",
                "--state", f"{statedir}/following.json",
                "--log", f"{statedir}/new_follows.jsonl"] + argv
    buf = io.StringIO()
    with redirect_stdout(buf):
        track.main()
    return buf.getvalue(), calls["n"]


def state_ids(statedir):
    return set(json.loads(Path(f"{statedir}/following.json").read_text())["ids"])


def logged(statedir):
    p = Path(f"{statedir}/new_follows.jsonl")
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


fails = []
def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


d = tempfile.mkdtemp()

# ---- 1. first run seeds a 500-account baseline, files nothing -------------
base = list(range(500, 0, -1))  # newest-first: 500 is the most recent follow
out, n = run(base, d, [])
check("seed: no issue filed", "No issue filed" in out)
check("seed: full baseline stored", state_ids(d) == {str(i) for i in base},
      f"got {len(state_ids(d))}")
check("seed: crawled all 3 pages", n == 3, f"calls={n}")
check("seed: nothing logged", logged(d) == [])

# ---- 2. two new follows -> reported, only page 1 fetched ------------------
out, n = run([502, 501] + base, d, [])
check("new: both detected", "2 new follow(s)" in out, out[-400:])
check("new: correct handles", "@user502" in out and "@user501" in out)
check("new: only one API call", n == 1, f"calls={n}")
check("new: baseline grew to 502", len(state_ids(d)) == 502)
check("new: logged to jsonl", [e["username"] for e in logged(d)] == ["user502", "user501"])

# ---- 3. quiet day --------------------------------------------------------
out, n = run([502, 501] + base, d, [])
check("quiet: no new follows", "No new follows." in out)
check("quiet: one call", n == 1)
check("quiet: nothing new logged", len(logged(d)) == 2)

# ---- 4. THE FALSE-POSITIVE CASE: unfollow pulls an old account into page 1
# Drop #400 (inside page 1). Account #101 slides up from rank 201 into page 1.
shifted = [i for i in ([502, 501] + base) if i != 400]
out, n = run(shifted, d, [])
check("unfollow: no false positive", "No new follows." in out, out[-400:])

# ---- 5. burst of 250 new follows -> pages past page 1 --------------------
burst = list(range(752, 502, -1))
out, n = run(burst + shifted, d, [])
check("burst: all 250 detected", "250 new follow(s)" in out, out[-400:])
check("burst: paged twice", n == 2, f"calls={n}")

# ---- 6. --full prunes unfollowed accounts --------------------------------
# Two stale IDs should go: #300 (unfollowed just now) and #400 (unfollowed back
# in step 4 -- incremental mode never prunes, so it lingered until this resync).
before = state_ids(d)
live = [i for i in (burst + shifted) if i != 300]
out, n = run(live, d, ["--full"])
after = state_ids(d)
check("full: baseline matches live list exactly", after == {str(i) for i in live},
      f"{len(before)} -> {len(after)}, expected {len(live)}")
check("full: pruned both stale entries", before - after == {"300", "400"},
      f"pruned {sorted(before - after)}")
check("full: reported nothing new", "No new follows." in out)

# ---- 7. re-follow after a prune is reported as new ------------------------
out, n = run([300] + [i for i in (burst + shifted) if i != 300], d, [])
check("refollow: reported as new", "1 new follow(s)" in out and "@user300" in out)

# ---- 8. dry-run writes nothing -------------------------------------------
snapshot = Path(f"{d}/following.json").read_text()
out, n = run([999] + [300] + [i for i in (burst + shifted) if i != 300], d, ["--dry-run"])
check("dry-run: previews the issue", "(dry run)" in out and "@user999" in out)
check("dry-run: state untouched", Path(f"{d}/following.json").read_text() == snapshot)

# ---- 9. issue body renders ------------------------------------------------
title, body = track.build_issue("eladgil", [track.normalize(
    {"id": "1", "userName": "someone", "screen_name": "someone",
     "name": "Some | One", "followers_count": 12345,
     "description": "  builder of things\nsecond line ", "verified": False})])
check("issue: title", title.startswith("@eladgil followed 1 new account —"), title)
check("issue: link", "[@someone](https://x.com/someone)" in body)
check("issue: follower count formatted", "12,345 followers" in body)
check("issue: bio flattened and pipe-escaped", "> builder of things second line" in body)

# ---- 10. per-target paths are derived and isolated -----------------------
import argparse as _ap
_a = _ap.Namespace(state=None, log=None, state_dir="state")
sp, lp = track.target_paths("eladgil", _a)
sp2, _ = track.target_paths("GuptaRK22", _a)
check("paths: derived per target", str(sp) == "state/eladgil/following.json", str(sp))
check("paths: log alongside state", str(lp) == "state/eladgil/new_follows.jsonl", str(lp))
check("paths: targets do not collide", sp != sp2)
_b = _ap.Namespace(state="x/s.json", log="x/l.jsonl", state_dir="state")
check("paths: explicit override wins", str(track.target_paths("eladgil", _b)[0]) == "x/s.json")

# ---- 11. legacy single-file state is adopted, not re-seeded ---------------
import tempfile as _tf
legacy_dir = _tf.mkdtemp()
Path(f"{legacy_dir}/following.json").write_text(json.dumps(
    {"target": "eladgil", "seeded_at": "2026-08-17T07:11:11+00:00",
     "last_checked": "x", "ids": ["1", "2", "3"]}))
adopted = track.load_state_migrating("eladgil", Path(f"{legacy_dir}/eladgil/following.json"), legacy_dir)
check("migration: legacy baseline adopted", set(adopted["ids"]) == {"1", "2", "3"},
      f"got {adopted}")
other = track.load_state_migrating("GuptaRK22", Path(f"{legacy_dir}/GuptaRK22/following.json"), legacy_dir)
check("migration: other target not given elad's ids", not other.get("ids"), f"got {other}")

# ---- 12. THE RE-FOLLOW CASE: id already in the baseline, back at rank 1 ----
# @GuptaRK22 re-followed @jaredkushner; the id was still in the baseline from a
# follow he had since dropped, so a set difference alone reported nothing.
rf = tempfile.mkdtemp()
base_rf = list(range(300, 0, -1))                 # 300 accounts, newest = 300
out, n = run(base_rf, rf, [])                      # seed
check("refollow: seeded quietly", "No issue filed" in out)
out, n = run(base_rf, rf, [])                      # quiet run, anchors top_id
check("refollow: quiet day stays quiet", "No new follows." in out)
# he unfollows #50, then re-follows them -> back to rank 1, id already known
moved = [50] + [i for i in base_rf if i != 50]
out, n = run(moved, rf, [])
check("refollow: detected despite id in baseline", "1 new follow(s)" in out, out[-300:])
check("refollow: names the right account", "@user50" in out, out[-300:])
# and a genuine new follow alongside a re-follow, both reported newest-first
moved2 = [999, 50] + [i for i in base_rf if i != 50]
out, n = run(moved2, rf, [])
check("refollow: new follow above a re-follow still caught", "@user999" in out, out[-300:])

print("\n=== SAMPLE ISSUE ===")
print(title)
print(body)

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
