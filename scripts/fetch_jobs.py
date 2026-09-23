#!/usr/bin/env python3
"""Build the Job Command Center feed.

Pulls postings from public job-board APIs (Greenhouse, Lever, Ashby, Workday,
Remotive, and Adzuna/Jooble when their API keys are set), scores each posting
against the keyword profile in config/search.json, and writes the jobs.json
that index.html reads. GitHub Actions runs it hourly.

Only the standard library is used, so there is nothing to install.

    python scripts/fetch_jobs.py --previous jobs.json --out jobs.json
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

USER_AGENT = "JobCommandCenter/1.0 (+https://github.com/saradhi0003/Master)"

TYPE_LABELS = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "workday": "Workday",
    "remotive": "Remotive",
    "adzuna": "Adzuna",
    "jooble": "Jooble",
}
# Company career sites win over aggregator copies of the same posting.
ATS_TYPES = {"greenhouse", "lever", "ashby", "workday"}


class SourceError(Exception):
    """A source could not be fetched; its jobs from the previous run are kept."""


class SourceSkipped(Exception):
    """A source is not configured to run (for example, a missing API key)."""


# --------------------------------------------------------------------------- #
# HTTP

class Http:
    def __init__(self, timeout: float = 25, retries: int = 1):
        self.timeout = timeout
        self.retries = retries

    def get(self, url, headers=None):
        return self._request(url, None, headers)

    def post(self, url, body, headers=None):
        return self._request(url, body, headers)

    def _request(self, url, body, headers):
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        last = "request failed"
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, headers=hdrs,
                                         method="POST" if data is not None else "GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise SourceError("404 not found: check the board or site name") from None
                if e.code != 429 and e.code < 500:
                    raise SourceError(f"HTTP {e.code}") from None
                last = f"HTTP {e.code}"
            except urllib.error.URLError as e:
                last = f"network error: {e.reason}"
            except (TimeoutError, OSError) as e:
                last = f"network error: {e}"
            except json.JSONDecodeError:
                last = "response was not JSON"
            if attempt < self.retries:
                time.sleep(3 * (attempt + 1))
        # Never echo the URL: Adzuna and Jooble carry API keys in it.
        raise SourceError(last)


# --------------------------------------------------------------------------- #
# Text helpers

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def html_to_text(value) -> str:
    if not value:
        return ""
    s = html.unescape(str(value))  # Greenhouse sends entity-escaped HTML
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value) -> datetime | None:
    """ISO-8601 text or epoch seconds/millis -> aware UTC datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def workday_posted(text, now: datetime) -> datetime | None:
    """Workday only says "Posted Today" / "Posted 3 Days Ago" / "Posted 30+ Days Ago"."""
    t = (text or "").lower()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "today" in t:
        return day
    if "yesterday" in t:
        return day - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*days?", t)
    if m:
        return day - timedelta(days=int(m.group(1)))
    return None


def money_range(lo, hi, interval="", currency="USD", estimated=False) -> str:
    def fmt(v):
        if v in (None, ""):
            return None
        v = float(v)
        return f"${v / 1000:.0f}K" if v >= 1000 else f"${v:.0f}"

    parts = [p for p in (fmt(lo), fmt(hi)) if p]
    if not parts:
        return ""
    s = "–".join(dict.fromkeys(parts))
    if "hour" in (interval or "").lower():
        s += "/hr"
    if currency and currency != "USD":
        s += f" {currency}"
    return f"~{s} (est.)" if estimated else s


_AMOUNT = r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?\s?[kK]?"
_SALARY_RE = re.compile(
    _AMOUNT + r"\s?(?:-|–|—|to)\s?\$?\s?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?\s?[kK]?"
    r"(?:\s?(?:/|per\s)\s?(?:hr|hour|year|yr|annum))?",
    re.I,
)


def extract_salary(text: str) -> str:
    """First pay range in free text, e.g. "$145,000 - $165,000" or "$80-90/hr"."""
    for m in _SALARY_RE.finditer(text or ""):
        s = m.group(0)
        nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", s)]
        hourly = re.search(r"hr|hour", s, re.I)
        if re.search(r"\d\s?[kK]", s) or hourly or max(nums) >= 20000:
            return _WS_RE.sub(" ", s).strip()
    return ""


