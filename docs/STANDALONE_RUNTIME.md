# Standalone runtime (no Claude, no LLM subscription)

`career-agent daily` is the whole daily job-search loop, and it runs on GitHub
Actions every day at 05:15 IST, so it keeps working while the laptop is off
and whether or not any Claude plan is active.

## What is independent, and what is not

| Step | Runs without Claude? | How |
|---|---|---|
| Find jobs on company ATS boards (Greenhouse, Lever, Ashby) | YES | public job-board APIs |
| Find jobs on Himalayas / Remotive | YES | public APIs (link back, name the board) |
| Read public Telegram channels | YES | the `t.me/s/<channel>` web preview; no login |
| Read job blogs that publish schema.org JobPosting data | YES | robots.txt checked on every run |
| Gates: title, freshness, fresher-only, work authorisation, duplicates | YES | deterministic |
| Match score (0–100) | YES | the rubric in `scoring.py` |
| Tailored resume per job (DOCX + text on the tracker row) | YES | `tailor.py` selects/reorders only; the validation gate blocks anything not in the profile |
| ESTIMATED ATS score and the ≥ 90 gate | YES | `ats.py` |
| Link and post safety (scam wording, "exam help", shorteners, IP hosts, downloads, look-alike domains) | YES | `safety.py` |
| Malware / phishing reputation | OPTIONAL | Google Safe Browsing and/or VirusTotal free API keys |
| Government IT jobs (official pages + Telegram) → Govt Job Tracker | YES | `govt.py` |
| Daily digest with apply links → Notion "Daily Reports" + email | YES | Notion API + Gmail SMTP (app password) |
| Company trust check (Glassdoor, AmbitionBox, Google Maps reviews) | **NO** | these sites offer no permitted free API; rows arrive `Company Verdict = UNVERIFIED` and the Claude run (or Leela) completes the check |
| Reading the full text of a government notification PDF (age, marks, GATE) | **NO** | rows say exactly what must be read; nothing is guessed |
| Submitting applications | **NO, by design** | see below |
| LinkedIn, Naukri, Indeed | **NO, by their terms** | job alerts by email; Easy Apply is Leela's click |
| WhatsApp channels and private Telegram groups | **NO** | no public web view; automating WhatsApp breaks its terms; forward posts to the Notion Job Inbox |

### Why submission is not automated here

Greenhouse, Lever and Ashby only accept application POSTs with the
*employer's* API key, and most portals add CAPTCHA or an email code. A
candidate-side script that bypassed those would break the portals' terms and
Leela's own rules. Submission therefore stays with Leela, or with the laptop
run (Claude in Chrome, her standing rule: match ≥ 80, resume READY with ATS
≥ 90, company GENUINE, every answer known, CAPTCHA/codes typed by her).

## Daily flow

1. Read every source in `config/discovery.yaml`. A failed source is reported
   and skipped. A Telegram *group* (for example `offcampusjobs_4u`) has no
   public preview and is reported as unreadable rather than guessed at.
2. Telegram posts are classified as private-sector lead, government lead or
   skip (bank exams, non-CS government posts, internships, fresher batches,
   no link). Posts with payment/fee or "exam help" wording, or dangerous
   links, are dropped. Each kept lead records the channel's subscriber count
   and last-post date (trust HIGH/MEDIUM/LOW) and the link-safety verdict.
3. Gates: target role titles; not contract/intern/gig; posted in the last 45
   days; not fresher-only; work authorisation (India, UAE, Qatar, Saudi
   Arabia or worldwide remote); not already in the tracker.
4. Score, label TARGET / PRACTICE / OTHER, write rows (best first, 40 per run,
   6 per company). Community leads are always `MANUAL REVIEW` until the
   employer's own posting is read.
5. For up to 10 new APPLY/MAYBE rows with a full job description: tailor the
   resume, run the validation gate and the ESTIMATED ATS. The row gets the ATS
   number, the resume ID, the DOCX file and the full text. `Status =
   RESUME_PREPARED` only when the resume is READY (validation passed and ATS ≥
   90). Below 90 the Next Action says which required skills cannot truthfully
   be shown — nothing is added to raise the number.
