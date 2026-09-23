"""Offline tests for scripts/fetch_jobs.py. Run: python -m unittest discover -s tests"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_jobs as fj  # noqa: E402

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)
CONFIG = json.loads((ROOT / "config" / "search.json").read_text())


class FakeHttp:
    """Serves canned payloads by URL prefix and records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _match(self, url, body):
        self.calls.append((url, body))
        for prefix, payload in self.routes.items():
            if url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                return payload(url, body) if callable(payload) else payload
        raise fj.SourceError("404 not found: check the board or site name")

    def get(self, url, headers=None):
        return self._match(url, None)

    def post(self, url, body, headers=None):
        return self._match(url, body)


GREENHOUSE = {"jobs": [
    {"id": 101, "title": "Salesforce Agentforce Architect",
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
     "location": {"name": "Remote - US"}, "first_published": "2026-09-20T12:00:00-04:00",
     "content": "&lt;p&gt;Design &lt;b&gt;Agentforce&lt;/b&gt; agents on Data Cloud with Apex, "
                "LWC and RAG over the Data Library.&lt;/p&gt;&lt;p&gt;Pay: $150,000 - $185,000 "
                "per year.&lt;/p&gt;"},
    {"id": 102, "title": "Account Executive, Agentforce",
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/102",
     "location": {"name": "Austin, TX"}, "content": "Sell Agentforce to Salesforce customers."},
    {"id": 103, "title": "Backend Engineer, Storage",
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/103",
     "location": {"name": "Remote"}, "content": "Go, distributed storage, Kubernetes."},
]}

LEVER = [
    {"id": "lv-1", "text": "Senior Salesforce Developer (Data Cloud)",
     "hostedUrl": "https://jobs.lever.co/veeva/lv-1",
     "categories": {"location": "Austin, TX"}, "createdAt": 1789912800000,
     "workplaceType": "remote", "descriptionPlain": "Build on Salesforce Data Cloud with Apex.",
     "lists": [{"text": "Requirements", "content": "<li>LWC</li><li>Python a plus</li>"}],
     "salaryRange": {"min": 140000, "max": 170000, "currency": "USD",
                     "interval": "per-year-salary"}},
]

ASHBY = {"jobs": [
    {"id": "ab-1", "title": "Salesforce Engineer, GTM Systems", "location": "San Francisco",
     "isRemote": True, "isListed": True, "jobUrl": "https://jobs.ashbyhq.com/openai/ab-1",
     "publishedAt": "2026-09-22T10:00:00.000+00:00",
     "descriptionPlain": "Own our Salesforce org: Apex, LWC, Agentforce pilots and MCP tooling.",
     "compensation": {"scrapeableCompensationSalarySummary": "$230K – $310K"}},
    {"id": "ab-2", "title": "Salesforce Engineer (unlisted)", "isListed": False,
     "jobUrl": "https://jobs.ashbyhq.com/openai/ab-2", "descriptionPlain": "Salesforce"},
]}


def workday_search(url, body):
    posts = {
        "Agentforce": [
            {"title": "Lead Demo Engineer, Agentforce", "externalPath": "/job/SF/Lead-Demo_JR1",
             "locationsText": "California - San Francisco", "postedOn": "Posted 2 Days Ago",
             "bulletFields": ["JR1"]},
            {"title": "Forward Deployed Engineer, Agentforce", "externalPath": "/job/Remote/FDE_JR2",
             "locationsText": "Remote - USA", "postedOn": "Posted Today", "bulletFields": ["JR2"]},
        ],
        "Data Cloud": [
            {"title": "Forward Deployed Engineer, Agentforce", "externalPath": "/job/Remote/FDE_JR2",
             "locationsText": "Remote - USA", "postedOn": "Posted Today", "bulletFields": ["JR2"]},
        ],
    }
    found = posts.get(body["searchText"], [])
    return {"total": len(found), "jobPostings": found[body["offset"]:body["offset"] + 20]}


