# Follow tracker

Files a GitHub issue whenever [@eladgil](https://x.com/eladgil) follows someone new,
with the new account's handle, name, bio and follower count.

Runs on GitHub Actions once a day. No server, no extra accounts, and the API bill
is a fraction of a cent per day.

## How it works

`twitterapi.io`'s followings endpoint returns results **sorted by follow date,
newest first**, 200 per page. That's the whole trick: new follows always appear at
the top, so a daily check only needs page 1 — not a crawl of all ~N thousand
accounts he follows.

1. **First run** crawls the complete following list once and saves every account ID
   to `state/following.json`. This baseline is what makes the cheap daily check
   correct — see the note below. No issue is filed on this run.
2. **Every run after** fetches just page 1 and reports any ID missing from the
   baseline. If page 1 comes back *entirely* new (he followed 200+ accounts in a
   day), it keeps paging until it hits familiar territory, up to 5 pages.
3. New follows are appended to `state/new_follows.jsonl` as a permanent history,
   and the updated state is committed back to the repo.

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
   repository secret, named `TWITTERAPI_IO_KEY`.
3. **Merge this to your default branch.** GitHub only runs `schedule:` workflows
   from the default branch — on a side branch the cron will never fire.
4. **Run it once manually:** Actions → *Track new follows* → Run workflow. This
   seeds the baseline (takes a minute or two, one API call per 200 accounts).

That's it — from then on it runs itself at 14:07 UTC daily and opens an issue when
there's something to report. GitHub emails you on new issues in your own repo by
default; check your [notification settings](https://github.com/settings/notifications)
if you don't get them.

### Tracking someone else instead

Add a repository **variable** (not secret) called `TARGET_USERNAME` set to any
handle. Then delete `state/` and re-run to seed a fresh baseline.

## Cost

| | |
|---|---|
| GitHub Actions | Free (public repo; ~1 min/day of private-repo minutes otherwise) |
| Daily check | 1 API call, 200 profiles — well under a cent |
| One-time seed | ~1 call per 200 accounts followed |

twitterapi.io bills per profile returned, roughly $1 per 100,000 profiles. A daily
check is on the order of **a few cents a year**. The seed crawl is the only
meaningful spend and happens once.

`MAX_PAGES_INCREMENTAL` (default 5) hard-caps how many pages a single daily run can
fetch, so a bug or an upstream change can't quietly run up a bill.

## Running locally

```bash
export TWITTERAPI_IO_KEY=...
python track.py --dry-run      # print what would be filed, touch nothing
python track.py --full         # re-crawl everything; also prunes unfollows
```

`--dry-run` needs no GitHub credentials. Without `GITHUB_TOKEN` the script prints
the issue body instead of filing it.

## Notes

- **Follows are only visible if his following list is public.** If he protects his
  account or the endpoint stops returning ordered results, the daily check will go
  quiet rather than fail loudly — worth a manual `--full` run occasionally to
  confirm it's still tracking.
- **Scheduled workflows are disabled after 60 days of repository inactivity.** This
  one commits a `last_checked` timestamp on every run, which counts as activity and
  keeps the schedule alive indefinitely.
- Ordering by follow date is twitterapi.io's documented behaviour, not a guarantee
  from X. The occasional `--full` resync corrects anything the fast path missed,
  since the baseline is what determines what counts as new.