# --------------------------------------------------------------------------- #
# Scoring

def _compile(pattern: str) -> re.Pattern:
    # Letter/digit lookarounds instead of \b so "sr." and "c++"-style terms work,
    # while "rag" still does not match inside "storage".
    return re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % pattern, re.I)


class Scorer:
    def __init__(self, cfg: dict):
        self.min_score = int(cfg.get("min_score", 30))
        self.remote_bonus = int(cfg.get("remote_bonus", 0))
        self.require_any = [_compile(p) for p in cfg.get("require_any", [])]
        self.exclude_title = [_compile(p) for p in cfg.get("exclude_title", [])]
        self.keywords = [(k["label"], _compile(k["pattern"]), int(k["weight"]))
                         for k in cfg.get("keywords", [])]
        self.title_keywords = [(k["label"], _compile(k["pattern"]), int(k["weight"]))
                               for k in cfg.get("title_keywords", [])]

    def score(self, job: dict):
        """Return (score, matched labels) or None when the job is filtered out."""
        title = job.get("title", "")
        text = " ".join([title, job.get("location", ""), job.get("description", "")])
        if any(rx.search(title) for rx in self.exclude_title):
            return None
        gate = f"{text} {job.get('company', '')}"
        if self.require_any and not any(rx.search(gate) for rx in self.require_any):
            return None
        total, matched = 0, []
        for label, rx, weight in self.keywords:
            if rx.search(text):
                total += weight
                matched.append(label)
        for label, rx, weight in self.title_keywords:
            if rx.search(title):
                total += weight
        if job.get("remote"):
            total += self.remote_bonus
        return min(100, total), matched

    def snippet(self, description: str, width: int = 280) -> str:
        if not description:
            return ""
        for _, rx, _ in sorted(self.keywords, key=lambda k: -k[2]):
            m = rx.search(description)
            if m:
                start = max(0, m.start() - 110)
                if start:
                    start = description.find(" ", start) + 1 or start
                chunk = description[start:start + width]
                if start + width < len(description):
                    chunk = chunk.rsplit(" ", 1)[0] + "…"
                return ("…" if start else "") + chunk
        return description[:width].rsplit(" ", 1)[0] + ("…" if len(description) > width else "")


# --------------------------------------------------------------------------- #
# Source adapters. Each returns a list of raw postings:
#   {native_id, title, company, url, location, description, posted_at,
#    salary?, remote?, snippet?}