REMOTIVE = {"jobs": [
    {"id": 9, "url": "https://remotive.com/remote-jobs/9", "title": "Salesforce Data Cloud Architect",
     "company_name": "Apptad", "publication_date": "2026-09-21T08:00:00",
     "candidate_required_location": "USA", "salary": "$65-70/hr",
     "description": "<p>Hands-on Data Cloud architecture for Salesforce clients.</p>"},
]}


def routes():
    return {
        "https://boards-api.greenhouse.io/v1/boards/acme/": GREENHOUSE,
        "https://api.lever.co/v0/postings/veeva": LEVER,
        "https://api.ashbyhq.com/posting-api/job-board/openai": ASHBY,
        "https://salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/External_Career_Site/jobs":
            workday_search,
        "https://remotive.com/api/remote-jobs": REMOTIVE,
    }


CONFIG_SOURCES = [
    {"type": "greenhouse", "name": "Acme", "board": "acme"},
    {"type": "lever", "name": "Veeva", "board": "veeva"},
    {"type": "ashby", "name": "OpenAI", "board": "openai"},
    {"type": "workday", "name": "Salesforce careers", "company": "Salesforce",
     "host": "salesforce.wd12.myworkdayjobs.com", "tenant": "salesforce",
     "site": "External_Career_Site", "queries": ["Agentforce", "Data Cloud"]},
    {"type": "remotive", "name": "Remotive", "queries": ["salesforce"], "every_hours": 6},
    {"type": "adzuna", "name": "Adzuna US", "queries": ["agentforce"]},
]


def config(**overrides):
    cfg = {"profile": CONFIG["profile"], "scoring": dict(CONFIG["scoring"]),
           "sources": CONFIG_SOURCES}
    cfg.update(overrides)
    return cfg


def by_title(feed):
    return {j["title"]: j for j in feed["jobs"]}