6. Government: read ~30 official recruitment pages, keep new links that
   mention this or next year and a CS/IT signal (C-DAC, NIELIT, NIC, CRIS,
   STPI, DIC, C-DOT count every recruitment link); add Telegram government
   leads; write new rows to the Govt Job Tracker with `Status = NEW` and the
   list of things Leela must check (date of birth vs cut-off, B.Tech %, GATE,
   last date).
7. Digest: apply-now list with links, resumes below the ATS bar, community
   leads, rows still waiting from the last 14 days, new government links and
   upcoming government deadlines. Written to Notion → AI Career Agent → Daily
   Reports, and emailed with the READY resumes attached.

## One-time setup

The Notion part was set up on 2026-09-17 (`NOTION_TOKEN` secret,
`NOTION_DATA_SOURCE_ID` variable). The page and database ids used by v1.3 are
in `config/discovery.yaml` (`notion:`), so no new variables are needed. The
integration reaches the new "Daily Reports" page and "Govt Job Tracker"
because they sit under the shared "AI Career Agent" page.

### Email digest (about 3 minutes)

1. Google Account → Security → turn on **2-Step Verification** (required for
   app passwords).
2. Google Account → Security → **App passwords** → create one named
   `career-agent`. Copy the 16-character password.
3. GitHub → `leelakrishna288/ai-career-agent` → Settings → Secrets and
   variables → Actions → **Secrets** → New repository secret:
   - `GMAIL_USER` = `leelakrishna288@gmail.com`
   - `GMAIL_APP_PASSWORD` = the 16-character password
   - (optional) `DIGEST_TO` = another address to receive the digest
4. Actions → **Daily job discovery** → Run workflow. The summary says
   "Digest emailed" when it works.

Without these secrets the run still works; the digest goes to Notion only.

### Optional link-reputation checks (free)

- **Google Safe Browsing**: Google Cloud console → create a project → enable
  "Safe Browsing API" → Credentials → API key → add secret
  `GOOGLE_SAFE_BROWSING_KEY`.
- **VirusTotal**: create a free account → API key → add secret
  `VIRUSTOTAL_API_KEY` (free tier: 4 lookups a minute, 500 a day; personal,
  non-commercial use).

Without keys the digest says "not configured" for these checks; it never
claims a scan ran.

## Run it locally

```bash
pip install -e .
career-agent daily --out reports --save-resumes     # dry run: nothing written to Notion
career-agent daily --notion --email                 # what GitHub runs
```

On Windows PowerShell, set variables first, e.g. `$env:NOTION_TOKEN="..."`.

## Adding sources

`config/discovery.yaml`:

- Greenhouse / Lever / Ashby: the token is the path segment after the board's
  host (`job-boards.greenhouse.io/<token>`, `jobs.lever.co/<token>`,
  `jobs.ashbyhq.com/<token>`).
- Telegram: `{ platform: telegram, token: <public channel handle> }`. Check
  that `https://t.me/s/<handle>` shows posts in a browser first.
- Job blog: `{ platform: jobsite, token: <listing page URL> }`. Only sites
  whose job pages publish schema.org `JobPosting` data and whose robots.txt
  allows the page.
- Government page: add to `govt_watch` with `org` and `url`; set
  `cs_org: true` only for organisations that hire only in computing.

## Honest limits

- Telegram and job-blog posts are reposts. A HIGH channel-trust label means
  the channel is large and active, not that a post is true. The employer page
  and the company check decide.
- Government rows are leads until the official notification is read; the
  runtime never fills age, marks, GATE or dates from a post.
- The ATS number is our estimate of keyword and structure fit, not any
  employer's real ATS score.
- The Notion file-upload and email paths are covered by tests against mocked
  transports; the first scheduled run is their live check.
