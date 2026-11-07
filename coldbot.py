"""Review-first Apollo discovery, email pattern audit, drafting, and Gmail sending."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

CONTACT_FIELDS = [
    "Company", "Domain", "Name", "Title", "Email", "Email Status", "Email Type",
    "Job Title", "Job URL", "LinkedIn URL", "Country", "Apollo Person ID", "Notes",
]
DRAFT_FIELDS = CONTACT_FIELDS + [
    "Draft Subject", "Draft Body", "Approval Status", "Send Status", "Sent At",
    "Gmail Message ID", "Error",
]
EMAIL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PERSON_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z.' -]+ [A-Za-z][A-Za-z.' -]+$")
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def env(name: str, default: str = "") -> str:
    import os

    return os.environ.get(name, default).strip()


def data_path(name: str) -> Path:
    defaults = {
        "RESUME_PATH": "data/resume.pdf",
        "TOKEN_PATH": "data/token.json",
        "SENT_LOG_PATH": "data/sent_log.csv",
    }
    value = Path(env(name, defaults.get(name, "")))
    return value if value.is_absolute() else ROOT / value


def clean(value: object) -> str:
    return str(value or "").strip()


def domain_of(value: str) -> str:
    value = clean(value).lower()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else "https://" + value)
    return (parsed.hostname or "").removeprefix("www.")


def email_matches_domain(email: str, domain: str) -> bool:
    host = clean(email).lower().rpartition("@")[2]
    expected = domain_of(domain)
    return bool(EMAIL_RE.fullmatch(clean(email)) and expected and
                (host == expected or host.endswith("." + expected)))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{key: clean(value) for key, value in row.items() if key is not None}
                for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def api_post(path: str, payload: dict, key: str) -> dict:
    request = urllib.request.Request(
        "https://api.apollo.io/api/v1/" + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-api-key": key},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Apollo returned HTTP {exc.code}; check your key, quota, and permissions") from exc
        except urllib.error.URLError as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError("Apollo network request failed") from exc
    raise RuntimeError("Apollo request failed")


def select_organization(response: dict, domain: str) -> dict | None:
    for org in response.get("organizations", []):
        if domain_of(clean(org.get("primary_domain") or org.get("domain") or org.get("website_url"))) == domain:
            return org
    return None


def discover(args: argparse.Namespace) -> None:
    key = env("APOLLO_API_KEY")
    if not key:
        raise ValueError("Set APOLLO_API_KEY in .env first")
    companies = read_csv(args.companies)
    if args.per_company < 1 or args.per_company > 25:
        raise ValueError("--per-company must be between 1 and 25")
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for company in companies:
        name = clean(company.get("Company"))
        domain = domain_of(clean(company.get("Domain")))
        if not name or not domain:
            print(f"SKIP incomplete company: {name or '(blank)'}")
            continue
        try:
            response = api_post("mixed_companies/search", {
                "q_organization_domains_list": [domain], "page": 1, "per_page": 5,
            }, key)
            org = select_organization(response, domain)
            if not org or not org.get("id"):
                print(f"SKIP no exact Apollo organization-domain match: {name}")
                continue
            people_response = api_post("mixed_people/api_search", {
                "organization_ids": [org["id"]],
                "person_titles": ["software engineer", "data engineer", "machine learning engineer",
                                  "engineering manager", "technical recruiter", "talent acquisition"],
                "per_page": args.per_company, "page": 1,
                **({"person_locations": [args.location]} if args.location else {}),
            }, key)
            people = people_response.get("people") or people_response.get("contacts") or []
            print(f"{name}: {len(people)} Apollo people found")
            for person in people:
                if not isinstance(person, dict):
                    continue
                pid = clean(person.get("id"))
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                detail = person
                if args.enrich:
                    matched = api_post("people/match?reveal_personal_emails=false&reveal_phone_number=false",
                                       {"id": pid, "domain": domain}, key)
                    detail = matched.get("person") or person
                    time.sleep(0.3)
                email = clean(detail.get("email"))
                if not email_matches_domain(email, domain):
                    email = ""
                status = clean(detail.get("email_status"))
                country = clean(detail.get("country") or person.get("country"))
                if args.location and country.casefold() != args.location.casefold():
                    continue
                output.append({
                    "Company": name, "Domain": domain,
                    "Name": clean(detail.get("name")) or " ".join(filter(None, [
                        clean(detail.get("first_name")), clean(detail.get("last_name"))])),
                    "Title": clean(detail.get("title")), "Email": email,
                    "Email Status": status,
                    "Email Type": "verified" if email and status.lower() == "verified" else "observed" if email else "",
                    "Job Title": clean(company.get("Job Title")), "Job URL": clean(company.get("Job URL")),
                    "LinkedIn URL": clean(detail.get("linkedin_url")), "Country": country,
                    "Apollo Person ID": pid,
                    "Notes": "Apollo-reported work email" if email else "No domain-matched work email found",
                })
        except RuntimeError as exc:
            print(f"ERROR {name}: {exc}", file=sys.stderr)
        time.sleep(0.5)
    write_csv(args.out, output, CONTACT_FIELDS)
    print(f"Saved {len(output)} contacts to {args.out}; no messages sent")


def name_parts(name: str) -> tuple[str, str] | None:
    if not PERSON_NAME_RE.fullmatch(name):
        return None
    parts = re.findall(r"[A-Za-z]+", name.lower())
    if {"unknown", "user", "linkedin", "available", "recruiting", "company"} & set(parts):
        return None
    return (parts[0], parts[-1]) if len(parts) >= 2 else None


def address_patterns(first: str, last: str) -> dict[str, str]:
    return {
        "first.last": f"{first}.{last}",
        "first": first,
        "flast": first[0] + last,
        "firstl": first + last[0],
        "first.last_initial": first + "." + last[0],
        "f.last": first[0] + "." + last,
    }


def infer(args: argparse.Namespace) -> None:
    rows = read_csv(args.contacts)
    examples: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in rows:
        domain = domain_of(row.get("Domain", ""))
        email = row.get("Email", "")
        parts = name_parts(row.get("Name", ""))
        if email and not row.get("Email Type") and email_matches_domain(email, domain):
            row["Email Type"] = "provided"
            row["Email Status"] = row.get("Email Status") or "provided"
        if parts and email_matches_domain(email, domain) and row.get("Email Type", "").lower() in {
            "provided", "verified"
        }:
            examples[domain].append((email.split("@")[0].lower(), row["Name"]))
    patterns: dict[str, str] = {}
    for domain, values in examples.items():
        votes: Counter[str] = Counter()
        for local, name in values:
            first, last = name_parts(name)  # type: ignore[misc]
            matches = [pattern for pattern, candidate in address_patterns(first, last).items() if candidate == local]
            if len(matches) == 1:
                votes[matches[0]] += 1
        top = votes.most_common()
        if top and (len(top) == 1 or top[0][1] > top[1][1]):
            patterns[domain] = top[0][0]
    used = {row.get("Email", "").lower() for row in rows if row.get("Email")}
    count = 0
    for row in rows:
        domain = domain_of(row.get("Domain", ""))
        if row.get("Email") or domain not in patterns:
            continue
        parts = name_parts(row.get("Name", ""))
        if not parts:
            continue
        guess = address_patterns(*parts)[patterns[domain]] + "@" + domain
        if guess in used:
            row["Notes"] = "Pattern collision; email not guessed"
            continue
        row["Email"] = guess
        row["Email Type"] = "guessed"
        row["Email Status"] = "unverified"
        row["Notes"] = f"Unverified guess using {patterns[domain]} pattern; verify before use"
        used.add(guess)
        count += 1
    write_csv(args.out, rows, list(dict.fromkeys(CONTACT_FIELDS + [key for row in rows for key in row])))
    print(f"Saved {len(rows)} rows with {count} labeled guesses to {args.out}; no messages sent")


def draft_message(row: dict[str, str]) -> tuple[str, str]:
    company = row["Company"]
    job = clean(row.get("Job Title"))
    url = clean(row.get("Job URL"))
    first_name = clean(row.get("Name")).split(" ")[0]
    first_name = first_name if first_name and first_name.lower() not in {"unknown", "linkedin", "user"} else "there"
    subject = f"{job} at {company} | {env('SENDER_NAME', 'Candidate')}" if job else f"Engineering opportunities at {company} | {env('SENDER_NAME', 'Candidate')}"
    intro = f"I came across the {job} opening at {company}." if job else f"I am interested in engineering opportunities at {company}."
    link = f"Role: {url}\n\n" if job and url.startswith("https://") else ""
    body = (
        f"Hi {first_name},\n\n{intro} {env('PROFILE_HIGHLIGHT', 'I build reliable software and data systems.')}\n\n"
        f"{link}I have attached my resume for context. If this is relevant to your team, I would appreciate your consideration or a pointer to the appropriate hiring contact.\n\n"
        f"Best,\n{env('SENDER_NAME', 'Candidate')}\n"
    )
    if env("LINKEDIN_URL"):
        body += f"LinkedIn: {env('LINKEDIN_URL')}\n"
    return subject, body


def draft(args: argparse.Namespace) -> None:
    rows = read_csv(args.contacts)
    wb = Workbook()
    ws = wb.active
    ws.title = "Drafts"
    ws.append(DRAFT_FIELDS)
    best_by_email: dict[str, tuple[int, int]] = {}
    for position, row in enumerate(rows):
        email = row.get("Email", "").lower()
        if not email:
            continue
        trusted = email_matches_domain(email, row.get("Domain", "")) and row.get("Email Type", "").lower() in {
            "provided", "verified"
        }
        score = int(trusted) * 2 + int(bool(row.get("Job URL", "")))
        if email not in best_by_email or score > best_by_email[email][0]:
            best_by_email[email] = (score, position)
    for position, row in enumerate(rows):
        email = row.get("Email", "").lower()
        domain = row.get("Domain", "")
        kind = row.get("Email Type", "").lower()
        valid = email_matches_domain(email, domain) and kind in {"provided", "verified"}
        if email and best_by_email[email][1] != position:
            row["Notes"] = "Duplicate email; review only"
            valid = False
        subject, body = draft_message(row)
        ws.append([row.get(field, "") for field in CONTACT_FIELDS] +
                  [subject, body, "REVIEW" if valid else "BLOCKED", "", "", "", ""])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="123B4A")
    for column in ws.columns:
        letter = column[0].column_letter
        ws.column_dimensions[letter].width = 58 if column[0].value == "Draft Body" else 25
    for row in ws.iter_rows(min_row=2):
        row[DRAFT_FIELDS.index("Draft Body")].alignment = Alignment(wrap_text=True, vertical="top")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(args.out)
    print(f"Saved {len(rows)} review-first drafts to {args.out}; mark selected verified rows READY")


def authenticate():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_id = env("GMAIL_CLIENT_ID")
    client_secret = env("GMAIL_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env")
    token_path = data_path("TOKEN_PATH")
    credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES) if token_path.exists() else None
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except Exception as exc:
                raise RuntimeError(f"OAuth refresh failed. Move {token_path} aside and authorize again") from exc
        else:
            config = {"installed": {
                "client_id": client_id, "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }}
            flow = InstalledAppFlow.from_client_config(config, SCOPES)
            credentials = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
    return credentials


def sent_emails(path: Path) -> set[str]:
    return {row.get("Email", "").lower() for row in read_csv(path)} if path.exists() else set()


def append_sent(path: Path, email: str, subject: str, message_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Email", "Subject", "Sent At", "Gmail Message ID"])
        if not existed:
            writer.writeheader()
        writer.writerow({"Email": email, "Subject": subject,
                         "Sent At": datetime.now(timezone.utc).isoformat(), "Gmail Message ID": message_id})


def send(args: argparse.Namespace) -> None:
    if not 1 <= args.limit <= 20:
        raise ValueError("--limit must be between 1 and 20; review batches individually")
    if args.delay_seconds < 0:
        raise ValueError("--delay-seconds cannot be negative")
    wb = load_workbook(args.input)
    ws = wb["Drafts"]
    headers = [clean(cell.value) for cell in ws[1]]
    missing = set(DRAFT_FIELDS) - set(headers)
    if missing:
        raise ValueError(f"Drafts sheet is missing columns: {', '.join(sorted(missing))}")
    columns = {header: index for index, header in enumerate(headers, 1)}
    log_path = data_path("SENT_LOG_PATH")
    previously_sent = sent_emails(log_path)
    resume = data_path("RESUME_PATH")
    if not resume.is_file():
        raise FileNotFoundError(f"Resume attachment not found: {resume}")
    if resume.suffix.lower() != ".pdf":
        raise ValueError("RESUME_PATH must point to a PDF file")
    service = None
    count = 0
    in_batch: set[str] = set()
    for index in range(2, ws.max_row + 1):
        row = {field: clean(ws.cell(index, columns[field]).value) for field in DRAFT_FIELDS}
        email = row["Email"].lower()
        if row["Approval Status"].upper() != "READY" or row["Send Status"].upper() in {"SENT", "ERROR_UNCERTAIN"}:
            continue
        if not email_matches_domain(email, row["Domain"]) or row["Email Type"].lower() not in {"provided", "verified"}:
            print(f"BLOCKED unverified or domain-mismatched: {email}")
            continue
        if email in previously_sent or email in in_batch:
            print(f"SKIP duplicate/prior recipient: {email}")
            continue
        if not row["Draft Subject"] or not row["Draft Body"]:
            print(f"BLOCKED missing subject/body: {email}")
            continue
        if count >= args.limit:
            break
        in_batch.add(email)
        count += 1
        if not args.send:
            print(f"DRY RUN {count}: {email} | {row['Draft Subject']}")
            continue
        if service is None:
            from googleapiclient.discovery import build

            service = build("gmail", "v1", credentials=authenticate(), cache_discovery=False)
        message = EmailMessage()
        message["To"] = email
        message["Subject"] = row["Draft Subject"]
        message.set_content(row["Draft Body"])
        message.add_attachment(resume.read_bytes(), maintype="application", subtype="pdf", filename=resume.name)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        try:
            result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except Exception as exc:
            ws.cell(index, columns["Send Status"], "ERROR_UNCERTAIN")
            ws.cell(index, columns["Error"], f"{type(exc).__name__}: check Gmail Sent before retry")
            wb.save(args.input)
            raise RuntimeError(f"Send outcome uncertain for {email}; check Gmail Sent before retry") from exc
        message_id = clean(result.get("id"))
        sent_at = datetime.now(timezone.utc).isoformat()
        append_sent(log_path, email, row["Draft Subject"], message_id)
        previously_sent.add(email)
        ws.cell(index, columns["Send Status"], "SENT")
        ws.cell(index, columns["Sent At"], sent_at)
        ws.cell(index, columns["Gmail Message ID"], message_id)
        ws.cell(index, columns["Error"], "")
        wb.save(args.input)
        print(f"SENT {count}: {email}")
        if args.delay_seconds and count < args.limit:
            time.sleep(args.delay_seconds)
    if not args.send:
        print(f"Dry-run checked {count} approved rows; no messages sent")
    else:
        print(f"Sent {count} messages; log: {log_path}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("discover", help="Search Apollo for work contacts")
    p.add_argument("--companies", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--per-company", type=int, default=5)
    p.add_argument("--location", default=env("LOCATION", "India"))
    p.add_argument("--enrich", action="store_true", help="Reveal work emails; may consume Apollo credits")
    p = sub.add_parser("infer", help="Label unverified pattern guesses")
    p.add_argument("--contacts", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("draft", help="Create reviewable outreach workbook")
    p.add_argument("--contacts", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("send", help="Dry-run or send approved Gmail messages")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--delay-seconds", type=float, default=30)
    p.add_argument("--send", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        {"discover": discover, "infer": infer, "draft": draft, "send": send}[args.command](args)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