@mock.patch.dict(os.environ, {"ADZUNA_APP_ID": "", "ADZUNA_APP_KEY": ""})
class BuildFeedTest(unittest.TestCase):
    def build(self, previous=None, http=None, now=NOW, cfg=None):
        return fj.build_feed(cfg or config(), previous or {}, http or FakeHttp(routes()), now)

    def test_scores_and_filters_across_sources(self):
        feed, attempted, failed = self.build()
        self.assertEqual((attempted, failed), (5, 0))
        jobs = by_title(feed)

        self.assertNotIn("Account Executive, Agentforce", jobs)  # excluded title
        self.assertNotIn("Backend Engineer, Storage", jobs)  # never mentions Salesforce
        self.assertNotIn("Salesforce Engineer (unlisted)", jobs)

        arch = jobs["Salesforce Agentforce Architect"]
        self.assertTrue(arch["remote"])
        self.assertEqual(arch["salary"], "$150,000 - $185,000 per year")
        self.assertEqual(arch["posted_at"], "2026-09-20T16:00:00Z")
        self.assertIn("Agentforce", arch["matched"])
        self.assertIn("RAG / vector search", arch["matched"])
        self.assertEqual(arch["id"], "greenhouse:acme:101")
        self.assertEqual(arch["first_seen"], "2026-09-23T15:00:00Z")
        self.assertEqual(feed["jobs"][0]["title"], arch["title"])  # best match sorts first

        dev = jobs["Senior Salesforce Developer (Data Cloud)"]
        self.assertEqual(dev["salary"], "$140K–$170K")
        self.assertTrue(dev["remote"])  # from Lever's workplaceType
        self.assertIn("LWC", dev["matched"])

        self.assertEqual(jobs["Salesforce Engineer, GTM Systems"]["salary"], "$230K – $310K")
        self.assertEqual(jobs["Salesforce Data Cloud Architect"]["salary"], "$65-70/hr")

    def test_workday_merges_queries_and_dates(self):
        feed, _, _ = self.build()
        fde = by_title(feed)["Forward Deployed Engineer, Agentforce"]
        self.assertEqual(fde["company"], "Salesforce")
        self.assertEqual(fde["url"], "https://salesforce.wd12.myworkdayjobs.com/en-US/"
                                     "External_Career_Site/job/Remote/FDE_JR2")
        self.assertEqual(fde["snippet"],
                         "Matched Salesforce careers search for: Agentforce, Data Cloud")
        self.assertEqual(fde["posted_at"], "2026-09-23T00:00:00Z")
        self.assertIn("Data 360 / Data Cloud", fde["matched"])
        demo = by_title(feed)["Lead Demo Engineer, Agentforce"]
        self.assertEqual(demo["posted_at"], "2026-09-21T00:00:00Z")
        self.assertFalse(demo["remote"])

    def test_missing_keys_skip_adzuna(self):
        feed, _, _ = self.build()
        adzuna = next(s for s in feed["sources"] if s["id"] == "adzuna:adzuna-us")
        self.assertEqual(adzuna["state"], "skipped")
        self.assertIn("ADZUNA_APP_ID", adzuna["message"])

    def test_first_seen_survives_and_failed_source_keeps_jobs(self):
        first, _, _ = self.build()
        later = NOW + timedelta(hours=1)
        broken = routes()
        broken["https://api.lever.co/v0/postings/veeva"] = fj.SourceError("HTTP 503")
        second, attempted, failed = self.build(previous=first, http=FakeHttp(broken), now=later)

        self.assertEqual(failed, 1)
        jobs = by_title(second)
        self.assertEqual(jobs["Salesforce Agentforce Architect"]["first_seen"],
                         "2026-09-23T15:00:00Z")
        self.assertIn("Senior Salesforce Developer (Data Cloud)", jobs)  # carried over
        lever = next(s for s in second["sources"] if s["id"] == "lever:veeva")
        self.assertEqual((lever["state"], lever["message"]), ("error", "HTTP 503"))

    def test_slow_sources_are_cached_until_due(self):
        first, _, _ = self.build()
        http = FakeHttp(routes())
        second, _, _ = self.build(previous=first, http=http, now=NOW + timedelta(hours=2))
        self.assertFalse(any("remotive" in url for url, _ in http.calls))
        self.assertIn("Salesforce Data Cloud Architect", by_title(second))
        remotive = next(s for s in second["sources"] if s["id"] == "remotive:remotive")
        self.assertEqual(remotive["state"], "cached")

        http = FakeHttp(routes())
        self.build(previous=first, http=http, now=NOW + timedelta(hours=6))
        self.assertTrue(any("remotive" in url for url, _ in http.calls))

    def test_unexpected_payload_is_reported_not_fatal(self):
        odd = routes()
        odd["https://api.ashbyhq.com/posting-api/job-board/openai"] = {"jobs": [None]}
        feed, attempted, failed = self.build(http=FakeHttp(odd))
        self.assertEqual(failed, 1)
        ashby = next(s for s in feed["sources"] if s["id"] == "ashby:openai")
        self.assertTrue(ashby["message"].startswith("AttributeError"))

    def test_duplicates_collapse_to_best_copy(self):
        dup = routes()
        dup["https://remotive.com/api/remote-jobs"] = {"jobs": [{
            "id": 77, "url": "https://remotive.com/remote-jobs/77",
            "title": "Salesforce Agentforce Architect (Remote)", "company_name": "Acme, Inc.",
            "description": "Agentforce on Salesforce.", "publication_date": "2026-09-21T08:00:00"}]}
        feed, _, _ = self.build(http=FakeHttp(dup))
        arch = [j for j in feed["jobs"] if "Agentforce Architect" in j["title"]]
        self.assertEqual(len(arch), 1)
        self.assertEqual(arch[0]["via"], "Greenhouse")

    def test_old_postings_age_out(self):
        feed, _, _ = self.build(now=NOW + timedelta(days=60))
        self.assertNotIn("Salesforce Agentforce Architect", by_title(feed))


