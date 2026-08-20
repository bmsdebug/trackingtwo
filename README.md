# Follow tracker

Files a GitHub issue whenever a tracked account follows someone new, with the new
account's handle, name, bio and follower count. Currently tracking
[@eladgil](https://x.com/eladgil) and [@GuptaRK22](https://x.com/GuptaRK22).

Runs on GitHub Actions once a day. No server and no extra accounts — the only
running cost is one small API call per check.

## How it works

`twitterapi.io`'s followings endpoint returns results **sorted by follow date,
newest first**, 200 per page. That's the whole trick: new follows always appear at
the top, so each check only needs page 1 — not a crawl of every account they
follow. Verified against the live API: page 1 comes back in exact
reverse-chronological order.

1. **First run** for a handle crawls its complete following list once and saves
   every account ID to `state/<handle>/following.json`. This baseline is what makes the cheap page-1 check
   correct — see the note below. No issue is filed on this run.
2. **Every run after** fetches just page 1 and reports any ID missing from the
   baseline. If page 1 comes back *entirely* new (200+ follows since the last
   check), it keeps paging until it hits familiar territory, up to 5 pages.
3. New follows are appended to `state/<handle>/new_follows.jsonl` as a permanent
   history, and the updated state is committed back to the repo.

<details>
<summary>Why the full baseline matters</summary>

If we only ever remembered the last page 1, an **unfollow** would produce a false
alarm: dropping someone from the top 200 pulls an old follow up from rank 201 into
page 1, where it looks brand new. Diffing against the complete set instead of
against the previous page means slide-ups are recognised and stay quiet.

</details>

## Setup

1. **Get an API key** at [twitterapi.io](https://twitterapi.io) — no X developer
   account needed, pay-as-you-go with no subscription. Add a couple of dollars;
   at this usage it will last years.
2. **Add it as a repo secret:** Settings → Secrets and variables → Actions → New
   repository secret, named exactly `TWITTERAPI_IO_KEY`. It must be a **repository
   secret**, not a variable and not an environment secret — see Troubleshooting.
3. **Merge this to your default branch.** GitHub only runs `schedule:` workflows
   from the default branch — on a side branch the cron will never fire.
4. **Run it once manually:** Actions → *Track new follows* → Run workflow. This
   seeds the baseline (takes a minute or two, one API call per 200 accounts).

That's it — from then on it runs itself daily at 14:07 UTC and opens an issue when
there's something to report. Each issue assigns and ccs you so it actually reaches
your inbox; owning the repo is *not* by itself enough to be notified about a
bot-authored issue (see Troubleshooting).

### Tracking more accounts

Add a repository **variable** (not secret) called `TARGETS`, set to a
comma-separated list of handles — e.g. `eladgil,GuptaRK22,someoneelse`. Each gets
its own baseline under `state/<handle>/` and its own issues, and a newly added
handle seeds itself silently on its next run. Removing a handle stops the checks
but leaves its history in place.

One target failing (renamed, suspended, gone private) is logged and the run
continues, so it cannot silence the others; the job still exits non-zero so the
failure is visible.

## Cost

| | |
|---|---|
| GitHub Actions | Free (public repo; ~1 min/day of private-repo minutes otherwise) |
| Each check | 1 API call, 200 profiles |
| One-time seed | ~1 call per 200 accounts followed |

twitterapi.io bills per profile returned, roughly $1 per 100,000 profiles, with a
per-call minimum the published pricing is vague about. One check a day of 200
profiles lands **under $1/year** either way; the one-time seed crawl was the only
real spend. Raising the cron to `"7 */4 * * *"` or `"7 * * * *"` multiplies that by
4x or 24x in exchange for a shorter worst-case delay.

`MAX_PAGES_INCREMENTAL` (default 5) hard-caps how many pages a single run can
fetch, so a bug or an upstream change can't quietly run up a bill. Calls are paced
to one every 5.2s (`API_MIN_INTERVAL`) because the free tier answers 429 above one
request per 5 seconds.

## Running locally

```bash
export TWITTERAPI_IO_KEY=...
python track.py --probe        # 1 call: dump the raw API shape and stop
python track.py --check a b    # does @a follow @b?  (also on the Run workflow form)
python track.py --targets x,y  # override the tracked handles
python track.py --dry-run      # print what would be filed, touch nothing
python track.py --full         # re-crawl everything; also prunes unfollows
```

`--dry-run` needs no GitHub credentials. Without `GITHUB_TOKEN` the script prints
the issue body instead of filing it. All three are also available as checkboxes on
the *Run workflow* button.

```bash
python3 test_track.py    # 31 checks against a mock API, no key or network needed
```

The mock mirrors the live response shape (`followings`, `followers_count`,
`screen_name`), and the suite covers the cases that are easy to get wrong:
seeding, the unfollow slide-up false positive, a 200+ follow burst, `--full`
pruning, re-follow after a prune, `--dry-run` leaving state untouched, per-target
path isolation, and adopting the pre-multi-target state layout instead of
re-seeding.

## Troubleshooting

**Run fails in ~1 second with `TWITTERAPI_IO_KEY is not set`.** The secret isn't
reaching the job. The workflow now says this in the run summary rather than burying
it. Each of these looks identical from the job's side — an empty value:

- Saved under the **Variables** tab instead of **Secrets** (same page, adjacent tabs)
- Saved as an **Environment** secret instead of a **Repository** secret
- Name isn't exactly `TWITTERAPI_IO_KEY`

**An issue was filed but no email arrived.** The issue is authored by
`github-actions[bot]`, i.e. someone else's activity. GitHub's default
"Participating and @mentions" subscription sends nothing for that unless you are
mentioned or assigned, so reports can pile up unread. Filed issues now assign and
cc the repo owner (override with a `NOTIFY_USER` repo variable), which counts as
participating. If you still hear nothing, check that email is enabled under
[notification settings](https://github.com/settings/notifications) -- or set the
repo's Watch to **All Activity** as a belt-and-braces.

**Run fails with `could not find a list of users in the response`.** The API's field
names changed. The error prints the top-level keys and a truncated response body;
`--probe` shows the full shape. Add the new field name to the tuple in `fetch_page`.

**Scheduled run lands ~20-30 minutes late.** Normal — GitHub's cron is best-effort
and deprioritised under load. It does not skip, it just slips.

**A follow shows on his profile before you get an issue.** Expected, for up to 24h:
the tracker only sees a follow on its next scheduled run. Ordering was verified
against the live API on 2026-08-20 — page 1 came back in exact reverse-chronological
order matching every detection on record — so the fast path is not missing follows,
it is sampling once a day. Raise the cron if that lag matters, or hit *Run workflow*
for an immediate check.

## Notes

- **Follows are only visible while the list is public.** If a tracked account goes
  protected or the endpoint stops returning ordered results, the check will go quiet
  rather than fail loudly — worth a manual `--full` run occasionally to
  confirm it's still tracking.
- **Scheduled workflows are disabled after 60 days of repository inactivity.** This
  one commits a `last_checked` timestamp on every run, which counts as activity and
  keeps the schedule alive indefinitely.
- Ordering by follow date is twitterapi.io's documented behaviour, not a guarantee
  from X, though it was confirmed correct against the live list on 2026-08-20. The
  occasional `--full` resync corrects anything the fast path might miss, since the
  baseline is what determines what counts as new.