def fetch_greenhouse(src, http, now):
    board = src["board"]
    data = http.get(f"https://boards-api.greenhouse.io/v1/boards/"
                    f"{urllib.parse.quote(board)}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        desc = html_to_text(j.get("content"))
        out.append({
            "native_id": j.get("id"),
            "title": j.get("title", ""),
            "company": src.get("name") or j.get("company_name") or board,
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "description": desc,
            "posted_at": parse_date(j.get("first_published") or j.get("updated_at")),
            "salary": extract_salary(desc),
        })
    return out


def fetch_lever(src, http, now):
    board = src["board"]
    data = http.get(f"https://api.lever.co/v0/postings/{urllib.parse.quote(board)}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        lists = " ".join(f"{l.get('text', '')} {html_to_text(l.get('content'))}"
                         for l in j.get("lists") or [])
        desc = _WS_RE.sub(" ", " ".join([j.get("descriptionPlain", ""), lists,
                                         j.get("additionalPlain", "")])).strip()
        sal = j.get("salaryRange") or {}
        salary = (money_range(sal.get("min"), sal.get("max"), sal.get("interval"),
                              sal.get("currency")) if sal else "") or extract_salary(desc)
        out.append({
            "native_id": j.get("id"),
            "title": j.get("text", ""),
            "company": src.get("name") or board,
            "url": j.get("hostedUrl", ""),
            "location": cats.get("location", ""),
            "description": desc,
            "posted_at": parse_date(j.get("createdAt")),
            "salary": salary,
            "remote": True if (j.get("workplaceType") or "").lower() == "remote" else None,
        })
    return out


def fetch_ashby(src, http, now):
    board = src["board"]
    data = http.get(f"https://api.ashbyhq.com/posting-api/job-board/"
                    f"{urllib.parse.quote(board)}?includeCompensation=true")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = j.get("compensation") or {}
        desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
        remote = j.get("isRemote") or (j.get("workplaceType") or "").lower() == "remote"
        out.append({
            "native_id": j.get("id") or j.get("jobUrl"),
            "title": j.get("title", ""),
            "company": src.get("name") or board,
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "location": j.get("location", ""),
            "description": desc,
            "posted_at": parse_date(j.get("publishedAt")),
            "salary": (comp.get("scrapeableCompensationSalarySummary")
                       or comp.get("compensationTierSummary") or extract_salary(desc)),
            "remote": True if remote else None,
        })
    return out


def fetch_workday(src, http, now):
    """Workday's public career-site search (the JSON behind *.myworkdayjobs.com).

    The search endpoint returns titles and locations only. Its full-text search
    already matched the query inside each posting, so the matched queries stand
    in for the description when scoring. That keeps it to a handful of requests
    per run instead of one per posting.
    """
    host, tenant, site = src["host"], src["tenant"], src["site"]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    cap = int(src.get("max_per_query", 40))
    found: dict[str, dict] = {}
    hits: dict[str, list[str]] = {}
    for query in src.get("queries") or [""]:
        offset = 0
        while offset < cap:
            data = http.post(f"{base}/jobs", {"appliedFacets": {}, "limit": 20,
                                              "offset": offset, "searchText": query})
            posts = data.get("jobPostings") or []
            for p in posts:
                path = p.get("externalPath")
                if not path:
                    continue
                found.setdefault(path, p)
                hits.setdefault(path, [])
                if query and query not in hits[path]:
                    hits[path].append(query)
            offset += 20
            total = data.get("total") or 0
            if len(posts) < 20 or (total and offset >= total):
                break
    label = src.get("name") or tenant
    out = []
    for path, p in found.items():
        bullets = p.get("bulletFields") or []
        queries = hits.get(path, [])
        out.append({
            "native_id": bullets[0] if bullets else path,
            "title": p.get("title", ""),
            "company": src.get("company") or label,
            "url": f"https://{host}/en-US/{site}{path}",
            "location": p.get("locationsText", ""),
            "description": " ".join(queries),
            "posted_at": workday_posted(p.get("postedOn"), now),
            "snippet": f"Matched {label} search for: {', '.join(queries)}" if queries else "",
        })
    return out


def fetch_remotive(src, http, now):
    out, seen = [], set()
    for query in src.get("queries") or ["salesforce"]:
        qs = urllib.parse.urlencode({"search": query, "limit": src.get("limit", 100)})
        data = http.get(f"https://remotive.com/api/remote-jobs?{qs}")
        for j in data.get("jobs", []):
            if j.get("id") in seen:
                continue
            seen.add(j.get("id"))
            desc = html_to_text(j.get("description"))
            out.append({
                "native_id": j.get("id"),
                "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "url": j.get("url", ""),
                "location": j.get("candidate_required_location") or "Remote",
                "description": desc,
                "posted_at": parse_date(j.get("publication_date")),
                "salary": j.get("salary") or extract_salary(desc),
                "remote": True,
            })
    return out


def fetch_adzuna(src, http, now):
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        raise SourceSkipped("add ADZUNA_APP_ID and ADZUNA_APP_KEY repo secrets to enable")
    country = src.get("country", "us")
    out, seen = [], set()
    for query in src.get("queries") or []:
        params = {
            "app_id": app_id, "app_key": app_key, "what": query,
            "results_per_page": 50, "max_days_old": src.get("max_days_old", 14),
            "sort_by": "date", "content-type": "application/json",
        }
        if src.get("where"):
            params["where"] = src["where"]
        data = http.get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1?"
                        + urllib.parse.urlencode(params))
        for j in data.get("results", []):
            if j.get("id") in seen:
                continue
            seen.add(j.get("id"))
            desc = html_to_text(j.get("description"))
            predicted = str(j.get("salary_is_predicted")) == "1"
            out.append({
                "native_id": j.get("id"),
                "title": html_to_text(j.get("title")),
                "company": (j.get("company") or {}).get("display_name", ""),
                "url": j.get("redirect_url", ""),
                "location": (j.get("location") or {}).get("display_name", ""),
                "description": desc,
                "posted_at": parse_date(j.get("created")),
                "salary": money_range(j.get("salary_min"), j.get("salary_max"),
                                      estimated=predicted) or extract_salary(desc),
            })
    return out


