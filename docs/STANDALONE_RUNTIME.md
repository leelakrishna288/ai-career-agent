# Standalone runtime (no Claude, no LLM subscription)

`career-agent discover` finds jobs, scores them and writes them to the Notion
Application Tracker. It runs on GitHub Actions every day at 05:15 IST, so it
keeps working while the laptop is off and whether or not any Claude plan is
active.

## What it does

1. Reads public job-board APIs published by employers' applicant tracking
   systems: Greenhouse, Lever and Ashby (`config/discovery.yaml`). These
   endpoints are documented and meant for embedding job lists. No login,
   no scraping, no CAPTCHA handling. Since v1.2 it also reads two remote-job
   boards with free public APIs, Himalayas and Remotive. Their terms require
   linking back to the listing and naming the board as the source, so each row
   keeps the board URL and sets Platform to the board's name. Remotive asks
   for at most four fetches a day, so the run makes one Remotive call.
   LinkedIn, Naukri and Indeed are deliberately not included. Their terms
   prohibit automated collection and automated applying, so their jobs arrive
   through the email alerts Leela subscribes to.
2. Filters out titles that are not target role families.
3. Skips postings older than 45 days.
4. **Checks work authorisation before scoring.** Roles in the USA, Europe and
   other non-target countries are skipped. Remote-worldwide roles are kept.
5. Deduplicates against every tracker row, using the external job ID, the job
   URL, and company plus normalised role.
6. Extracts facts deterministically. Years come only from explicit phrases,
   skills only from a fixed vocabulary using whole-word matching, and work
   mode only from explicit words. Hybrid is never labelled remote. Anything
   not found stays empty or Unknown.
7. Scores each job with the repo's 100-point rubric.
8. Labels each row's **Purpose** from the city lists in the config:
   - `TARGET`: remote, Hyderabad, Bengaluru, Chennai or the Gulf. Leela
     would accept an offer here.
   - `PRACTICE`: Pune, Mumbai, Delhi NCR or Kolkata. These are for
     interview practice only.
   - `OTHER`: anything else.

   If a posting lists a target city anywhere, it counts as TARGET.
9. Writes rows in score order, capped at 40 per run and 6 per company.
10. Writes a report page to Notion. The public job log shows counts only.

## What it deliberately does NOT do

- **Company trust checks.** Glassdoor, AmbitionBox and Google Maps have no
  permitted API. New rows arrive with `Company Verdict = UNVERIFIED`, and the
  Claude daily run or Leela completes the check.
- **Resume tailoring and application packs.** These stay with the Claude run,
  which has the validation gate and the resume files.
- **Applying.** This runtime never submits anything. A separate Claude run on
  Leela's laptop submits on company career sites, and only under her standing
  rule (SYSTEM_SPEC §11a: score of 75 or more, company GENUINE, posting still
  live, every answer known) or when she has set `Status = APPROVED`.

## One-time setup (about 10 minutes)

1. **Create a Notion integration.**
   - Go to https://www.notion.so/profile/integrations, choose **New
     integration**, pick your workspace and select type **Internal**.
   - Under Capabilities, tick **Read content**, **Update content** and
     **Insert content**.
   - Copy the secret. It starts with `ntn_`.
2. **Share the page with the integration.**
   - Open the Notion page **AI Career Agent**.
   - Click `•••` → **Connections** → add your integration.
   - The Application Tracker inherits access from the page.
3. **Add the secret and variables in GitHub.** Open
   github.com/leelakrishna288/ai-career-agent → Settings → Secrets and
   variables → Actions.
   - **Secrets** tab → New repository secret: name `NOTION_TOKEN`, value
     the `ntn_…` secret.
   - **Variables** tab → New repository variable: name
     `NOTION_DATA_SOURCE_ID`, value
     `790bcdd9-8ff0-445c-b64d-2e1064b4de1e`.
   - **Variables** tab → New repository variable: name
     `NOTION_REPORT_PAGE_ID`, value `3d1be82cb5588106804bd9c1f09ec9f2`.
4. **Run it once.** Go to Actions → **Daily job discovery** → **Run
   workflow**. A green run with "New rows written: N" in the summary means it
   works.

## Run it locally

```bash
pip install -e .
career-agent discover                 # dry run: prints the report, writes nothing
NOTION_TOKEN=... NOTION_DATA_SOURCE_ID=790bcdd9-8ff0-445c-b64d-2e1064b4de1e \
  career-agent discover --notion      # writes to the tracker
```

On Windows PowerShell, set the variables first with `$env:NOTION_TOKEN="..."`.

## Adding employers

Add a line to `config/discovery.yaml`. The token is the path segment after
the board's host:

- Greenhouse: `job-boards.greenhouse.io/<token>/jobs/…`
- Lever: `jobs.lever.co/<token>/…`
- Ashby: `jobs.ashbyhq.com/<token>/…`

A board that returns 404 is reported in the run summary and skipped.

## Verified on 2026-09-17 against live data

The pipeline was replayed over that day's real responses from 30 configured
boards: 29 answered, 2,394 postings. Aisera's board returned 404 and was
removed. The replay found and fixed four real defects:

| Defect | Effect | Fix |
|---|---|---|
| Substring skill matching | `trust` produced a Rust gap; `scalable` produced a Scala gap | whole-term matching |
| Substring AI-relevance matching in the scorer | `management` counted as "agent" and `leverage` as "rag", so plain Java roles got full AI points | whole-term matching |
| Countries missing from the gate | Brazil, Canada, Argentina, Hong Kong and "US - Orlando" passed the work-authorisation gate as "unknown" | explicit list of non-target countries, plus a US token |
| Rows written in board order | the per-run cap could drop strong roles in favour of weak ones; one employer (Capco) could flood the tracker | score-ordered writes and a per-company cap |

A second replay against the updated tracker wrote no duplicates.

The real Notion API write path is covered by tests against a mocked
transport. It has **not** been exercised against the live Notion API from this
code, because the build sandbox cannot reach api.notion.com. The first GitHub
Actions run is that check.
