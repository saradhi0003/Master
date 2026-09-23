# Job Command Center

A job-search dashboard for Salesforce Data 360 / Agentforce Technical Builder roles. It has three parts:

- **Pipeline:** a Kanban board (Saved → Applied → Recruiter Screen → Interview → Offer → Rejected) with match %, resume version, follow-up dates, notes, stats, and a banner when a follow-up is due within 48 hours.
- **Live feed:** a GitHub Action checks company career sites and job APIs **every hour, even when the page is closed**. It scores every posting against your profile and publishes the matches. The page re-reads the feed hourly while it's open.
- **Strategy:** the September 2026 market scan, with leads you can add to the pipeline in one click, salary benchmarks and quick-search links.

It's one static page (`index.html`) plus a Python script, with no server and no database. It lives in a private repo.

## Getting started

1. **Run the feed once.** Go to **Actions → Job feed → Run workflow**. After that it runs every hour by itself.
2. **Make a read-only token** so the dashboard can read the private feed:
   - Go to GitHub → Settings → Developer settings → [Fine-grained tokens](https://github.com/settings/personal-access-tokens/new).
   - Under **Repository access**, pick **Only select repositories** → `jobsearch`.
   - Under **Permissions**, set **Contents** to **Read-only**. Pick an expiry you're comfortable with.
3. **Open the dashboard.** Download `index.html` (or clone the repo) and open it in your browser. Go to **Settings**, paste the token and save. The Live feed fills in straight away and re-checks every hour while the page is open.

The token stays in that browser, is sent only to `api.github.com`, and is left out of exports. The page and your pipeline never leave your machine.

Your pipeline data (jobs, notes, follow-ups) stays in your browser's local storage and is never uploaded. Use **Export** to back it up or move it to another browser, and **Import** to load it back. Import also takes plain job-list JSON (an array, or `{"jobs": [...]}` with title/company/url/status fields), so an export from the earlier offline dashboard should load too.

## How the hourly feed works

```
GitHub Actions (every hour, :17)
  └─ scripts/fetch_jobs.py
       ├─ fetch   Workday (Salesforce careers, CrowdStrike), Greenhouse, Lever, Ashby,
       │          Remotive, plus Adzuna / Jooble when their keys are set
       ├─ score   keyword weights from config/search.json → 0–100
       ├─ filter  must mention Salesforce/Agentforce, US/remote only, drops sales and
       │          non-builder titles, min score 30
       └─ write   jobs.json → force-pushed as one commit to the `job-feed` branch
                            (the dashboard reads it through the GitHub API)
```

- **First-seen dates** carry over between runs, so the dashboard can flag what's **NEW** since you last looked.
- **Sources fail independently.** If a board is down, its jobs from the last good run are kept and the **Sources** panel in the Live feed tab shows the error.
- **Rate-limited sources** such as Remotive run on their own cadence (`every_hours`) and are served from cache in between.
- **Postings drop out** when they're removed from the source or are older than 45 days.
- **LinkedIn, Indeed, Dice and BuiltIn** have no public API and don't allow scraping, so the Strategy tab links to saved searches for them instead.

### Adding a company

Look at the company's careers URL, take the board name from it, and add a line to `sources` in `config/search.json`:

| Careers URL looks like | Add |
| --- | --- |
| `boards.greenhouse.io/<board>` or `job-boards.greenhouse.io/<board>` | `{"type": "greenhouse", "name": "Acme", "board": "<board>"}` |
| `jobs.lever.co/<board>` | `{"type": "lever", "name": "Acme", "board": "<board>"}` |
| `jobs.ashbyhq.com/<board>` | `{"type": "ashby", "name": "Acme", "board": "<board>"}` |
| `<tenant>.wd5.myworkdayjobs.com/<site>` | `{"type": "workday", "name": "Acme careers", "host": "<tenant>.wd5.myworkdayjobs.com", "tenant": "<tenant>", "site": "<site>", "queries": ["Agentforce", "Salesforce"]}` |

After adding a board, check the **Sources** panel once the next run finishes. `404 not found` means the board name is wrong or the company uses a different ATS. Big SIs such as Deloitte, Accenture and Capgemini run their own career systems and can't be pulled this way.

A source can also carry a `"bonus"` that is added to every posting it returns. The Salesforce careers source uses `"bonus": 15`, so a plain "Technical Architect" at Salesforce still makes the feed.

### Optional: wider coverage with Adzuna and Jooble

Both aggregators have free API keys and index many boards that don't have public APIs:

- **Adzuna:** sign up at [developer.adzuna.com](https://developer.adzuna.com/), then add repo secrets `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`.
- **Jooble:** request a key at [jooble.org/api/about](https://jooble.org/api/about), then add a repo secret `JOOBLE_API_KEY`.

Secrets live under **Settings → Secrets and variables → Actions**. Until the keys are set, these sources show as "not configured".

### Tuning the scoring

`config/search.json → scoring` holds:

- `keywords`: patterns matched anywhere in the posting, each with a weight.
- `title_keywords`: extra weight when a pattern appears in the title.
  Negative weights push roles down, for example support, QA, other platforms and executive titles.
- `require_any`: at least one of these must appear, or the posting is dropped.
- `exclude_title`: postings whose title matches are dropped.
- `exclude_locations` / `keep_locations`: postings whose location names a non-US country or city are dropped unless it also names the US or Austin. Empty the list to search globally.
- `remote_bonus`, `min_score`, `max_age_days` and `max_jobs`.

**Company boilerplate is discounted.** When a keyword shows up in most of one board's postings, it's treated as the company blurb. For example, every NeuraFlash posting mentions Agentforce. That keyword then counts for a quarter of its weight, unless the posting's title names it.

Patterns are case-insensitive regular expressions matched as whole words, so `rag` doesn't match "storage".

## Local development

```bash
python -m unittest discover -s tests -v              # offline tests, standard library only
python scripts/fetch_jobs.py --out data/jobs.json    # build a real feed locally (needs internet)
python -m http.server 8000                           # then open http://localhost:8000
```

When `data/jobs.json` exists, the page uses it. Otherwise it falls back to the published feed.

## Notes

- **Actions minutes.** Private repos draw on your account's free Actions minutes: 2,000 a month on GitHub Free. The hourly run takes under a minute, but GitHub bills each run as at least one minute, so it uses roughly 730 minutes a month.
- **Hosting the page online is optional.** GitHub Pages on a private repo needs a paid plan, and the published site is public except on Enterprise. If you ever set **Settings → Pages → Source** to **GitHub Actions**, the hourly workflow deploys the dashboard and feed there automatically.
- **Scheduled workflows can be paused.** GitHub pauses scheduled workflows after 60 days without repository activity. If the feed stops updating, re-enable **Job feed** in the Actions tab.
- **Salesforce and CrowdStrike use Workday,** whose search results only include titles, so those postings are scored on their title alone.
