# Cold Email Bot (personal, review-first)

![tests](https://github.com/chetan10510/chausar-assignment/actions/workflows/tests.yml/badge.svg)

Find professional contacts with Apollo, infer a **clearly labeled** company email pattern from known work addresses, build a reviewable job-outreach workbook, and send approved messages through the Gmail API. This is a clean standalone version: it contains no real contacts, resumes, credentials, or send history.

It packages the reusable discovery-to-send workflow, not the original project's Streamlit app, one-off company batch scripts, historical contact data, or follow-up tooling. No real contacts, credentials, send history, or resumes are included.

## Setup (PowerShell)

```powershell
git clone https://github.com/chetan10510/chausar-assignment.git
Set-Location .\chausar-assignment
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
New-Item -ItemType Directory -Force data | Out-Null
Copy-Item .\examples\companies.csv .\data\companies.csv
```

Edit `.env` with **your own** Apollo API key, Gmail OAuth Desktop client ID and secret, name, LinkedIn URL, and resume path. Put your own PDF at `data/resume.pdf` or change `RESUME_PATH`. Do not use somebody else's credentials or put keys in a command or repository. Never commit `.env` or `data/`.

The `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET` live in that one `.env`; an OAuth token is created locally at `TOKEN_PATH` after first authorization. The token is not part of `.env` because it changes automatically. Apollo enrichment may consume credits; run small previews first.

## Gmail API setup

**Gmail sending needs an OAuth 2.0 Desktop client ID and secret, not a Gmail API key.** Each person should create a client in their own Google Cloud project and authorize the Gmail account they intend to send from. Follow [Google's Gmail API Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python) if the Cloud Console labels change.

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a project and enable the **Gmail API** in **APIs & Services > Library**. Use this same project for the OAuth client.
2. In **Google Auth Platform**, configure **Branding** and **Audience**. For a personal Gmail account, choose **External**. If the app is in **Testing**, add the sender's Gmail address under **Test users**. In **Data Access**, add the `https://www.googleapis.com/auth/gmail.send` scope if prompted. This [send-only scope](https://developers.google.com/workspace/gmail/api/auth/scopes) does not grant inbox-reading access.
3. In **Google Auth Platform > Clients**, create an **OAuth client ID** with application type **Desktop app**. Download its client JSON. Copy `installed.client_id` and `installed.client_secret` from the JSON into the corresponding `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET` lines in `.env`. This bot reads `.env`, **not** the downloaded JSON file; do not commit either file.
4. Check that `RESUME_PATH` points to your PDF and `TOKEN_PATH` points to a local ignored path such as `data/token.json`. Review drafts and mark only the intended rows `READY`. A dry run does not authenticate or send:

```powershell
.\.venv\Scripts\python.exe .\coldbot.py send --input data/drafts.xlsx --limit 1
.\.venv\Scripts\python.exe .\coldbot.py send --input data/drafts.xlsx --limit 1 --send
```

The first real send opens a browser for Google sign-in and consent. Sign in with the **intended sender account**. The resulting refresh token is stored locally at `TOKEN_PATH`; keep it private. If consent is in Testing, Google may expire that authorization after seven days, requiring sign-in again ([Google's audience guidance](https://support.google.com/cloud/answer/15549945?hl=en)). If you see `invalid_grant`, first check the account and OAuth setup, then rename the existing token file as a backup and repeat the one-message send to authorize again.

If you see `accessNotConfigured` or a Gmail API 403, check that the Gmail API is enabled in the **same project** as the OAuth client. If you see `redirect_uri_mismatch`, check that you created a **Desktop app** client. If access is denied while in Testing, check the **Test users** list. Gmail account sending limits are separate from API quotas; neither guarantees a particular daily send volume.

## Workflow

1. Replace the example row in `data/companies.csv` with actual companies and exact job URLs. Use the employer's official domain, not a LinkedIn/ATS domain.
2. Discover a **small** contact sample, optionally with Apollo work-email enrichment:

```powershell
.\.venv\Scripts\python.exe .\coldbot.py discover --companies data/companies.csv --out data/contacts.csv --per-company 5 --location India --enrich
```

3. Or start from your own contacts CSV (`examples/contacts.csv`). A supplied work email is tagged `provided`. `infer` can fill blank emails from a recognized company pattern, but these are tagged `guessed` and are never eligible for sending without independent verification.

```powershell
.\.venv\Scripts\python.exe .\coldbot.py infer --contacts data/contacts.csv --out data/contacts_review.csv
.\.venv\Scripts\python.exe .\coldbot.py draft --contacts data/contacts_review.csv --out data/drafts.xlsx
```

4. Read every draft and check job title, URL, name, recipient, and your resume. Set `Approval Status` to `READY` only for rows you want to send. Leave guessed/mismatched rows `BLOCKED`; they remain unsendable even if accidentally marked ready.
5. Dry-run, then explicitly send a small approved batch:

```powershell
.\.venv\Scripts\python.exe .\coldbot.py send --input data/drafts.xlsx --limit 5
.\.venv\Scripts\python.exe .\coldbot.py send --input data/drafts.xlsx --limit 5 --send
```

The sender attaches your resume, records Gmail message IDs, skips prior recipients, and stops on an uncertain send error so you can inspect Gmail Sent before retrying. Re-running processes only unsent, approved rows. It is not designed for mass blasting; respect provider limits and recipients' preferences.

## What each command does

- `discover` makes Apollo organization and people-search calls. `--enrich` calls Apollo's person-match endpoint and may use credits. Results are saved as local CSV; no email is sent.
- `infer` recognizes a pattern such as `first.last@company.com` from a known work address. Any generated address is marked `guessed` and `unverified`.
- `draft` writes the `Drafts` Excel sheet. Every sendable row starts as `REVIEW`; duplicate, guessed, and domain-mismatched rows start as `BLOCKED`. Edit the subject/body and approve only after checking them.
- `send` checks `READY`, email type, company-domain match, prior sent log, and resume attachment. Without `--send`, it only prints a dry run. With `--send`, it performs Gmail OAuth on first use and records each successful send. A batch is limited to 20 rows.

Run local tests after installation with `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`. Tests use fake Apollo/Gmail data and do not spend credits or send messages.

## Example run (the safety states in action)

Using only `examples/contacts.csv` (fake data): Alex has a provided work email, Sam has
none, so `infer` guesses one from the company pattern and labels it. The draft step then
sets the approval state per row, and `send` without `--send` is always a dry run:

```text
$ python coldbot.py draft --contacts data/contacts_review.csv --out data/drafts.xlsx
Saved 2 review-first drafts to data\drafts.xlsx; mark selected verified rows READY

# Row states in the workbook:
#   alex.example@example.com -> REVIEW    (provided address, human must approve)
#   sam.sample@example.com   -> BLOCKED   (guessed address, unsendable even if marked READY)

$ python coldbot.py send --input data/drafts.xlsx --limit 5     # before any approval
Dry-run checked 0 approved rows; no messages sent

$ python coldbot.py send --input data/drafts.xlsx --limit 5     # after marking Alex READY
DRY RUN 1: alex.example@example.com | Software Engineer at Example Company | Candidate
Dry-run checked 1 approved rows; no messages sent
```

Nothing is sent until `--send` is passed explicitly, and the sender re-checks the email
label, company-domain match, prior-send log and resume attachment at send time — approval
alone is not enough.

## CSV columns

- Companies: `Company`, `Domain`, `Job Title`, `Job URL`.
- Contacts: `Company`, `Domain`, `Name`, `Title`, `Email`, `Email Status`, `Email Type`, `Job Title`, `Job URL`. Extra Apollo fields are retained by `discover`.
- `Email Type`: `provided`, `verified`, or `guessed`. `verified` means Apollo reported verified; it is not a guarantee of delivery.
- The exact job URL is optional, but when missing the draft does not claim that a specific opening exists.

## Security and limitations

- Do not publish CSV/XLSX outputs, OAuth tokens, resumes, or logs containing recipients.
- Apollo results can be wrong or stale. The script checks company domain and email domain, but you still review identity and relevance.
- Inferred email patterns are **unverified guesses**, even if they resemble a real address.
- Gmail API access and Apollo features depend on your own account permissions, quotas, and current provider behavior.
- A `Remote` job may still restrict hiring to a particular country. Verify the job location and eligibility yourself before outreach.
- Contact only relevant people, use truthful claims, and honor opt-outs. This tool does not scrape LinkedIn or bypass access controls.
