import argparse
import base64
import os
import sys
import tempfile
import types
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

import coldbot
from openpyxl import load_workbook


class ColdBotTests(unittest.TestCase):
    def test_domain_match_rejects_similar_or_personal_domains(self):
        self.assertTrue(coldbot.email_matches_domain("a@example.com", "example.com"))
        self.assertTrue(coldbot.email_matches_domain("a@team.example.com", "example.com"))
        self.assertFalse(coldbot.email_matches_domain("a@example.co", "example.com"))
        self.assertFalse(coldbot.email_matches_domain("a@gmail.com", "example.com"))

    def test_infer_marks_guess_and_never_guesses_collision_or_bad_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "in.csv"
            target = Path(temporary) / "out.csv"
            coldbot.write_csv(source, [
                {"Company": "Example", "Domain": "example.com", "Name": "Alex Example",
                 "Email": "alex.example@example.com", "Email Type": "provided"},
                {"Company": "Example", "Domain": "example.com", "Name": "Sam Sample"},
                {"Company": "Example", "Domain": "example.com", "Name": "Unknown User"},
                {"Company": "Example", "Domain": "example.com", "Name": "Sam Sample"},
            ], coldbot.CONTACT_FIELDS)
            coldbot.infer(argparse.Namespace(contacts=source, out=target))
            rows = coldbot.read_csv(target)
            self.assertEqual(rows[1]["Email"], "sam.sample@example.com")
            self.assertEqual(rows[1]["Email Type"], "guessed")
            self.assertEqual(rows[2]["Email"], "")
            self.assertEqual(rows[3]["Email"], "")

    def test_draft_and_send_dry_run_block_guesses_and_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            contacts = directory / "contacts.csv"
            workbook = directory / "drafts.xlsx"
            resume = directory / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4\n")
            coldbot.write_csv(contacts, [
                {"Company": "Example", "Domain": "example.com", "Name": "Alex Example",
                 "Email": "alex@example.com", "Email Type": "verified", "Job Title": "Backend Engineer"},
                {"Company": "Example", "Domain": "example.com", "Name": "Sam Sample",
                 "Email": "sam@example.com", "Email Type": "guessed"},
                {"Company": "Example", "Domain": "example.com", "Name": "Other Example",
                 "Email": "alex@example.com", "Email Type": "verified"},
            ], coldbot.CONTACT_FIELDS)
            coldbot.draft(argparse.Namespace(contacts=contacts, out=workbook))
            wb = load_workbook(workbook)
            ws = wb["Drafts"]
            headers = {cell.value: cell.column for cell in ws[1]}
            self.assertEqual(ws.cell(2, headers["Approval Status"]).value, "REVIEW")
            self.assertEqual(ws.cell(3, headers["Approval Status"]).value, "BLOCKED")
            self.assertEqual(ws.cell(4, headers["Approval Status"]).value, "BLOCKED")
            for row in range(2, 5):
                ws.cell(row, headers["Approval Status"], "READY")
            wb.save(workbook)
            with patch.dict(os.environ, {
                "RESUME_PATH": str(resume), "SENT_LOG_PATH": str(directory / "sent.csv"),
            }):
                coldbot.send(argparse.Namespace(input=workbook, limit=5, delay_seconds=0, send=False))
            self.assertFalse((directory / "sent.csv").exists())

    def test_duplicate_email_prefers_verified_row_over_guess(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            contacts = directory / "contacts.csv"
            workbook = directory / "drafts.xlsx"
            coldbot.write_csv(contacts, [
                {"Company": "Example", "Domain": "example.com", "Name": "Alex Example",
                 "Email": "alex@example.com", "Email Type": "guessed"},
                {"Company": "Example", "Domain": "example.com", "Name": "Alex Example",
                 "Email": "alex@example.com", "Email Type": "verified"},
            ], coldbot.CONTACT_FIELDS)
            coldbot.draft(argparse.Namespace(contacts=contacts, out=workbook))
            ws = load_workbook(workbook)["Drafts"]
            headers = {cell.value: cell.column for cell in ws[1]}
            self.assertEqual(ws.cell(2, headers["Approval Status"]).value, "BLOCKED")
            self.assertEqual(ws.cell(3, headers["Approval Status"]).value, "REVIEW")

    def test_discover_requires_exact_org_domain(self):
        response = {"organizations": [
            {"id": "wrong", "primary_domain": "example.co"},
            {"id": "right", "primary_domain": "example.com"},
        ]}
        self.assertEqual(coldbot.select_organization(response, "example.com")["id"], "right")

    def test_discover_filters_country_and_mismatched_work_email(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            companies = directory / "companies.csv"
            contacts = directory / "contacts.csv"
            coldbot.write_csv(companies, [{"Company": "Example", "Domain": "example.com"}],
                              ["Company", "Domain"])
            responses = [
                {"organizations": [{"id": "org1", "primary_domain": "example.com"}]},
                {"contacts": [{"id": "p1", "name": "Alex Example", "country": "India"},
                              {"id": "p2", "name": "Sam Sample", "country": "United States"}]},
                {"person": {"id": "p1", "name": "Alex Example", "country": "India",
                            "email": "alex@gmail.com", "email_status": "verified"}},
                {"person": {"id": "p2", "name": "Sam Sample", "country": "United States",
                            "email": "sam@example.com", "email_status": "verified"}},
            ]
            with patch.dict(os.environ, {"APOLLO_API_KEY": "test-key"}), \
                    patch.object(coldbot, "api_post", side_effect=responses), \
                    patch.object(coldbot.time, "sleep"):
                coldbot.discover(argparse.Namespace(companies=companies, out=contacts,
                                                    per_company=5, location="India", enrich=True))
            rows = coldbot.read_csv(contacts)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Email"], "")
            self.assertEqual(rows[0]["Country"], "India")

    def test_mocked_gmail_send_attaches_resume_and_dedupes_rerun(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            contacts = directory / "contacts.csv"
            workbook = directory / "drafts.xlsx"
            resume = directory / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4\nprivate test resume")
            coldbot.write_csv(contacts, [{
                "Company": "Example", "Domain": "example.com", "Name": "Alex Example",
                "Email": "alex@example.com", "Email Type": "verified",
            }], coldbot.CONTACT_FIELDS)
            coldbot.draft(argparse.Namespace(contacts=contacts, out=workbook))
            wb = load_workbook(workbook)
            ws = wb["Drafts"]
            headers = {cell.value: cell.column for cell in ws[1]}
            ws.cell(2, headers["Approval Status"], "READY")
            wb.save(workbook)

            sent_payloads = []

            class FakeRequest:
                def execute(self):
                    return {"id": "test-message-id"}

            class FakeService:
                def users(self):
                    return self

                def messages(self):
                    return self

                def send(self, **kwargs):
                    sent_payloads.append(kwargs["body"]["raw"])
                    return FakeRequest()

            discovery = types.ModuleType("googleapiclient.discovery")
            discovery.build = lambda *args, **kwargs: FakeService()
            with patch.dict(os.environ, {
                "RESUME_PATH": str(resume), "SENT_LOG_PATH": str(directory / "sent.csv"),
            }), patch.dict(sys.modules, {
                "googleapiclient": types.ModuleType("googleapiclient"),
                "googleapiclient.discovery": discovery,
            }), patch.object(coldbot, "authenticate", return_value=object()):
                args = argparse.Namespace(input=workbook, limit=5, delay_seconds=0, send=True)
                coldbot.send(args)
                coldbot.send(args)
            self.assertEqual(len(sent_payloads), 1)
            mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(sent_payloads[0]))
            attachments = list(mime.iter_attachments())
            self.assertEqual(len(attachments), 1)
            self.assertEqual(attachments[0].get_payload(decode=True), resume.read_bytes())
            self.assertEqual(len(coldbot.read_csv(directory / "sent.csv")), 1)
            self.assertEqual(load_workbook(workbook)["Drafts"].cell(2, headers["Send Status"]).value, "SENT")


if __name__ == "__main__":
    unittest.main()