def fetch_jooble(src, http, now):
    key = os.environ.get("JOOBLE_API_KEY")
    if not key:
        raise SourceSkipped("add a JOOBLE_API_KEY repo secret to enable")
    out, seen = [], set()
    for query in src.get("queries") or []:
        data = http.post(f"https://jooble.org/api/{key}",
                         {"keywords": query, "location": src.get("location", "")})
        for j in data.get("jobs", []):
            jid = j.get("id") or j.get("link")
            if jid in seen:
                continue
            seen.add(jid)
            desc = html_to_text(j.get("snippet"))
            out.append({
                "native_id": jid,
                "title": html_to_text(j.get("title")),
                "company": j.get("company", ""),
                "url": j.get("link", ""),
                "location": j.get("location", ""),
                "description": desc,
                "posted_at": parse_date(j.get("updated")),
                "salary": j.get("salary") or extract_salary(desc),
            })
    return out


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "remotive": fetch_remotive,
    "adzuna": fetch_adzuna,
    "jooble": fetch_jooble,
}


# --------------------------------------------------------------------------- #
# Feed assembly

def source_id(src: dict) -> str:
    if src.get("id"):
        return src["id"]
    kind = src["type"]
    if kind == "workday":
        return f"workday:{src['tenant']}/{src['site']}".lower()
    if src.get("board"):
        return f"{kind}:{src['board']}".lower()
    return f"{kind}:{re.sub(r'[^a-z0-9]+', '-', src.get('name', kind).lower()).strip('-')}"


_REMOTE_RE = re.compile(r"(?<![a-z])remote(?![a-z])", re.I)


def to_job(raw: dict, src: dict, sid: str, scorer: Scorer):
    raw["remote"] = bool(raw.get("remote")) or bool(
        _REMOTE_RE.search(f"{raw.get('location', '')} {raw.get('title', '')}"))
    result = scorer.score(raw)
    if result is None:
        return None
    score, matched = result
    if score < scorer.min_score:
        return None
    return {
        "id": f"{sid}:{raw['native_id']}",
        "title": raw.get("title", "").strip(),
        "company": (raw.get("company") or "").strip(),
        "url": raw.get("url", ""),
        "location": (raw.get("location") or "").strip(),
        "remote": raw["remote"],
        "salary": raw.get("salary") or "",
        "posted_at": iso(raw.get("posted_at")),
        "score": score,
        "matched": matched,
        "snippet": raw.get("snippet") or scorer.snippet(raw.get("description", "")),
        "source": src.get("name") or TYPE_LABELS.get(src["type"], src["type"]),
        "via": TYPE_LABELS.get(src["type"], src["type"]),
        "source_id": sid,
    }


def _norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|the|remote|hybrid|us|usa)\b", " ", s)
    return " ".join(s.split())


def dedupe(jobs: list[dict]) -> list[dict]:
    """Collapse the same role seen on several sources or city postings."""
    best: dict[tuple, dict] = {}
    for job in jobs:
        key = (_norm(job["company"]), _norm(job["title"]))
        cur = best.get(key)
        if cur is None:
            best[key] = job
            continue
        rank = lambda j: (j["score"], j["source_id"].split(":")[0] in ATS_TYPES)  # noqa: E731
        keep, drop = (job, cur) if rank(job) > rank(cur) else (cur, job)
        keep["first_seen"] = min(keep["first_seen"], drop["first_seen"])
        best[key] = keep
    return list(best.values())