class AdzunaTest(unittest.TestCase):
    @mock.patch.dict(os.environ, {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "secret-key"})
    def test_fetches_with_keys_and_marks_estimates(self):
        http = FakeHttp({"https://api.adzuna.com/": {"results": [{
            "id": "a1", "title": "<strong>Agentforce</strong> Developer",
            "company": {"display_name": "Eliassen"}, "location": {"display_name": "Remote"},
            "redirect_url": "https://adzuna.com/a1", "description": "Salesforce Agentforce build",
            "created": "2026-09-22T00:00:00Z", "salary_min": 150000, "salary_max": 150000,
            "salary_is_predicted": "1"}]}})
        raws = fj.fetch_adzuna({"queries": ["agentforce"]}, http, NOW)
        self.assertEqual(raws[0]["title"], "Agentforce Developer")
        self.assertEqual(raws[0]["salary"], "~$150K (est.)")
        self.assertIn("what=agentforce", http.calls[0][0])


class HelpersTest(unittest.TestCase):
    def test_extract_salary(self):
        cases = {
            "Base pay $145,000 - $165,000 plus equity": "$145,000 - $165,000",
            "Range: $146K–$225K": "$146K–$225K",
            "Contract at $80-90/hr, remote": "$80-90/hr",
            "$65.00 to $70.00 per hour": "$65.00 to $70.00 per hour",
            "We raised $10 - $20 million": "",
            "No numbers here": "",
        }
        for text, want in cases.items():
            self.assertEqual(fj.extract_salary(text), want, text)

    def test_terms_match_whole_words(self):
        scorer = fj.Scorer(CONFIG["scoring"])
        score = lambda title, desc="": scorer.score(  # noqa: E731
            {"title": title, "description": desc, "company": "X"})
        _, matched = score("Engineer", "Salesforce storage")
        self.assertNotIn("RAG / vector search", matched)
        _, matched = score("Engineer", "Salesforce RAG pipelines")
        self.assertIn("RAG / vector search", matched)
        self.assertIsNone(score("Sr. Account Executive", "Salesforce"))
        self.assertIsNone(score("Engineer", "no platform named"))
        full, _ = score("Sr. Salesforce Agentforce Architect",
                        "Agentforce Data Cloud Apex LWC RAG MCP LLM architecture Austin")
        self.assertEqual(full, 100)

    def test_parse_dates(self):
        self.assertEqual(fj.iso(fj.parse_date(1789912800000)), "2026-09-20T14:00:00Z")
        self.assertEqual(fj.iso(fj.parse_date("2026-09-21T10:00:00.0000000")),
                         "2026-09-21T10:00:00Z")
        self.assertIsNone(fj.parse_date("soon"))
        self.assertEqual(fj.workday_posted("Posted 30+ Days Ago", NOW).day, 24)

    def test_html_to_text(self):
        self.assertEqual(fj.html_to_text("&lt;p&gt;A &amp;amp; B&lt;/p&gt;&lt;br&gt;C"), "A & B C")


def quiet():
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
    return stack


class MainTest(unittest.TestCase):
    def test_all_sources_failing_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "jobs.json"
            out.write_text('{"jobs": []}')
            with mock.patch.object(fj, "Http", lambda: FakeHttp({})), quiet():
                code = fj.main(["--config", str(ROOT / "config" / "search.json"),
                                "--out", str(out), "--only", "greenhouse"])
            self.assertEqual(code, 2)
            self.assertEqual(out.read_text(), '{"jobs": []}')

    def test_writes_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "search.json"
            cfg.write_text(json.dumps(config()))
            out = Path(tmp) / "nested" / "jobs.json"
            with mock.patch.object(fj, "Http", lambda: FakeHttp(routes())), \
                    mock.patch.dict(os.environ, {"ADZUNA_APP_ID": ""}), quiet():
                code = fj.main(["--config", str(cfg), "--out", str(out)])
            self.assertEqual(code, 0)
            feed = json.loads(out.read_text())
            self.assertEqual(feed["schema"], 1)
            self.assertGreaterEqual(len(feed["jobs"]), 5)


if __name__ == "__main__":
    unittest.main()
