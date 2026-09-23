# Job Command Center

A job-search dashboard for Salesforce Data 360 / Agentforce Technical Builder roles. It has three parts:

- **Pipeline:** a Kanban board (Saved → Applied → Recruiter Screen → Interview → Offer → Rejected) with match %, resume version, follow-up dates, notes, stats, and a banner when a follow-up is due within 48 hours.
- **Live feed:** a GitHub Action checks company career sites and job APIs **every hour, even when the page is closed**. It scores every posting against your profile and publishes the matches. The page re-reads the feed hourly while it's open.
- **Strategy:** the September 2026 market scan, with leads you can add to the pipeline in one click, salary benchmarks and quick-search links.

It's one static page (`index.html`) plus a Python script, with no server, no database and no monthly cost.

## Getting started

1. **Turn on hosting.** In the repo, go to **Settings → Pages → Build and deployment → Source** and pick **GitHub Actions**.
2. **Run the feed once.** Go to **Actions → Job feed → Run workflow**. After that it runs every hour by itself.
3. **Open the dashboard** at `https://saradhi0003.github.io/Master/`. You can also open `index.html` straight from disk, which reads the same hourly feed from the `job-feed` branch.

Your pipeline data (jobs, notes, follow-ups) stays in your browser's local storage and is never uploaded. Use **Export** to back it up or move it to another browser, and **Import** to load it back. Import also takes plain job-list JSON (an array, or `{"jobs": [...]}` with title/company/url/status fields), so an export from the earlier offline dashboard should load too.

## How the hourly feed works

```
GitHub Actions (every hour, :17)
  └─ scripts/fetch_jobs.py
       ├─ fetch   Workday (Salesforce careers, CrowdStrike), Greenhouse, Lever, Ashby,
       │          Remotive, plus Adzuna / Jooble when their keys are set
       ├─ score   keyword weights from config/search.json → 0–100
       ├─ filter  must mention Salesforce/Agentforce, drops sales/recruiting titles, min score 30
       └─ write   jobs.json → force-pushed as one commit to the `job-feed` branch
                            → deployed with index.html to GitHub Pages
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

The Salesforce partner boards in the config (NeuraFlash, Spaulding Ridge, Certinia, nCino, Copado, Veeva) are best guesses at their board names. After the first run, check the **Sources** panel. A partner showing `404 not found` either uses a different board name or doesn't use that ATS, so fix the name or delete the line. Big SIs such as Deloitte, Accenture and Capgemini run their own career systems and can't be pulled this way.

### Optional: wider coverage with Adzuna and Jooble

Both aggregators have free API keys and index many boards that don't have public APIs:

- **Adzuna:** sign up at [developer.adzuna.com](https://developer.adzuna.com/), then add repo secrets `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`.
- **Jooble:** request a key at [jooble.org/api/about](https://jooble.org/api/about), then add a repo secret `JOOBLE_API_KEY`.

Secrets live under **Settings → Secrets and variables → Actions**. Until the keys are set, these sources show as "not configured".

### Tuning the scoring

`config/search.json → scoring` holds:

- `keywords`: patterns matched anywhere in the posting, each with a weight.
- `title_keywords`: extra weight when a pattern appears in the title.
- `require_any`: at least one of these must appear, or the posting is dropped.
- `exclude_title`: postings whose title matches are dropped.
- `remote_bonus`, `min_score`, `max_age_days` and `max_jobs`.

Patterns are case-insensitive regular expressions matched as whole words, so `rag` doesn't match "storage".

## Local development

```bash
python -m unittest discover -s tests -v              # offline tests, standard library only
python scripts/fetch_jobs.py --out data/jobs.json    # build a real feed locally (needs internet)
python -m http.server 8000                           # then open http://localhost:8000
```

When `data/jobs.json` exists, the page uses it. Otherwise it falls back to the published feed.

## Notes

- **The repo is public,** so the Pages site, `index.html` and the job feed are public too. They only contain public postings and the market scan, with your name and current client left out. Your pipeline never leaves your browser.
- **GitHub pauses scheduled workflows** in public repos after 60 days without repository activity. If the feed stops updating, re-enable **Job feed** in the Actions tab.
- **Salesforce and CrowdStrike use Workday,** whose search results only include titles. Those postings are scored on their title plus the search terms that found them.