def build_feed(config: dict, previous: dict, http, now: datetime, only=None):
    """Return (feed, attempted, failed)."""
    scoring = config.get("scoring", {})
    scorer = Scorer(scoring)
    prev_jobs = {j["id"]: j for j in previous.get("jobs", []) if j.get("id")}
    prev_sources = {s["id"]: s for s in previous.get("sources", []) if s.get("id")}

    collected: list[dict] = []
    statuses: list[dict] = []
    attempted = failed = 0

    for src in config.get("sources", []):
        if src.get("enabled", True) is False:
            continue
        sid = source_id(src)
        if only and src["type"] not in only and sid not in only:
            continue
        before = prev_sources.get(sid, {})
        carried = [j for j in prev_jobs.values() if j.get("source_id") == sid]
        status = {"id": sid, "name": src.get("name", sid),
                  "via": TYPE_LABELS.get(src["type"], src["type"])}

        # Sources with a slower cadence (rate-limited APIs) reuse their last good
        # result until it is due; the 10-minute slack absorbs cron jitter.
        every = float(src.get("every_hours", 1))
        last_ok = parse_date(before.get("fetched_at"))
        if every > 1 and last_ok and now - last_ok < timedelta(hours=every, minutes=-10):
            status.update(state="cached", fetched_at=before["fetched_at"],
                          found=before.get("found", 0), kept=len(carried),
                          message=f"refreshes every {every:g}h")
            collected.extend(carried)
            statuses.append(status)
            continue

        adapter = ADAPTERS.get(src["type"])
        try:
            if adapter is None:
                raise SourceError(f"unknown source type {src['type']!r}")
            raws = adapter(src, http, now)
        except SourceSkipped as e:
            status.update(state="skipped", message=str(e))
            statuses.append(status)
            continue
        except Exception as e:  # one bad source must not sink the whole feed
            attempted += 1
            failed += 1
            msg = str(e) if isinstance(e, SourceError) else f"{type(e).__name__}: {e}"
            status.update(state="error", message=msg, fetched_at=before.get("fetched_at"),
                          kept=len(carried))
            collected.extend(carried)
            statuses.append(status)
            continue

        attempted += 1
        kept = []
        seen = set()
        for raw in raws:
            if raw.get("native_id") in (None, "") or raw["native_id"] in seen:
                continue
            seen.add(raw["native_id"])
            job = to_job(raw, src, sid, scorer)
            if job:
                kept.append(job)
        status.update(state="ok", fetched_at=iso(now), found=len(raws), kept=len(kept))
        collected.extend(kept)
        statuses.append(status)

    stamp = iso(now)
    for job in collected:
        prior = prev_jobs.get(job["id"])
        job["first_seen"] = (prior or {}).get("first_seen") or job.get("first_seen") or stamp

    jobs = dedupe(collected)
    max_age = timedelta(days=int(scoring.get("max_age_days", 45)))
    jobs = [j for j in jobs
            if not j.get("posted_at") or now - parse_date(j["posted_at"]) <= max_age]
    jobs.sort(key=lambda j: (-j["score"], -(parse_date(j.get("posted_at")) or now).timestamp(),
                             j["title"]))
    jobs = jobs[: int(scoring.get("max_jobs", 300))]

    feed = {
        "schema": 1,
        "generated_at": stamp,
        "profile": config.get("profile", {}).get("headline", ""),
        "min_score": scorer.min_score,
        "sources": statuses,
        "jobs": jobs,
    }
    return feed, attempted, failed


def _load_json(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"warning: ignoring unreadable {path}: {e}", file=sys.stderr)
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default="config/search.json")
    ap.add_argument("--previous", help="last jobs.json: keeps first-seen dates and cached sources")
    ap.add_argument("--out", default="jobs.json")
    ap.add_argument("--only", help="comma-separated source types or ids to fetch")
    args = ap.parse_args(argv)

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)
    only = set(filter(None, (args.only or "").split(","))) or None
    feed, attempted, failed = build_feed(config, _load_json(args.previous), Http(),
                                         datetime.now(timezone.utc), only)

    for s in feed["sources"]:
        counts = f"found={s.get('found', '-')} kept={s.get('kept', '-')}"
        print(f"{s['state']:<8} {s['name']:<28} {counts:<22} {s.get('message', '')}")
    if attempted and failed == attempted:
        print("Every source failed; leaving the previous feed in place.", file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    tmp = f"{args.out}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, args.out)
    print(f"Wrote {len(feed['jobs'])} jobs to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
