"""Django tests:  python manage.py test"""
import json
import io
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .image_storage import delete_question_image, upload_question_image
from .models import (
    AIExamAssessment, AIRequestQuota, BackupSnapshot, Comment, ExamAttempt, ExamTemplate, ExamTemplateChapter, ImageTarget, Option, Question, QuestionBank,
    QuestionImage, QuestionNote, SavedQuestion, Subject, SubjectSection, Book, BookChapter, TopicSearch,
)
from . import full_backup
from .subject_backup import encode_backup, validate_backup

SAMPLE = """Q: Pick B
A. one
B. two
ANSWER: B
EXPLANATION: because

Q: Statements
TYPE: TF
A. yes
B. no
ANSWER: TF
"""
DESCRIPTIVE_SAMPLE = """Q: Explain the resting membrane potential.
TYPE: DESC
LABEL: U of K
ANSWER: The voltage difference across a cell membrane at rest.
"""


def give(user, *codenames):
    for c in codenames:
        user.user_permissions.add(Permission.objects.get(codename=c))


class Base(TestCase):
    def setUp(self):
        cache.clear()  # home page counts are cached for a week
        self.subject = Subject.objects.create(name="Physiology")
        self.editor = User.objects.create_user("ed", password="pw")
        give(self.editor, "add_question", "change_question", "delete_question")
        self.student = User.objects.create_user("stu", password="pw")

    def import_sample(self, title="Haematology", semester=1):
        self.client.force_login(self.editor)
        self.client.post(reverse("bank:import"), {
            "action": "commit", "text": SAMPLE, "subject": self.subject.pk,
            "semester": semester, "deck_title": title, "skip_dupes": "1"})
        self.client.logout()
        return QuestionBank.objects.get(title=title)

    def post_json(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")


class SubjectBackupTests(Base):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("backup-admin", "admin@example.com", "secret")

    def old_export(self):
        """A file in the older questions-only format, which is only ever added, never replaced."""
        return SimpleNamespace(content=encode_backup())

    def test_old_format_export_includes_answer_content(self):
        deck = self.import_sample()
        descriptive = Question.objects.create(
            bank=deck, kind=Question.DESC, stem="Describe resting potential.",
            answer="A voltage difference at rest.", explanation="Explanation text.",
            note="Source note.", label="U of K", is_lab=True,
        )
        answer_image = QuestionImage.objects.create(
            bank=deck, public_id="supabase:answer-diagram.png",
            secure_url="https://example.supabase.co/answer-diagram.png",
            original_name="answer-diagram.png", width=640, height=480,
        )
        descriptive.answer_image_asset = answer_image
        descriptive.save(update_fields=["answer_image_asset"])
        second_subject = Subject.objects.create(name="Anatomy")
        second_deck = QuestionBank.objects.create(
            subject=second_subject, semester=2, title="Anatomy basics",
        )
        Question.objects.create(
            bank=second_deck, kind=Question.DESC,
            stem="Name the body system.", answer="The skeletal system.",
        )
        payload = json.loads(self.old_export().content)
        self.assertEqual(
            {subject["name"] for subject in payload["subjects"]},
            {"Physiology", "Anatomy"},
        )
        subject_data = next(item for item in payload["subjects"] if item["name"] == "Physiology")
        deck_data = subject_data["decks"][0]
        self.assertEqual(subject_data["name"], self.subject.name)
        self.assertEqual(deck_data["title"], deck.title)
        self.assertIn(descriptive.answer, json.dumps(payload))
        self.assertEqual(
            next(q for q in deck_data["questions"] if q["stem"] == descriptive.stem)["label"],
            "U of K",
        )
        self.assertEqual(
            next(q for q in deck_data["questions"] if q["stem"] == descriptive.stem)[
                "answer_image_asset_id"
            ],
            answer_image.pk,
        )
        self.assertTrue(
            next(q for q in deck_data["questions"] if q["stem"] == descriptive.stem)["is_lab"],
        )
        self.assertEqual(
            {question["kind"] for question in deck_data["questions"]},
            {Question.MCQ, Question.TF, Question.DESC},
        )
        mcq = next(q for q in deck_data["questions"] if q["kind"] == Question.MCQ)
        self.assertEqual(len(mcq["options"]), 2)
        self.assertTrue(any(option["is_correct"] for option in mcq["options"]))

    def test_restore_adds_deleted_content_and_skips_matching_records(self):
        deck = self.import_sample()
        self.client.force_login(self.admin)
        backup = self.old_export()
        payload = json.loads(backup.content)
        original_questions = list(deck.questions.order_by("pk"))
        removed_question = original_questions[0]
        untouched_question = original_questions[1]
        original_max_pk = max(question.pk for question in original_questions)
        untouched_stem = untouched_question.stem
        removed_id = removed_question.pk
        removed_question.delete()

        upload = SimpleUploadedFile("backup.json", backup.content, content_type="application/json")
        response = self.client.post(
            reverse("bank:import_subject_backup"), {"backup": upload}, follow=True,
        )
        self.assertEqual(response.status_code, 200)
        restored_question = Question.objects.get(pk=removed_id)
        self.assertEqual(restored_question.stem, payload["subjects"][0]["decks"][0]["questions"][0]["stem"])
        self.assertEqual(restored_question.options.count(), 2)
        new_question = Question.objects.create(bank=deck, stem="Created after restore")
        self.assertGreater(new_question.pk, original_max_pk)
        untouched_question.refresh_from_db()
        self.assertEqual(untouched_question.stem, untouched_stem)
        self.assertContains(
            response,
            "Added 0 subjects, 0 sections, 0 decks, 1 question, 2 options",
        )
        self.assertContains(response, "skipped")

    def test_conflict_rejects_entire_import_without_overwriting_or_partial_restore(self):
        deck = self.import_sample()
        self.client.force_login(self.admin)
        backup = self.old_export()
        questions = list(deck.questions.order_by("pk"))
        missing_question_id = questions[1].pk
        questions[1].delete()
        questions[0].stem = "Changed after the backup"
        questions[0].save(update_fields=["stem"])

        upload = SimpleUploadedFile("backup.json", backup.content, content_type="application/json")
        response = self.client.post(
            reverse("bank:import_subject_backup"), {"backup": upload}, follow=True,
        )
        self.assertContains(response, "already exists with different content")
        self.assertFalse(Question.objects.filter(pk=missing_question_id).exists())
        self.assertEqual(Question.objects.count(), 1)
        self.assertEqual(Question.objects.get(pk=questions[0].pk).stem, "Changed after the backup")

    def test_invalid_backup_does_not_write_and_import_is_staff_only(self):
        self.client.force_login(self.student)
        denied = self.client.post(reverse("bank:import_subject_backup"), {})
        self.assertEqual(denied.status_code, 302)

        self.client.force_login(self.admin)
        upload = SimpleUploadedFile("backup.json", b"not JSON", content_type="application/json")
        response = self.client.post(
            reverse("bank:import_subject_backup"), {"backup": upload}, follow=True,
        )
        self.assertContains(response, "not valid JSON")
        self.assertEqual(Subject.objects.count(), 1)
        self.assertEqual(QuestionBank.objects.count(), 0)

    def test_upload_size_limit_is_enforced(self):
        self.client.force_login(self.admin)
        upload = SimpleUploadedFile("backup.json", b"{}", content_type="application/json")
        with patch("bank.full_backup.MAX_BACKUP_BYTES", 1):
            response = self.client.post(
                reverse("bank:import_subject_backup"), {"backup": upload}, follow=True,
            )
        self.assertContains(response, "larger than the 100 MB limit")
        self.assertEqual(Subject.objects.count(), 1)
        with patch("bank.full_backup.MAX_BACKUP_BYTES", 1):
            response = self.client.get(reverse("bank:export_subject_backup"))
        self.assertEqual(response.status_code, 413)

    def test_backup_restores_sections_and_image_question_metadata(self):
        subject = Subject.objects.create(name="Anatomy")
        section = SubjectSection.objects.create(
            subject=subject, name="Gross Anatomy", slug="gross-anatomy", order=2,
        )
        deck = QuestionBank.objects.create(
            subject=subject, section=section, semester=2, title="Thorax",
        )
        image = QuestionImage.objects.create(
            bank=deck, public_id="supabase:diagram.png",
            secure_url="https://example.supabase.co/storage/v1/object/public/"
            "rawasi-question-images/diagram.png",
            original_name="diagram.png", width=1200, height=800,
        )
        question = Question.objects.create(
            bank=deck, kind=Question.IMAGE, image_asset=image,
            image_answer_mode=Question.IMAGE_IDENTIFY, stem="Identify the image.", is_lab=True,
        )
        target = ImageTarget.objects.create(
            question=question, label="A", answer="Test structure",
            x_percent=25, y_percent=60, order=0,
        )
        self.client.force_login(self.admin)
        backup = self.old_export()

        payload = validate_backup(backup.content)
        exported_subject = next(item for item in payload["subjects"] if item["id"] == subject.pk)
        exported_deck = exported_subject["decks"][0]
        exported_question = exported_deck["questions"][0]
        self.assertEqual(exported_deck["section_id"], section.pk)
        self.assertEqual(exported_question["kind"], Question.IMAGE)
        self.assertTrue(exported_question["is_lab"])
        self.assertEqual(exported_question["image_asset_id"], image.pk)
        self.assertEqual(exported_question["image_targets"][0]["id"], target.pk)
        subject_id, section_id, deck_id = subject.pk, section.pk, deck.pk
        image_id, question_id, target_id = image.pk, question.pk, target.pk

        question.delete()
        image.delete()
        deck.delete()
        section.delete()
        subject.delete()

        upload = SimpleUploadedFile("backup.json", backup.content, content_type="application/json")
        response = self.client.post(
            reverse("bank:import_subject_backup"), {"backup": upload}, follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "older questions-only backup, so nothing was deleted")
        restored_subject = Subject.objects.get(pk=subject_id)
        restored_deck = QuestionBank.objects.get(pk=deck_id)
        restored_question = Question.objects.get(pk=question_id)
        self.assertEqual(restored_subject.sections.get(pk=section_id).name, "Gross Anatomy")
        self.assertEqual(restored_deck.section_id, section_id)
        self.assertEqual(restored_question.image_asset_id, image_id)
        self.assertTrue(restored_question.is_lab)
        restored_target = restored_question.image_targets.get(pk=target_id)
        self.assertEqual(restored_target.answer, "Test structure")
        self.assertEqual(float(restored_target.x_percent), 25)

    def test_old_backup_with_image_questions_is_rejected_before_writing(self):
        deck = self.import_sample()
        image = QuestionImage.objects.create(
            bank=deck, public_id="supabase:old.png",
            secure_url="https://example.supabase.co/old.png",
            original_name="old.png", width=1, height=1,
        )
        Question.objects.create(bank=deck, kind=Question.IMAGE, image_asset=image, stem="Old image.")
        self.client.force_login(self.admin)
        backup = json.loads(self.old_export().content)
        backup["version"] = 1
        for subject in backup["subjects"]:
            subject.pop("sections", None)
            for item in subject["decks"]:
                item.pop("section_id", None)
                item.pop("images", None)
                for item_question in item["questions"]:
                    item_question.pop("image_asset_id", None)
                    item_question.pop("image_answer_mode", None)
                    item_question.pop("image_targets", None)

        upload = SimpleUploadedFile(
            "old-backup.json", json.dumps(backup).encode(),
            content_type="application/json",
        )
        response = self.client.post(
            reverse("bank:import_subject_backup"), {"backup": upload}, follow=True,
        )
        self.assertContains(response, "version 1 backup contains image questions")
        self.assertEqual(Question.objects.count(), 3)


class FullBackupTests(Base):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("backup-admin", "admin@example.com", "secret")
        self.deck = self.import_sample()
        self.question = self.deck.questions.order_by("pk").first()
        book = Book.objects.create(title="Gray’s Anatomy Review")
        self.chapter = BookChapter.objects.create(book=book, number=1, title="Thorax")
        self.question.book_chapter = self.chapter
        self.question.save(update_fields=["book_chapter"])
        SavedQuestion.objects.create(user=self.student, question=self.question)
        QuestionNote.objects.create(user=self.student, question=self.question, text="my private note")
        Comment.objects.create(user=self.student, question=self.question, text="typo in option B")
        self.template = ExamTemplate.objects.create(user=self.student, bank=self.deck, name="Mock", question_count=2)
        ExamAttempt.objects.create(user=self.student, bank=self.deck, title="Try 1", question_ids=[self.question.pk])

    def download(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("bank:export_subject_backup"))
        self.assertEqual(response.status_code, 200)
        return response

    def upload(self, content, confirm="REPLACE", name="backup.json.gz"):
        return self.client.post(reverse("bank:import_subject_backup"), {
            "backup": SimpleUploadedFile(name, content, content_type="application/gzip"), "confirm": confirm,
        }, follow=True)

    def test_download_holds_the_whole_site_but_no_notes_or_comments(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("bank:export_subject_backup")).status_code, 302)
        response = self.download()
        self.assertEqual(response["Content-Type"], "application/gzip")
        self.assertIn("deca-shine-backup-", response["Content-Disposition"])
        payload = full_backup.decode(response.content)
        self.assertEqual(payload["format"], "deca-shine-full-backup")
        counts = payload["counts"]
        for label in ("auth.User", "bank.Question", "bank.Option", "bank.Book", "bank.BookChapter",
                      "bank.SavedQuestion", "bank.ExamTemplate", "bank.ExamAttempt"):
            self.assertGreater(counts[label], 0, label)
        self.assertNotIn("bank.QuestionNote", counts)
        self.assertNotIn("bank.Comment", counts)
        text = json.dumps(payload)
        self.assertNotIn("my private note", text)
        self.assertNotIn("typo in option B", text)
        editor = next(r for r in payload["records"] if r["model"] == "auth.user" and r["fields"]["username"] == "ed")
        self.assertIn(["bank", "question", "add_question"], editor["fields"]["user_permissions"])

    def test_import_saves_a_restore_point_then_replaces_everything(self):
        backup = self.download().content
        created = self.question.created_at
        # Change the site after the backup.
        Question.objects.create(bank=self.deck, stem="Added after the backup")
        newcomer = User.objects.create_user("newcomer", password="pw")
        self.student.delete()

        response = self.upload(backup)
        self.assertContains(response, "a copy of the previous database was saved as a restore point")
        self.assertEqual(Question.objects.count(), 2)
        self.assertFalse(Question.objects.filter(stem="Added after the backup").exists())
        self.assertFalse(User.objects.filter(pk=newcomer.pk).exists())
        student = User.objects.get(username="stu")
        self.assertTrue(student.check_password("pw"))
        self.assertTrue(student.saved_questions.exists())
        self.assertEqual(Question.objects.get(pk=self.question.pk).created_at, created)
        self.assertEqual(Question.objects.get(pk=self.question.pk).book_chapter_id, self.chapter.pk)
        self.assertTrue(User.objects.get(username="ed").has_perm("bank.add_question"))
        self.assertEqual(Comment.objects.count(), 0)  # not part of downloads
        self.assertEqual(QuestionNote.objects.count(), 0)
        # The admin is still signed in, and new rows get fresh IDs.
        self.assertEqual(self.client.get(reverse("bank:subject_backup")).status_code, 200)
        self.assertGreater(Question.objects.create(bank=self.deck, stem="new").pk, self.question.pk)

        point = BackupSnapshot.objects.get()
        self.assertIn("backup.json.gz", point.reason)
        saved = full_backup.decode(bytes(point.data))
        self.assertTrue(saved["includes_private"])
        self.assertEqual(saved["counts"]["bank.Question"], 3)
        self.assertEqual(saved["counts"]["bank.Comment"], 1)  # restore points keep comments
        self.assertContains(self.client.get(reverse("bank:subject_backup")), "Restore this version")

    def test_restore_point_brings_back_the_previous_version_including_notes(self):
        backup = self.download().content
        Question.objects.create(bank=self.deck, stem="Only in the newer version")
        self.upload(backup)
        self.assertFalse(Question.objects.filter(stem="Only in the newer version").exists())
        point = BackupSnapshot.objects.get()

        download = self.client.get(reverse("bank:download_restore_point", args=[point.pk]))
        self.assertEqual(full_backup.decode(download.content)["counts"]["bank.Question"], 3)

        refused = self.client.post(reverse("bank:restore_restore_point", args=[point.pk]), {"confirm": ""}, follow=True)
        self.assertContains(refused, "Type REPLACE")
        self.assertFalse(Question.objects.filter(stem="Only in the newer version").exists())

        response = self.client.post(
            reverse("bank:restore_restore_point", args=[point.pk]), {"confirm": "REPLACE"}, follow=True,
        )
        self.assertContains(response, "restore point")
        self.assertTrue(Question.objects.filter(stem="Only in the newer version").exists())
        self.assertEqual(QuestionNote.objects.get().text, "my private note")
        self.assertEqual(Comment.objects.get().text, "typo in option B")
        self.assertEqual(BackupSnapshot.objects.count(), 2)

    def test_nothing_changes_without_confirmation_or_with_a_bad_file(self):
        backup = self.download().content
        Question.objects.create(bank=self.deck, stem="Keep me")
        self.assertContains(self.upload(backup, confirm="yes"), "Type REPLACE")
        self.assertTrue(Question.objects.filter(stem="Keep me").exists())
        self.assertFalse(BackupSnapshot.objects.exists())

        payload = full_backup.decode(backup)
        payload["records"] = [r for r in payload["records"] if r["model"] != "auth.user"]
        response = self.upload(full_backup.encode(payload))
        self.assertContains(response, "no user accounts")
        self.assertTrue(Question.objects.filter(stem="Keep me").exists())

        payload = full_backup.decode(backup)
        for record in payload["records"]:
            if record["model"] == "auth.user":
                record["fields"]["is_staff"] = False
        self.assertContains(self.upload(full_backup.encode(payload)), "no active admin account")

        self.assertContains(self.upload(b"\x1f\x8bbroken"), "damaged")
        self.assertTrue(Question.objects.filter(stem="Keep me").exists())
        self.assertFalse(BackupSnapshot.objects.exists())

    def test_only_the_last_five_restore_points_are_kept(self):
        for _ in range(7):
            full_backup.save_snapshot("test")
        self.assertEqual(BackupSnapshot.objects.count(), 5)


class PwaTests(TestCase):
    def test_manifest_and_service_worker_are_public(self):
        manifest_response = self.client.get(reverse("bank:manifest"))
        self.assertEqual(manifest_response.status_code, 200)
        self.assertEqual(manifest_response["Content-Type"], "application/manifest+json")
        manifest = json.loads(b"".join(manifest_response.streaming_content))
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual({icon["sizes"] for icon in manifest["icons"]}, {"192x192", "512x512"})

        worker_response = self.client.get(reverse("bank:service_worker"))
        self.assertEqual(worker_response.status_code, 200)
        self.assertEqual(worker_response["Service-Worker-Allowed"], "/")
        worker = b"".join(worker_response.streaming_content)
        self.assertIn(b"/static/", worker)
        self.assertNotIn(b"/api/", worker)

    def test_install_banner_only_renders_for_authenticated_users(self):
        login_page = self.client.get(reverse("bank:login"))
        self.assertNotContains(login_page, 'id="pwa-install"')
        self.assertContains(login_page, 'alt="Rawasi — Batch 101"')
        user = User.objects.create_user("pwa-user", password="pw")
        self.client.force_login(user)
        home = self.client.get(reverse("bank:home"))
        self.assertContains(home, 'id="pwa-install"')
        self.assertContains(home, "Install Rawasi 101")
        self.assertContains(home, 'alt="Rawasi — Batch 101"')
        self.assertNotContains(home, 'class="theme-toggle"')
        self.assertContains(home, reverse("bank:settings"))
        bank = QuestionBank.objects.create(subject=Subject.objects.create(name="PWA"), title="Install test")
        deck = self.client.get(bank.get_absolute_url())
        self.assertNotContains(deck, 'id="pwa-install"')


class UserSettingsTests(Base):
    def test_settings_page_is_authenticated_and_has_language_choices(self):
        url = reverse("bank:settings")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.student)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="theme-toggle"')
        self.assertContains(response, "Response language")
        for site in ("chatgpt", "claude", "gemini", "deepseek", "grok", "copilot", "perplexity"):
            self.assertContains(response, f'name="ai-site" value="{site}"')
        self.assertContains(response, 'name="ai-depth" value="regular"')
        self.assertContains(response, 'name="ai-depth" value="deep"')
        self.assertContains(response, 'value="en"')
        self.assertContains(response, 'value="ar"')
        self.assertContains(response, f'data-user="{self.student.pk}"')
        self.assertContains(response, "Change password")
        self.assertContains(response, "Student")

    def test_password_change_checks_the_old_password_and_keeps_this_device_signed_in(self):
        self.client.force_login(self.student)
        url = reverse("bank:settings")
        wrong = self.client.post(url, {
            "action": "password", "old_password": "nope",
            "new_password1": "Fresh-Pass-2026", "new_password2": "Fresh-Pass-2026",
        })
        self.assertEqual(wrong.status_code, 200)
        self.assertContains(wrong, "Your old password was entered incorrectly")
        self.assertContains(wrong, '<details class="settings-password" open>')
        self.student.refresh_from_db()
        self.assertTrue(self.student.check_password("pw"))

        done = self.client.post(url, {
            "action": "password", "old_password": "pw",
            "new_password1": "Fresh-Pass-2026", "new_password2": "Fresh-Pass-2026",
        }, follow=True)
        self.assertContains(done, "Password changed")
        self.student.refresh_from_db()
        self.assertTrue(self.student.check_password("Fresh-Pass-2026"))
        self.assertEqual(self.client.get(url).status_code, 200)  # still signed in


class SubjectSectionSeedTests(TestCase):
    def test_seed_subjects_creates_default_anatomy_sections(self):
        call_command("seed_subjects", stdout=io.StringIO())
        anatomy = Subject.objects.get(slug="anatomy")
        self.assertEqual(
            list(anatomy.sections.values_list("name", flat=True)),
            ["Embryology", "Histology", "Gross Anatomy"],
        )


class ReviewWorkflowTests(Base):
    def make_staff_user(self):
        return User.objects.create_user("review-admin", password="review-test-password", is_staff=True)

    def test_review_mode_and_edit_endpoint_are_staff_only(self):
        deck = self.import_sample()
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse("bank:review_deck", args=[deck.slug])).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("bank:api_questions", args=[deck.slug]) + "?review=1").status_code,
            403,
        )
        question = deck.questions.first()
        self.assertEqual(
            self.post_json(reverse("bank:api_question_edit", args=[question.pk]), {}).status_code,
            403,
        )

    def test_deck_has_separate_review_actions_for_each_question_type(self):
        deck = self.import_sample()
        Question.objects.create(
            bank=deck, kind=Question.DESC, stem="Describe a teacup.", answer="A very small bathtub."
        )
        self.client.force_login(self.make_staff_user())
        page = self.client.get(deck.get_absolute_url())
        self.assertContains(
            page,
            f'href="{reverse("bank:quiz", args=[deck.slug])}?type=mcq_tf" '
            'aria-label="Start MCQs and True / False quiz"',
        )
        self.assertContains(
            page,
            f'href="{reverse("bank:quiz", args=[deck.slug])}?type=descriptive" '
            'aria-label="Start Descriptive quiz"',
        )
        self.assertContains(page, f'href="{reverse("bank:review_deck", args=[deck.slug])}?type=mcq_tf"')
        self.assertContains(page, f'href="{reverse("bank:review_deck", args=[deck.slug])}?type=descriptive"')

        mcq_review = self.client.get(
            reverse("bank:review_deck", args=[deck.slug]), {"type": "mcq_tf"})
        self.assertContains(mcq_review, "?review=1&amp;type=mcq_tf")
        mcq_ids = {
            question["kind"]
            for question in self.client.get(
                reverse("bank:api_questions", args=[deck.slug]), {"review": "1", "type": "mcq_tf"}
            ).json()["questions"]
        }
        self.assertEqual(mcq_ids, {Question.MCQ, Question.TF})

        descriptive_review = self.client.get(
            reverse("bank:review_deck", args=[deck.slug]), {"type": "descriptive"})
        self.assertContains(descriptive_review, "?review=1&amp;type=descriptive")

    def test_deck_source_filter_shows_counts_and_preserves_selected_mode(self):
        deck = self.import_sample()
        self.client.force_login(self.student)
        Question.objects.create(
            bank=deck, kind=Question.DESC, stem="U of K question.",
            answer="Answer.", label="U of K",
        )
        Question.objects.create(
            bank=deck, kind=Question.DESC, stem="Unlabelled question.",
            answer="Answer.",
        )
        page = self.client.get(deck.get_absolute_url())
        self.assertContains(page, 'id="study-label-filter"')
        self.assertContains(page, "U of K only (1)")
        self.assertContains(page, "Other questions (3)")
        self.assertContains(page, 'data-counts="{&quot;all&quot;: 4, &quot;uofk&quot;: 1, &quot;other&quot;: 3}"')

        selected = self.client.get(deck.get_absolute_url(), {"label": "uofk"})
        self.assertContains(selected, '<option value="uofk" selected>')

        quiz_page = self.client.get(reverse("bank:quiz", args=[deck.slug]), {
            "type": "descriptive", "label": "other",
        })
        self.assertContains(quiz_page, f'data-key="deck:{deck.slug}:descriptive:label:other"')
        self.assertContains(
            quiz_page,
            f'data-api="{reverse("bank:api_questions", args=[deck.slug])}?type=descriptive&amp;label=other"',
        )

    def test_review_edit_saves_and_moves_to_bin_without_deleting_question(self):
        deck = self.import_sample()
        question = deck.questions.get(kind=Question.MCQ)
        self.client.force_login(self.make_staff_user())
        review_url = reverse("bank:api_questions", args=[deck.slug]) + "?review=1"
        review_question = self.client.get(review_url).json()["questions"][0]
        self.assertIn("answer", review_question)
        self.assertIn("explanation", review_question)

        payload = {
            "kind": question.kind, "stem": "Corrected stem for review.", "answer": "",
            "explanation": "Updated explanation.", "note": "Checked by staff.",
            "is_in_sorting_bin": True,
            "options": [
                {"text": option.text, "is_correct": option.is_correct}
                for option in question.options.all()
            ],
        }
        response = self.post_json(reverse("bank:api_question_edit", args=[question.pk]), payload)
        self.assertEqual(response.status_code, 200, response.content)
        question.refresh_from_db()
        self.assertEqual(question.stem, "Corrected stem for review.")
        self.assertTrue(question.is_in_sorting_bin)
        self.assertEqual(question.sorting_bin_at is not None, True)
        self.assertEqual(
            [q["id"] for q in self.client.get(
                reverse("bank:api_questions", args=[deck.slug]) + "?type=mcq_tf"
            ).json()["questions"]],
            [deck.questions.get(kind=Question.TF).pk],
        )
        self.assertEqual(
            self.client.get(reverse("bank:api_sorting_bin_questions")).json()["questions"][0]["id"],
            question.pk,
        )

    def test_staff_can_move_question_from_quiz_to_sorting_bin(self):
        deck = self.import_sample()
        question = deck.questions.get(kind=Question.MCQ)
        endpoint = reverse("bank:api_question_sorting_bin", args=[question.pk])

        self.client.force_login(self.editor)
        self.assertEqual(self.post_json(endpoint, {}).status_code, 403)

        self.client.force_login(self.make_staff_user())
        quiz_page = self.client.get(reverse("bank:quiz", args=[deck.slug]))
        self.assertContains(quiz_page, 'data-can-sort="1"')
        response = self.post_json(endpoint, {})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["moved"])

        question.refresh_from_db()
        self.assertTrue(question.is_in_sorting_bin)
        live_questions = self.client.get(
            reverse("bank:api_questions", args=[deck.slug]), {"type": "mcq_tf"}
        ).json()["questions"]
        self.assertNotIn(question.pk, [item["id"] for item in live_questions])
        bin_questions = self.client.get(reverse("bank:api_sorting_bin_questions")).json()["questions"]
        self.assertIn(question.pk, [item["id"] for item in bin_questions])

    def test_sorting_bin_page_offers_copyable_ai_reorganization_prompt(self):
        self.import_sample()
        self.client.force_login(self.make_staff_user())
        page = self.client.get(reverse("bank:sorting_bin"))

        self.assertContains(page, 'id="copy-sorting-prompt"')
        self.assertContains(page, "View the AI reorganization prompt")
        self.assertContains(page, "Reorganize these questions into the exact same plain-text format.")
        self.assertContains(
            page,
            "Available destination decks, grouped by SOURCE_SUBJECT.",
        )
        self.assertContains(
            page,
            "SOURCE_SUBJECT: Physiology",
        )
        self.assertContains(page, "TARGET_SEMESTER: 1 | TARGET_DECK: Haematology")
        self.assertContains(page, "Do not transfer questions between subjects")
        self.assertContains(page, "Never move a question to a different subject.")
        self.assertContains(page, "give it to your AI together with the exported sorting-bin text")

    def test_sorting_bin_questions_are_grouped_by_original_chapter(self):
        deck = self.import_sample()
        other_deck = QuestionBank.objects.create(
            subject=self.subject, semester=2, title="Cardiology"
        )
        first = deck.questions.first()
        first.is_in_sorting_bin = True
        first.save(update_fields=["is_in_sorting_bin"])
        second = Question.objects.create(
            bank=other_deck, kind=Question.DESC, stem="Describe a pulse.", answer="A beat."
        )
        second.is_in_sorting_bin = True
        second.save(update_fields=["is_in_sorting_bin"])

        self.client.force_login(self.make_staff_user())
        page = self.client.get(reverse("bank:sorting_bin"))
        content = page.content.decode()
        self.assertLess(content.index("Semester 1 › Haematology"), content.index("Semester 2 › Cardiology"))
        self.assertContains(page, "Semester 1 › Haematology")
        self.assertContains(page, "Semester 2 › Cardiology")

    def test_review_bin_tracks_questions_removed_from_the_active_deck(self):
        deck = self.import_sample()
        question = deck.questions.first()
        self.client.force_login(self.make_staff_user())

        response = self.client.post(reverse("bank:api_question_review_bin", args=[question.pk]))
        self.assertEqual(response.status_code, 200)
        question.refresh_from_db()
        self.assertTrue(question.is_in_review_bin)
        self.assertFalse(question.is_in_sorting_bin)

        page = self.client.get(reverse("bank:review_bin"))
        self.assertContains(page, "Review bin")
        self.assertContains(page, "Q" + str(question.number or question.pk))
        self.assertNotIn(question.pk, [item["id"] for item in self.client.get(
            reverse("bank:api_questions", args=[deck.slug]), {"type": "mcq_tf"}
        ).json()["questions"]])

    def test_staff_can_delete_a_question_from_review_mode(self):
        deck = self.import_sample()
        question = deck.questions.first()
        self.client.force_login(self.make_staff_user())
        response = self.post_json(
            reverse("bank:api_question_edit", args=[question.pk]),
            {"delete_question": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])
        self.assertFalse(Question.objects.filter(pk=question.pk).exists())

    def test_true_false_review_allows_every_statement_to_be_false(self):
        deck = self.import_sample()
        question = deck.questions.get(kind=Question.TF)
        self.client.force_login(self.make_staff_user())
        response = self.post_json(reverse("bank:api_question_edit", args=[question.pk]), {
            "kind": Question.TF, "stem": question.stem, "answer": "",
            "explanation": "", "note": "", "is_in_sorting_bin": False,
            "options": [
                {"text": option.text, "is_correct": False}
                for option in question.options.all()
            ],
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(any(option["is_correct"] for option in response.json()["question"]["options"]))

    def test_sorting_bin_export_preview_and_reimport_preserve_question_identity(self):
        deck = self.import_sample()
        question = deck.questions.get(kind=Question.MCQ)
        question.is_in_sorting_bin = True
        question.save(update_fields=["is_in_sorting_bin"])
        self.client.force_login(self.make_staff_user())

        exported = self.client.get(reverse("bank:sorting_bin_export")).content.decode()
        self.assertIn(f"ID: {question.pk}", exported)
        reorganized = exported.replace("TARGET_SUBJECT:", "TARGET_SUBJECT: Physiology")
        reorganized = reorganized.replace("TARGET_SEMESTER:", "TARGET_SEMESTER: 2")
        reorganized = reorganized.replace("TARGET_DECK:", "TARGET_DECK: Corrected Chapter")
        reorganized = reorganized.replace("Q: Pick B", "Q: Rewritten question from the sorting bin")
        url = reverse("bank:sorting_bin_import")

        preview_response = self.client.post(url, {"action": "preview", "text": reorganized})
        self.assertRedirects(preview_response, url)
        preview = self.client.get(url)
        self.assertContains(preview, "Review destination chapters")
        self.assertContains(preview, "Ready</span>")
        self.assertContains(preview, "Semester 2 › Corrected Chapter")
        self.assertContains(preview, "Apply and refile 1 question")
        question.refresh_from_db()
        self.assertTrue(question.is_in_sorting_bin)
        self.assertEqual(question.stem, "Pick B")
        self.assertFalse(QuestionBank.objects.filter(subject=self.subject, semester=2, title="Corrected Chapter").exists())

        response = self.client.post(url, {"action": "commit"})
        self.assertEqual(response.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.stem, "Rewritten question from the sorting bin")
        self.assertFalse(question.is_in_sorting_bin)
        self.assertEqual(question.bank.title, "Corrected Chapter")
        self.assertEqual(question.bank.semester, 2)
        self.assertEqual(question.bank.subject.name, "Physiology")

    def test_sorting_bin_preview_can_be_edited_or_discarded_without_applying(self):
        deck = self.import_sample()
        question = deck.questions.get(kind=Question.MCQ)
        question.is_in_sorting_bin = True
        question.save(update_fields=["is_in_sorting_bin"])
        self.client.force_login(self.make_staff_user())
        url = reverse("bank:sorting_bin_import")
        text = self.client.get(reverse("bank:sorting_bin_export")).content.decode()

        self.assertRedirects(
            self.client.post(url, {"action": "preview", "text": text}), url)
        preview = self.client.get(url)
        self.assertContains(preview, "Edit imported text")
        self.assertContains(preview, "Discard preview")

        edit_response = self.client.post(url, {"action": "edit"})
        edit_url = url + "?edit=1"
        self.assertRedirects(edit_response, edit_url)
        self.assertContains(self.client.get(edit_url), f"ID: {question.pk}")

        self.assertRedirects(self.client.post(url, {"action": "cancel"}), url)
        self.assertNotIn("sorting_bin_pending_import", self.client.session)
        question.refresh_from_db()
        self.assertTrue(question.is_in_sorting_bin)

    def test_sorting_bin_reimport_rejects_cross_subject_reassignment(self):
        deck = self.import_sample()
        other_subject, _ = Subject.objects.get_or_create(name="Anatomy", defaults={"order": 0})
        question = deck.questions.get(kind=Question.MCQ)
        question.is_in_sorting_bin = True
        question.save(update_fields=["is_in_sorting_bin"])
        self.client.force_login(self.make_staff_user())

        exported = self.client.get(reverse("bank:sorting_bin_export")).content.decode()
        reorganized = exported.replace("TARGET_SUBJECT:", "TARGET_SUBJECT: Anatomy")
        reorganized = reorganized.replace("TARGET_SEMESTER:", "TARGET_SEMESTER: 1")
        reorganized = reorganized.replace("TARGET_DECK:", "TARGET_DECK: Anatomy")
        preview_url = reverse("bank:sorting_bin_import")
        preview_response = self.client.post(
            preview_url, {"action": "preview", "text": reorganized})
        self.assertRedirects(preview_response, preview_url)
        preview = self.client.get(preview_url)
        self.assertContains(preview, "cannot be moved to another subject")

        response = self.client.post(preview_url, {"action": "commit"})
        self.assertEqual(response.status_code, 200)
        question.refresh_from_db()
        self.assertTrue(question.is_in_sorting_bin)
        self.assertEqual(question.bank.subject, self.subject)
        self.assertFalse(QuestionBank.objects.filter(subject=other_subject, title="Anatomy").exists())


class BrowseAndImportTests(Base):
    def anatomy_sections(self):
        anatomy, _ = Subject.objects.get_or_create(name="Anatomy", defaults={"order": 0})
        sections = []
        for order, name in enumerate(("Embryology", "Histology", "Gross Anatomy")):
            section, _ = SubjectSection.objects.get_or_create(
                subject=anatomy, name=name, defaults={"order": order},
            )
            sections.append(section)
        return anatomy, sections

    def test_import_permissions(self):
        url = reverse("bank:import")
        self.assertEqual(self.client.get(url).status_code, 302)          # anonymous -> login
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(url).status_code, 403)          # no permission

    def test_preview_commit_dupes_and_hierarchy(self):
        self.client.force_login(self.editor)
        url = reverse("bank:import")
        data = {"text": SAMPLE, "subject": self.subject.pk, "semester": 2,
                "deck_title": "Haematology", "skip_dupes": "1"}
        r = self.client.post(url, {**data, "action": "preview"})
        self.assertContains(r, "2</b> parsed")
        self.assertEqual(Question.objects.count(), 0)
        self.client.post(url, {**data, "action": "commit"})
        deck = QuestionBank.objects.get()
        self.assertEqual((deck.subject, deck.semester, deck.title), (self.subject, 2, "Haematology"))
        self.assertEqual(Question.objects.count(), 2)
        self.client.post(url, {**data, "action": "commit"})               # same deck again
        self.assertEqual(Question.objects.count(), 2)                      # duplicates skipped
        self.assertEqual(QuestionBank.objects.count(), 1)

    def test_anatomy_sections_are_ordered_and_show_their_decks(self):
        anatomy, sections = self.anatomy_sections()
        sections.sort(key=lambda section: section.order)
        self.assertEqual(
            [section.name for section in sections],
            ["Embryology", "Histology", "Gross Anatomy"],
        )
        deck = QuestionBank.objects.create(
            subject=anatomy, section=sections[2], semester=2, title="Thorax",
        )
        self.client.force_login(self.editor)
        page = self.client.get(anatomy.get_absolute_url())
        self.assertContains(page, "01")
        self.assertLess(page.content.index(b"Embryology"), page.content.index(b"Histology"))
        self.assertLess(page.content.index(b"Histology"), page.content.index(b"Gross Anatomy"))
        self.assertNotContains(page, "Thorax")
        section_page = self.client.get(sections[2].get_absolute_url())
        self.assertContains(section_page, "Thorax")
        self.assertContains(section_page, "Semester 2")
        self.assertEqual(deck.path, "Anatomy › Gross Anatomy › Semester 2 › Thorax")
        exported = self.client.get(reverse("bank:export", args=[deck.slug]))
        self.assertContains(exported, "SECTION: Gross Anatomy")

    def test_any_subject_can_have_its_own_sections(self):
        section = SubjectSection.objects.create(
            subject=self.subject, name="Neurophysiology", order=0,
        )
        self.client.force_login(self.editor)
        subject_page = self.client.get(self.subject.get_absolute_url())
        self.assertContains(subject_page, "Neurophysiology")
        section_page = self.client.get(section.get_absolute_url())
        self.assertContains(section_page, "No decks in Neurophysiology yet.")

    def test_import_can_assign_deck_to_anatomy_section(self):
        anatomy, sections = self.anatomy_sections()
        section = sections[1]
        self.client.force_login(self.editor)
        response = self.client.post(reverse("bank:import"), {
            "action": "commit", "text": SAMPLE, "subject": anatomy.pk,
            "section": section.pk, "semester": 3, "deck_title": "Epithelia",
            "skip_dupes": "1",
        })
        deck = QuestionBank.objects.get(title="Epithelia")
        self.assertEqual(deck.section, section)
        self.assertEqual(response.status_code, 302)

    def test_import_rejects_section_from_another_subject(self):
        _, sections = self.anatomy_sections()
        section = sections[2]
        self.client.force_login(self.editor)
        response = self.client.post(reverse("bank:import"), {
            "action": "commit", "text": SAMPLE, "subject": self.subject.pk,
            "section": section.pk, "semester": 1, "deck_title": "Invalid category",
        })
        self.assertContains(response, "Choose a sub-subject belonging to the selected subject.")
        self.assertFalse(QuestionBank.objects.filter(title="Invalid category").exists())

    def test_commit_needs_subject_semester_title(self):
        self.client.force_login(self.editor)
        self.client.post(reverse("bank:import"), {"action": "commit", "text": SAMPLE})
        self.assertEqual(QuestionBank.objects.count(), 0)

    def test_home_subject_and_deck_pages(self):
        deck = self.import_sample()
        self.subject.order = 1
        self.subject.save(update_fields=["order"])
        Subject.objects.get_or_create(name="Anatomy", defaults={"order": 0})
        Subject.objects.create(name="Biochemistry", order=2)
        self.client.force_login(self.editor)
        home = self.client.get(reverse("bank:home"))
        self.assertContains(home, "Physiology")
        self.assertContains(home, "سُمُّوا رَوَاسِيَ بِالْجِبَالِ تَيَمُّناً")
        self.assertContains(home, "لَا كَالسَّفِينِ الرَّاسِيَاتِ رُكُودَا")
        self.assertNotContains(home, "Study with Rawasi")
        self.assertLess(home.content.index(b">Anatomy<"), home.content.index(b">Physiology<"))
        self.assertLess(home.content.index(b">Physiology<"), home.content.index(b">Biochemistry<"))
        self.assertContains(home, "Subjects")
        self.assertNotContains(home, "CHAPTER")
        self.assertNotContains(home, "Forecasts for clouds")
        self.assertNotContains(home, "deck ·")
        self.assertNotContains(home, "question ·")
        # The counts are reused from the weekly cache: a new question doesn't show until it expires.
        Question.objects.create(bank=deck, stem="Added later")
        self.assertContains(self.client.get(reverse("bank:home")), 'data-count="2"')
        page = self.client.get(self.subject.get_absolute_url())
        self.assertContains(page, "Semester 1")
        self.assertContains(page, deck.title)

    def test_header_lines_prefill(self):
        self.client.force_login(self.editor)
        text = "SUBJECT: physiology\nSEMESTER: 3\nDECK: Renal\n\n" + SAMPLE
        self.client.post(reverse("bank:import"), {"action": "commit", "text": text, "skip_dupes": "1"})
        deck = QuestionBank.objects.get()
        self.assertEqual((deck.subject, deck.semester, deck.title), (self.subject, 3, "Renal"))

    def test_descriptive_import_and_export(self):
        deck = self.import_sample(title="Neurophysiology")
        self.client.force_login(self.editor)
        self.client.post(reverse("bank:import"), {
            "action": "commit", "text": DESCRIPTIVE_SAMPLE, "subject": self.subject.pk,
            "semester": 1, "deck_title": deck.title, "skip_dupes": "1"})
        question = Question.objects.get(kind=Question.DESC)
        self.assertEqual(question.answer, "The voltage difference across a cell membrane at rest.")
        self.assertEqual(question.label, "U of K")
        self.assertEqual(question.number, "1")
        exported = self.client.get(reverse("bank:export", args=[question.bank.slug])).content.decode()
        self.assertIn("TYPE: DESC", exported)
        self.assertIn("LABEL: U of K", exported)
        self.assertIn("ANSWER: " + question.answer, exported)

    def test_bulk_import_only_routes_to_labs_when_selected_and_keeps_categories_separate(self):
        deck = self.import_sample()
        self.client.force_login(self.editor)
        response = self.client.post(reverse("bank:import"), {
            "action": "commit", "text": SAMPLE, "subject": self.subject.pk,
            "semester": 1, "deck_title": deck.title, "skip_dupes": "1",
            "import_to_labs": "1",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(deck.questions.filter(is_lab=True).count(), 2)

        self.client.post(reverse("bank:import"), {
            "action": "commit", "text": DESCRIPTIVE_SAMPLE, "subject": self.subject.pk,
            "semester": 1, "deck_title": deck.title, "skip_dupes": "1",
            "import_to_labs": "1",
        })
        self.assertTrue(deck.questions.get(kind=Question.DESC).is_lab)

        self.client.force_login(self.student)
        api_url = reverse("bank:api_questions", args=[deck.slug])
        standard_mcq_tf = self.client.get(api_url, {"type": "mcq_tf"}).json()["questions"]
        self.assertEqual(len(standard_mcq_tf), 2)
        self.assertTrue(all(not question["is_lab"] for question in standard_mcq_tf))
        lab_questions = self.client.get(api_url, {"type": "labs"}).json()["questions"]
        self.assertEqual(
            {question["kind"] for question in lab_questions},
            {Question.MCQ, Question.TF, Question.DESC},
        )
        self.assertTrue(all(question["is_lab"] for question in lab_questions))

        self.client.force_login(self.editor)
        page = self.client.get(deck.get_absolute_url())
        self.assertContains(page, "Labs")
        self.assertContains(page, f"?type=labs")
        self.assertContains(page, "Import questions to Labs")
        quiz_page = self.client.get(reverse("bank:quiz", args=[deck.slug]), {"type": "labs"})
        self.assertContains(quiz_page, f"data-question-type=\"labs\"")

    def test_deck_page_shows_separate_quiz_modes_and_counts(self):
        deck = self.import_sample()
        Question.objects.create(bank=deck, kind=Question.DESC, stem="Describe a membrane.", answer="A lipid bilayer.")
        self.client.force_login(self.editor)
        page = self.client.get(deck.get_absolute_url())
        self.assertContains(page, "Choose a question type")
        self.assertContains(page, "MCQs &amp; True / False")
        self.assertContains(page, "value=\"mcq_tf\"")
        self.assertContains(page, '<strong data-counts="{&quot;all&quot;: 2, &quot;uofk&quot;: 0, &quot;other&quot;: 2}">2</strong> questions')
        self.assertContains(page, "Descriptive")
        self.assertContains(page, "value=\"descriptive\"")
        self.assertContains(page, '<strong data-counts="{&quot;all&quot;: 1, &quot;uofk&quot;: 0, &quot;other&quot;: 1}">1</strong> questions')
        self.assertContains(page, "Shuffle question order")

    def test_labs_count_is_separate_from_regular_question_type_counts(self):
        deck = self.import_sample()
        Question.objects.create(
            bank=deck, kind=Question.MCQ, stem="Lab-only MCQ.", is_lab=True,
        )
        self.client.force_login(self.editor)
        page = self.client.get(deck.get_absolute_url())
        self.assertContains(
            page,
            '<strong data-counts="{&quot;all&quot;: 2, &quot;uofk&quot;: 0, &quot;other&quot;: 2}">2</strong> questions',
        )
        self.assertContains(
            page,
            '<strong data-counts="{&quot;all&quot;: 1, &quot;uofk&quot;: 0, &quot;other&quot;: 1}">1</strong> questions',
        )


class QuizApiTests(Base):
    def test_anonymous_users_are_redirected_to_login(self):
        deck = self.import_sample()
        self.client.logout()
        self.assertEqual(self.client.get(reverse("bank:home")).status_code, 302)
        self.assertEqual(self.client.get(reverse("bank:api_questions", args=[deck.slug])).status_code, 302)

    def test_stale_session_cache_does_not_lock_out_current_user(self):
        self.client.force_login(self.editor)
        from django.core.cache import cache
        cache.set(f"bank:active-session:{self.editor.pk}", "stale-session-key", None)
        self.assertEqual(self.client.get(reverse("bank:home")).status_code, 200)

    def test_practice_api_includes_answer_keys_and_checks_remain_available(self):
        deck = self.import_sample()
        self.client.force_login(self.editor)
        data = self.client.get(reverse("bank:api_questions", args=[deck.slug])).json()
        mcq, tf = data["questions"]
        self.assertEqual(mcq["correct_ids"], [mcq["options"][1]["id"]])
        self.assertEqual(tf["answer_key"], {
            str(option["id"]): index == 0 for index, option in enumerate(tf["options"])
        })
        self.assertNotIn("is_correct", json.dumps(data))
        wrong, right = mcq["options"][0]["id"], mcq["options"][1]["id"]
        chk = lambda pk, body: self.post_json(reverse("bank:api_check", args=[pk]), body).json()
        self.assertFalse(chk(mcq["id"], {"selected": wrong})["correct"])
        self.assertTrue(chk(mcq["id"], {"selected": right})["correct"])
        a, b = (o["id"] for o in tf["options"])
        self.assertTrue(chk(tf["id"], {"answers": {str(a): True, str(b): False}})["correct"])
        self.assertFalse(chk(tf["id"], {"answers": {str(a): True, str(b): True}})["correct"])

    def test_descriptive_practice_api_includes_answer_and_filters_by_type(self):
        deck = self.import_sample()
        question = Question.objects.create(
            bank=deck, kind=Question.DESC, stem="Describe resting potential.",
            answer="A voltage difference at rest.", explanation="Ions are distributed unequally.",
            note="Remember the membrane is selectively permeable.",
        )
        self.client.force_login(self.editor)
        descriptive = self.client.get(reverse("bank:api_questions", args=[deck.slug]) + "?type=descriptive").json()
        self.assertEqual([q["id"] for q in descriptive["questions"]], [question.pk])
        self.assertEqual(descriptive["questions"][0]["answer"], question.answer)
        self.assertEqual(descriptive["questions"][0]["explanation"], question.explanation)
        self.assertEqual(descriptive["questions"][0]["note"], question.note)
        lab_question = Question.objects.create(
            bank=deck, kind=Question.DESC, stem="Describe this lab finding.",
            answer="Lab answer.", explanation="Lab explanation.", note="Lab note.", is_lab=True,
        )
        labs = self.client.get(
            reverse("bank:api_questions", args=[deck.slug]), {"type": "labs"},
        ).json()["questions"]
        self.assertEqual(
            (labs[0]["answer"], labs[0]["explanation"], labs[0]["note"]),
            (lab_question.answer, lab_question.explanation, lab_question.note),
        )
        mcq_tf = self.client.get(reverse("bank:api_questions", args=[deck.slug]) + "?type=mcq_tf").json()
        self.assertEqual({q["kind"] for q in mcq_tf["questions"]}, {Question.MCQ, Question.TF})
        check_url = reverse("bank:api_check", args=[question.pk])
        self.assertEqual(self.post_json(check_url, {}).status_code, 400)
        revealed = self.post_json(check_url, {"reveal": True}).json()
        self.assertEqual(revealed["answer"], question.answer)

    def test_practice_api_filters_questions_by_uofk_label(self):
        deck = self.import_sample()
        uofk = Question.objects.create(
            bank=deck, kind=Question.DESC, stem="U of K question.",
            answer="Answer.", label="U of K",
        )
        legacy_uofk = Question.objects.create(
            bank=deck, kind=Question.DESC, stem="Legacy UofK question.",
            answer="Answer.", label="UofK",
        )
        other = Question.objects.create(
            bank=deck, kind=Question.DESC, stem="Other source question.",
            answer="Answer.", label="Other source",
        )
        unlabelled = Question.objects.create(
            bank=deck, kind=Question.DESC, stem="Unlabelled question.",
            answer="Answer.",
        )
        self.client.force_login(self.editor)
        url = reverse("bank:api_questions", args=[deck.slug])
        uofk_ids = {
            question["id"] for question in self.client.get(
                url, {"type": "descriptive", "label": "uofk"},
            ).json()["questions"]
        }
        other_ids = {
            question["id"] for question in self.client.get(
                url, {"type": "descriptive", "label": "other"},
            ).json()["questions"]
        }
        self.assertEqual(uofk_ids, {uofk.pk, legacy_uofk.pk})
        self.assertEqual(other_ids, {other.pk, unlabelled.pk})
        self.assertEqual(
            len(self.client.get(url, {"type": "descriptive", "label": "invalid"}).json()["questions"]),
            4,
        )

class MockExamTests(Base):
    def setUp(self):
        super().setUp()
        self.deck = self.import_sample()
        self.mcq, self.tf = self.deck.questions.order_by("pk")
        self.mcq.answer = "SECRET MCQ ANSWER"
        self.mcq.explanation = "SECRET MCQ EXPLANATION"
        self.mcq.save(update_fields=["answer", "explanation"])
        self.descriptive = Question.objects.create(
            bank=self.deck, kind=Question.DESC, stem="Explain this concept.",
            answer="SECRET DESCRIPTIVE ANSWER", explanation="SECRET DESCRIPTIVE EXPLANATION",
        )
        self.identify = Question.objects.create(
            bank=self.deck, kind=Question.IMAGE, image_answer_mode=Question.IMAGE_IDENTIFY,
            stem="Identify the labelled structure.",
        )
        self.target = ImageTarget.objects.create(
            question=self.identify, label="A", answer="SECRET TARGET ANSWER", order=0,
        )
        self.client.force_login(self.student)

    def create_attempt(self, question_count=4, mix=None, time_limit_minutes=None):
        mix = mix or {"mcq": 25, "tf": 25, "descriptive": 25, "identify": 25}
        template = ExamTemplate.objects.create(
            user=self.student, bank=self.deck, name="Balanced",
            question_count=question_count, mix=mix,
            time_limit_minutes=time_limit_minutes,
        )
        return _start_exam_for_test(template)

    def test_personal_notes_appear_only_after_the_exam_is_submitted(self):
        attempt = self.create_attempt()
        QuestionNote.objects.create(user=self.student, question=self.mcq, text="Watch the distractor")
        api = reverse("bank:api_exam_attempt", args=[attempt.pk])
        self.assertNotContains(self.client.get(api), "Watch the distractor")
        self.post_json(reverse("bank:api_exam_submit", args=[attempt.pk]), {"responses": {}})
        notes = {q["id"]: q["my_note"] for q in self.client.get(api).json()["questions"]}
        self.assertEqual(notes[self.mcq.pk], "Watch the distractor")
        self.assertEqual(notes[self.tf.pk], "")

    def test_submit_saves_all_exam_answers_in_one_request(self):
        attempt = self.create_attempt()
        tf_answers = {
            str(option.pk): option.is_correct for option in self.tf.options.all()
        }
        payload = {
            "responses": {
                str(self.mcq.pk): {"selected": self.mcq.options.get(is_correct=True).pk},
                str(self.tf.pk): {"answers": tf_answers},
                str(self.descriptive.pk): {"text": "My final written response"},
                str(self.identify.pk): {"targets": {str(self.target.pk): "My final target answer"}},
            },
        }
        submitted = self.post_json(reverse("bank:api_exam_submit", args=[attempt.pk]), payload)
        self.assertEqual(submitted.status_code, 200)
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.submitted_at)
        self.assertEqual(
            attempt.responses[str(self.mcq.pk)]["selected"],
            self.mcq.options.get(is_correct=True).pk,
        )
        self.assertEqual(
            attempt.responses[str(self.tf.pk)]["answers"], tf_answers,
        )
        self.assertEqual(
            attempt.responses[str(self.descriptive.pk)]["text"],
            "My final written response",
        )
        self.assertEqual(
            attempt.responses[str(self.identify.pk)]["targets"][str(self.target.pk)],
            "My final target answer",
        )

    def test_invalid_bulk_answer_does_not_submit_or_save_partial_responses(self):
        attempt = self.create_attempt()
        submitted = self.post_json(reverse("bank:api_exam_submit", args=[attempt.pk]), {
            "responses": {
                str(self.descriptive.pk): {"text": "x" * 10001},
                str(self.mcq.pk): {"selected": self.mcq.options.get(is_correct=True).pk},
            },
        })
        self.assertEqual(submitted.status_code, 400)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.submitted_at)
        self.assertEqual(attempt.responses, {})

    def test_answers_are_not_sent_before_submission_and_submission_scores_objective_questions(self):
        attempt = self.create_attempt()
        api_url = reverse("bank:api_exam_attempt", args=[attempt.pk])
        initial = self.client.get(api_url)
        self.assertEqual(initial.status_code, 200)
        initial_data = initial.json()
        self.assertFalse(initial_data["submitted"])
        for item in initial_data["questions"]:
            self.assertNotIn("answer", item)
            self.assertNotIn("explanation", item)
            self.assertNotIn("correct_ids", item)
            self.assertNotIn("answer_key", item)
        self.assertNotIn("SECRET", initial.content.decode())

        mcq_item = next(item for item in initial_data["questions"] if item["id"] == self.mcq.pk)
        tf_item = next(item for item in initial_data["questions"] if item["id"] == self.tf.pk)
        desc_item = next(item for item in initial_data["questions"] if item["id"] == self.descriptive.pk)
        identify_item = next(item for item in initial_data["questions"] if item["id"] == self.identify.pk)
        self.assertEqual(desc_item["kind"], Question.DESC)
        self.assertEqual(identify_item["kind"], Question.IMAGE_IDENTIFY)
        correct_option = self.mcq.options.get(is_correct=True)
        self.assertEqual(
            self.post_json(reverse("bank:api_exam_answer", args=[attempt.pk]), {
                "question_id": self.mcq.pk, "response": {"selected": correct_option.pk},
            }).status_code,
            200,
        )
        tf_answers = {
            str(option.pk): option.is_correct for option in self.tf.options.all()
        }
        self.assertEqual(
            self.post_json(reverse("bank:api_exam_answer", args=[attempt.pk]), {
                "question_id": self.tf.pk, "response": {"answers": tf_answers},
            }).status_code,
            200,
        )
        self.assertEqual(len(mcq_item["options"]), 2)
        self.assertEqual(len(tf_item["options"]), 2)
        self.assertEqual(
            self.post_json(reverse("bank:api_exam_answer", args=[attempt.pk]), {
                "question_id": self.descriptive.pk,
                "response": {"text": "My explanation attempt"},
            }).status_code,
            200,
        )
        self.assertEqual(
            self.post_json(reverse("bank:api_exam_answer", args=[attempt.pk]), {
                "question_id": self.identify.pk,
                "response": {"targets": {str(self.target.pk): "My labelled answer"}},
            }).status_code,
            200,
        )

        submitted = self.post_json(reverse("bank:api_exam_submit", args=[attempt.pk]), {})
        self.assertEqual(submitted.status_code, 200)
        review = submitted.json()
        self.assertTrue(review["submitted"])
        self.assertFalse(review["report_ready"])
        self.assertNotIn("score_percent", review)
        descriptive = next(item for item in review["questions"] if item["id"] == self.descriptive.pk)
        identify = next(item for item in review["questions"] if item["id"] == self.identify.pk)
        self.assertEqual(descriptive["answer"], "SECRET DESCRIPTIVE ANSWER")
        self.assertEqual(descriptive["response"]["text"], "My explanation attempt")
        self.assertEqual(
            identify["response"]["targets"][str(self.target.pk)], "My labelled answer",
        )
        self.assertEqual(identify["targets"][0]["answer"], "SECRET TARGET ANSWER")
        self.assertEqual(
            self.post_json(reverse("bank:api_exam_answer", args=[attempt.pk]), {
                "question_id": self.descriptive.pk, "ratings": {"answer": "known"},
            }).status_code,
            200,
        )
        self.assertEqual(
            self.post_json(reverse("bank:api_exam_answer", args=[attempt.pk]), {
                "question_id": self.identify.pk, "ratings": {str(self.target.pk): "missed"},
            }).status_code,
            200,
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.responses[str(self.descriptive.pk)]["ratings"]["answer"], "known")
        self.assertEqual(
            attempt.responses[str(self.identify.pk)]["ratings"][str(self.target.pk)], "missed",
        )
        final_report = self.client.get(api_url).json()
        self.assertTrue(final_report["report_ready"])
        self.assertEqual(final_report["score_percent"], 75)
        self.assertEqual(final_report["points_earned"], 3)
        self.assertEqual(final_report["points_possible"], 4)
        self.assertEqual([row["id"] for row in final_report["missed"]], [self.identify.pk])
        self.assertEqual(
            self.post_json(reverse("bank:api_exam_answer", args=[attempt.pk]), {
                "question_id": self.mcq.pk, "response": {"selected": correct_option.pk},
            }).status_code,
            400,
        )

    @override_settings(GEMINI_API_KEY="test-key")
    @patch("bank.ai_marking._gemini_generate", return_value="1.C")
    def test_ai_assessment_marks_descriptive_answers_and_leaves_identify_manual(self, generate):
        attempt = self.create_attempt()
        self.post_json(reverse("bank:api_exam_answer", args=[attempt.pk]), {
            "question_id": self.descriptive.pk,
            "response": {"text": "My explanation attempt"},
        })
        self.post_json(reverse("bank:api_exam_submit", args=[attempt.pk]), {})

        assess_url = reverse("bank:api_exam_ai_assess", args=[attempt.pk])
        queued = self.post_json(assess_url, {})
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(queued.json()["assessment_status"], "queued")
        result = self.client.get(assess_url)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["report_ready"])
        self.assertEqual(result.json()["assessment_status"], "completed")
        marked_descriptive = next(
            item for item in result.json()["questions"] if item["id"] == self.descriptive.pk
        )
        self.assertEqual(marked_descriptive["response"]["ratings"]["answer"], "known")
        self.assertEqual(
            [item["id"] for item in result.json()["questions"] if item["kind"] == Question.DESC],
            [self.descriptive.pk],
        )
        self.assertEqual(generate.call_count, 1)
        prompt = generate.call_args.args[0]
        self.assertIn("Suggested answer: SECRET DESCRIPTIVE ANSWER", prompt)
        self.assertIn("Student answer: My explanation attempt", prompt)
        attempt.refresh_from_db()
        self.assertEqual(
            attempt.responses[str(self.descriptive.pk)]["ratings"]["answer"], "known",
        )
        self.assertEqual(
            attempt.responses[str(self.descriptive.pk)]["assessment_source"], "ai",
        )
        self.assertEqual(
            self.post_json(reverse("bank:api_exam_answer", args=[attempt.pk]), {
                "question_id": self.identify.pk,
                "ratings": {str(self.target.pk): "missed"},
            }).status_code,
            200,
        )
        report = self.client.get(reverse("bank:api_exam_attempt", args=[attempt.pk])).json()
        self.assertTrue(report["report_ready"])
        self.assertEqual(
            next(row for row in report["report"] if row["id"] == self.descriptive.pk)["reason"],
            "AI marked correct",
        )

    @override_settings(GEMINI_API_KEY="test-key")
    @patch("bank.ai_marking._gemini_generate")
    def test_ai_assessment_waits_in_queue_when_shared_quota_is_exhausted(self, generate):
        attempt = self.create_attempt()
        self.post_json(reverse("bank:api_exam_answer", args=[attempt.pk]), {
            "question_id": self.descriptive.pk,
            "response": {"text": "My explanation attempt"},
        })
        self.post_json(reverse("bank:api_exam_submit", args=[attempt.pk]), {})
        AIRequestQuota.objects.filter(name="gemini-exam-marking").update(
            request_timestamps=[timezone.now().timestamp()] * 15,
        )
        assess_url = reverse("bank:api_exam_ai_assess", args=[attempt.pk])
        queued = self.post_json(assess_url, {})
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(queued.json()["assessment_status"], "queued")
        result = self.client.get(assess_url).json()
        self.assertEqual(result["assessment_status"], "queued")
        self.assertEqual(result["queue_position"], 1)
        generate.assert_not_called()

    @override_settings(GEMINI_API_KEY="test-key")
    @patch("bank.ai_marking._gemini_generate")
    def test_ai_queue_requests_are_idempotent_and_manual_choice_cancels_pending_job(self, generate):
        attempt = self.create_attempt()
        self.post_json(reverse("bank:api_exam_submit", args=[attempt.pk]), {})
        assess_url = reverse("bank:api_exam_ai_assess", args=[attempt.pk])
        first = self.post_json(assess_url, {})
        second = self.post_json(assess_url, {})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(AIExamAssessment.objects.filter(attempt=attempt).count(), 1)

        cancelled = self.post_json(assess_url, {"action": "cancel"})
        self.assertTrue(cancelled.json()["cancelled"])
        self.assertEqual(
            AIExamAssessment.objects.get(attempt=attempt).status,
            AIExamAssessment.CANCELLED,
        )
        self.assertEqual(
            self.client.get(assess_url).json()["assessment_status"],
            AIExamAssessment.CANCELLED,
        )
        generate.assert_not_called()

    @override_settings(GEMINI_API_KEY="test-key")
    @patch("bank.ai_marking._gemini_generate", return_value="1.C\n2.W")
    def test_invalid_ai_response_does_not_save_any_marks(self, generate):
        attempt = self.create_attempt()
        self.post_json(reverse("bank:api_exam_answer", args=[attempt.pk]), {
            "question_id": self.descriptive.pk,
            "response": {"text": "My explanation attempt"},
        })
        self.post_json(reverse("bank:api_exam_submit", args=[attempt.pk]), {})
        assess_url = reverse("bank:api_exam_ai_assess", args=[attempt.pk])
        self.post_json(assess_url, {})
        result = self.client.get(assess_url).json()
        self.assertEqual(result["assessment_status"], "failed")
        attempt.refresh_from_db()
        self.assertNotIn("ratings", attempt.responses[str(self.descriptive.pk)])

    @override_settings(GEMINI_API_KEY="test-key", GEMINI_MODEL="missing-model")
    @patch("bank.ai_marking.urlopen")
    def test_gemini_404_reports_model_configuration_error(self, urlopen):
        from urllib.error import HTTPError
        from io import BytesIO

        urlopen.side_effect = HTTPError(
            url="https://generativelanguage.googleapis.com/",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=BytesIO(b'{"error":{"message":"model not found"}}'),
        )
        from .ai_marking import AIMarkingUnavailable, _gemini_generate

        with self.assertRaisesRegex(AIMarkingUnavailable, "could not find model 'missing-model'"):
            _gemini_generate("prompt", 1)

    def test_mix_uses_image_question_answer_formats_and_rejects_shortage(self):
        from .views import _exam_question_counts

        image_mcq = Question.objects.create(
            bank=self.deck, kind=Question.IMAGE, image_answer_mode=Question.IMAGE_MCQ,
            stem="Image MCQ.",
        )
        image_tf = Question.objects.create(
            bank=self.deck, kind=Question.IMAGE, image_answer_mode=Question.TF,
            stem="Image true/false.",
        )
        image_desc = Question.objects.create(
            bank=self.deck, kind=Question.IMAGE, image_answer_mode=Question.DESC,
            stem="Image descriptive.",
        )
        Question.objects.create(
            bank=self.deck, kind=Question.IMAGE, image_answer_mode=Question.IMAGE_IDENTIFY,
            stem="Identify question without configured targets.",
        )
        self.assertEqual(_exam_question_counts(self.deck), {
            "mcq": 2, "tf": 2, "descriptive": 2, "identify": 1,
        })
        mix = {"mcq": 100, "tf": 0, "descriptive": 0, "identify": 0}
        attempt = self.create_attempt(question_count=2, mix=mix)
        self.assertEqual(len(attempt.question_ids), 2)
        self.assertTrue(set(attempt.question_ids).issubset({self.mcq.pk, image_mcq.pk}))

        response = self.client.post(reverse("bank:deck_exams", args=[self.deck.slug]), {
            "name": "Too many",
            "question_count": 8,
            "mcq_percent": 100,
            "tf_percent": 0,
            "descriptive_percent": 0,
            "identify_percent": 0,
            "time_limit_minutes": "",
        })
        self.assertContains(response, "Not enough eligible questions")
        self.assertFalse(ExamTemplate.objects.filter(name="Too many").exists())
        self.assertEqual(
            Question.objects.filter(pk__in=[image_tf.pk, image_desc.pk]).count(), 2,
        )

    def test_deck_form_saves_a_reusable_template_and_starts_an_attempt(self):
        response = self.client.get(reverse("bank:deck_exams", args=[self.deck.slug]))
        self.assertContains(response, "Mock exams")
        self.assertContains(response, "Set up a new exam")
        started = self.client.post(reverse("bank:deck_exams", args=[self.deck.slug]), {
            "name": "My timed test",
            "question_count": 4,
            "mcq_percent": 25,
            "tf_percent": 25,
            "descriptive_percent": 25,
            "identify_percent": 25,
            "time_limit_minutes": 10,
            "is_published": "on",
        })
        self.assertEqual(started.status_code, 302)
        template = ExamTemplate.objects.get(user=self.student, bank=self.deck)
        self.assertFalse(template.is_published)
        attempt = template.attempts.get()
        edit_url = reverse("bank:exam_template_edit", args=[template.pk])
        self.assertContains(self.client.get(edit_url), 'value="My timed test"')
        changed = self.client.post(edit_url, {
            "name": "Updated timed test",
            "question_count": 4,
            "mcq_percent": 25,
            "tf_percent": 25,
            "descriptive_percent": 25,
            "identify_percent": 25,
            "time_limit_minutes": 20,
        })
        self.assertEqual(changed.status_code, 302)
        template.refresh_from_db()
        self.assertEqual(template.name, "Updated timed test")
        self.assertEqual(template.time_limit_minutes, 20)
        self.assertEqual(template.attempts.count(), 1)
        self.assertEqual(attempt.configuration["time_limit_minutes"], 10)
        self.assertEqual(attempt.configuration["quotas"], {
            "mcq": 1, "tf": 1, "descriptive": 1, "identify": 1,
        })
        self.assertEqual(attempt.configuration["time_limit_minutes"], 10)
        self.assertEqual(started.url, reverse("bank:exam_attempt", args=[attempt.pk]))
        setups_page = self.client.get(reverse("bank:deck_exams", args=[self.deck.slug]))
        self.assertContains(setups_page, "Recent exam attempts")
        self.assertContains(setups_page, "Resume")

    def test_staff_can_publish_deck_template_for_users(self):
        admin = User.objects.create_superuser("exam-admin", "admin@example.com", "secret")
        self.client.force_login(admin)
        published = self.client.post(reverse("bank:deck_exams", args=[self.deck.slug]), {
            "name": "Official deck exam",
            "question_count": 1,
            "mcq_percent": 100,
            "tf_percent": 0,
            "descriptive_percent": 0,
            "identify_percent": 0,
            "time_limit_minutes": "",
            "is_published": "on",
        })
        self.assertEqual(published.status_code, 302)
        template = ExamTemplate.objects.get(name="Official deck exam")
        self.assertTrue(template.is_published)

        self.client.force_login(self.student)
        page = self.client.get(reverse("bank:deck_exams", args=[self.deck.slug]))
        self.assertContains(page, "Official deck exam")
        self.assertContains(page, "Ready-made exams")
        self.assertContains(page, "My saved exams")
        self.assertLess(page.content.index(b"Ready-made exams"), page.content.index(b"Set up a new exam"))
        self.assertLess(page.content.index(b"Set up a new exam"), page.content.index(b"My saved exams"))
        self.assertLess(page.content.index(b"My saved exams"), page.content.index(b"Recent exam attempts"))
        self.assertEqual(
            self.client.get(reverse("bank:exam_template_edit", args=[template.pk])).status_code,
            404,
        )
        started = self.client.post(reverse("bank:exam_template_start", args=[template.pk]))
        self.assertEqual(started.status_code, 302)
        self.assertTrue(
            ExamAttempt.objects.filter(user=self.student, template=template).exists(),
        )

    def test_staff_published_subject_template_is_shared(self):
        admin = User.objects.create_superuser("subject-exam-admin", "admin@example.com", "secret")
        template = ExamTemplate.objects.create(
            user=admin,
            subject=self.subject,
            name="Official subject exam",
            question_count=1,
            mix={"mcq": 100, "tf": 0, "descriptive": 0, "identify": 0},
            is_published=True,
        )
        ExamTemplateChapter.objects.create(template=template, bank=self.deck, percentage=100)
        self.client.force_login(self.student)
        page = self.client.get(reverse("bank:subject_exams", args=[self.subject.slug]))
        self.assertContains(page, "Official subject exam")
        started = self.client.post(reverse("bank:exam_template_start", args=[template.pk]))
        self.assertEqual(started.status_code, 302)
        self.assertTrue(
            ExamAttempt.objects.filter(user=self.student, template=template).exists(),
        )

    def test_subject_template_uses_chapter_shares_and_groups_questions_by_type(self):
        second_deck = self.import_sample(title="Cardiology", semester=2)
        Question.objects.create(
            bank=second_deck, kind=Question.DESC,
            stem="Explain cardiac output.", answer="A stored description.",
        )
        self.client.force_login(self.student)
        response = self.client.get(reverse("bank:subject_exams", args=[self.subject.slug]))
        self.assertContains(response, 'aria-label="Show chapters of semester"')
        self.assertContains(response, 'data-semester="1"')
        self.assertContains(response, 'data-semester="2"')

        started = self.client.post(reverse("bank:subject_exams", args=[self.subject.slug]), {
            "name": "Intermediate Physiology",
            "question_count": 4,
            "mcq_percent": 50,
            "tf_percent": 0,
            "descriptive_percent": 50,
            "identify_percent": 0,
            "time_limit_minutes": "",
            f"chapter_{self.deck.pk}_selected": "on",
            f"chapter_{self.deck.pk}_percentage": 50,
            f"chapter_{second_deck.pk}_selected": "on",
            f"chapter_{second_deck.pk}_percentage": 50,
        })
        self.assertEqual(started.status_code, 302)
        template = ExamTemplate.objects.get(user=self.student, subject=self.subject)
        self.assertEqual(
            set(template.chapters.values_list("bank_id", "percentage")),
            {(self.deck.pk, 50), (second_deck.pk, 50)},
        )
        attempt = template.attempts.get()
        self.assertEqual(attempt.subject, self.subject)
        self.assertEqual(
            attempt.configuration["chapter_quotas"],
            {str(self.deck.pk): 2, str(second_deck.pk): 2},
        )
        attempt_data = self.client.get(
            reverse("bank:api_exam_attempt", args=[attempt.pk]),
        )
        self.assertEqual(attempt_data.status_code, 200)
        self.assertNotIn("A stored description.", attempt_data.content.decode())
        self.assertNotIn("SECRET MCQ ANSWER", attempt_data.content.decode())
        second_descriptive = second_deck.questions.get(kind=Question.DESC)
        self.assertEqual(
            self.post_json(reverse("bank:api_exam_answer", args=[attempt.pk]), {
                "question_id": second_descriptive.pk,
                "response": {"text": "My answer from another selected chapter."},
            }).status_code,
            200,
        )
        questions = list(Question.objects.filter(pk__in=attempt.question_ids))
        question_by_id = {question.pk: question for question in questions}
        kinds = [
            question_by_id[question_id].kind
            for question_id in attempt.question_ids
        ]
        self.assertEqual(kinds, [Question.MCQ, Question.MCQ, Question.DESC, Question.DESC])
        self.assertEqual(
            {question.bank_id for question in questions},
            {self.deck.pk, second_deck.pk},
        )
        self.assertEqual(started.url, reverse("bank:exam_attempt", args=[attempt.pk]))
        self.assertEqual(
            self.client.get(reverse("bank:exam_attempt", args=[attempt.pk])).context["back_url"],
            reverse("bank:subject_exams", args=[self.subject.slug]),
        )
        edit_url = reverse("bank:exam_template_edit", args=[template.pk])
        edit_page = self.client.get(edit_url)
        self.assertContains(edit_page, 'value="50"')
        self.assertContains(edit_page, f'id="id_chapter_{self.deck.pk}_selected" checked')
        self.assertContains(edit_page, f'id="id_chapter_{second_deck.pk}_selected" checked')
        updated = self.client.post(edit_url, {
            "name": "Updated Physiology",
            "question_count": 4,
            "mcq_percent": 50,
            "tf_percent": 0,
            "descriptive_percent": 50,
            "identify_percent": 0,
            "time_limit_minutes": 15,
            f"chapter_{self.deck.pk}_selected": "on",
            f"chapter_{self.deck.pk}_percentage": 60,
            f"chapter_{second_deck.pk}_selected": "on",
            f"chapter_{second_deck.pk}_percentage": 40,
        })
        self.assertEqual(updated.status_code, 302)
        template.refresh_from_db()
        self.assertEqual(template.name, "Updated Physiology")
        self.assertEqual(template.time_limit_minutes, 15)
        self.assertEqual(
            set(template.chapters.values_list("bank_id", "percentage")),
            {(self.deck.pk, 60), (second_deck.pk, 40)},
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.configuration["chapter_weights"][0]["percentage"], 50)
        self.assertEqual(template.attempts.count(), 1)

    def test_subject_template_rejects_chapter_shares_that_do_not_total_100(self):
        second_deck = self.import_sample(title="Cardiology", semester=2)
        self.client.force_login(self.student)
        response = self.client.post(reverse("bank:subject_exams", args=[self.subject.slug]), {
            "name": "Invalid shares",
            "question_count": 4,
            "mcq_percent": 100,
            "tf_percent": 0,
            "descriptive_percent": 0,
            "identify_percent": 0,
            "time_limit_minutes": "",
            f"chapter_{self.deck.pk}_selected": "on",
            f"chapter_{self.deck.pk}_percentage": 70,
            f"chapter_{second_deck.pk}_selected": "on",
            f"chapter_{second_deck.pk}_percentage": 20,
        })
        self.assertContains(response, "Selected chapter percentages must add up to 100%.")
        self.assertFalse(ExamTemplate.objects.filter(name="Invalid shares").exists())

    def test_saved_templates_and_attempts_are_private_and_timer_expires(self):
        attempt = self.create_attempt(time_limit_minutes=1)
        self.assertIsNotNone(attempt.expires_at)
        other_user = User.objects.create_user("other-student", password="secret")
        self.client.force_login(other_user)
        self.assertEqual(
            self.client.post(
                reverse("bank:exam_template_start", args=[attempt.template_id]),
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("bank:exam_template_delete", args=[attempt.template_id]),
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("bank:exam_template_edit", args=[attempt.template_id]),
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse("bank:api_exam_attempt", args=[attempt.pk])).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse("bank:exam_attempt", args=[attempt.pk])).status_code,
            404,
        )
        self.client.force_login(self.student)
        attempt.expires_at = timezone.now() - timedelta(seconds=1)
        attempt.save(update_fields=["expires_at"])
        expired = self.client.get(reverse("bank:api_exam_attempt", args=[attempt.pk])).json()
        self.assertTrue(expired["submitted"])
        self.assertEqual(
            self.post_json(reverse("bank:api_exam_answer", args=[attempt.pk]), {
                "question_id": self.descriptive.pk, "response": {"text": "Late response"},
            }).status_code,
            409,
        )

    def test_active_exam_resume_context_is_only_available_until_submission(self):
        attempt = self.create_attempt()
        home = self.client.get(reverse("bank:home"))
        self.assertContains(home, 'id="active-exam-resume-context"')
        self.assertContains(home, f'data-id="{attempt.pk}"')
        self.assertContains(home, reverse("bank:exam_attempt", args=[attempt.pk]))
        attempt.submitted_at = timezone.now()
        attempt.save(update_fields=["submitted_at"])
        completed_home = self.client.get(reverse("bank:home"))
        self.assertNotContains(completed_home, 'id="active-exam-resume-context"')

    def test_completed_exam_link_starts_an_independent_practice_attempt(self):
        attempt = self.create_attempt(
            question_count=1,
            mix={"mcq": 100, "tf": 0, "descriptive": 0, "identify": 0},
        )
        correct_option = self.mcq.options.get(is_correct=True)
        attempt.responses = {
            str(self.mcq.pk): {"selected": correct_option.pk},
        }
        attempt.submitted_at = timezone.now()
        attempt.save(update_fields=["responses", "submitted_at"])
        share_url = reverse("bank:api_exam_share", args=[attempt.pk])
        created = self.post_json(share_url, {"action": "create"})
        self.assertEqual(created.status_code, 200)
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.share_token)
        public_url = reverse("bank:exam_share", args=[attempt.share_token])

        self.client.logout()
        anonymous = self.client.get(public_url)
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn(reverse("bank:login"), anonymous.url)

        recipient = User.objects.create_user("exam-recipient", "recipient@example.com", "secret")
        self.client.force_login(recipient)
        started = self.client.get(public_url)
        self.assertEqual(started.status_code, 302)
        practice = ExamAttempt.objects.get(user=recipient, shared_from=attempt)
        self.assertEqual(started.url, reverse("bank:exam_attempt", args=[practice.pk]))
        self.assertEqual(practice.question_ids, attempt.question_ids)
        self.assertEqual(practice.configuration, attempt.configuration)
        self.assertEqual(practice.responses, {})
        self.assertContains(self.client.get(started.url), 'id="exam-runner"')
        api_url = reverse("bank:api_exam_attempt", args=[practice.pk])
        initial = self.client.get(api_url).json()
        self.assertFalse(initial["submitted"])
        self.assertEqual([question["id"] for question in initial["questions"]], attempt.question_ids)
        self.assertNotIn("SECRET MCQ ANSWER", json.dumps(initial))
        self.assertNotIn("SECRET MCQ EXPLANATION", json.dumps(initial))
        self.assertNotIn("correct_ids", initial["questions"][0])

        answered = self.post_json(
            reverse("bank:api_exam_answer", args=[practice.pk]),
            {"question_id": self.mcq.pk, "response": {"selected": correct_option.pk}},
        )
        self.assertEqual(answered.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.responses[str(self.mcq.pk)]["selected"], correct_option.pk)
        self.assertEqual(self.client.get(public_url).url, started.url)

        practice.submitted_at = timezone.now()
        practice.save(update_fields=["submitted_at"])
        practice_page = self.client.get(started.url)
        self.assertEqual(
            practice_page.context["practice_again_url"],
            reverse("bank:exam_share_retry", args=[practice.pk]),
        )
        retry = self.client.post(reverse("bank:exam_share_retry", args=[practice.pk]))
        self.assertEqual(retry.status_code, 302)
        next_practice = attempt.shared_attempts.filter(user=recipient).exclude(
            pk=practice.pk,
        ).order_by("-started_at", "-pk").first()
        self.assertIsNotNone(next_practice)
        self.assertNotEqual(next_practice.pk, practice.pk)
        self.assertEqual(next_practice.responses, {})

        self.client.force_login(self.student)
        revoked = self.post_json(share_url, {"action": "revoke"})
        self.assertEqual(revoked.status_code, 200)
        self.assertIsNone(revoked.json()["share_url"])
        self.client.force_login(recipient)
        self.assertEqual(self.client.get(public_url).status_code, 404)

    def test_unfinished_exam_and_other_users_cannot_create_share_links(self):
        attempt = self.create_attempt()
        share_url = reverse("bank:api_exam_share", args=[attempt.pk])
        unfinished = self.post_json(share_url, {"action": "create"})
        self.assertEqual(unfinished.status_code, 409)
        self.assertIsNone(attempt.share_token)

        other_user = User.objects.create_user("exam-outsider", "outsider@example.com", "secret")
        self.client.force_login(other_user)
        unauthorized = self.post_json(share_url, {"action": "create"})
        self.assertEqual(unauthorized.status_code, 404)

    def test_completed_attempt_can_be_saved_as_unanswered_fixed_template(self):
        attempt = self.create_attempt(
            question_count=1,
            mix={"mcq": 100, "tf": 0, "descriptive": 0, "identify": 0},
        )
        option = self.mcq.options.get(is_correct=True)
        attempt.responses = {str(self.mcq.pk): {"selected": option.pk}}
        attempt.submitted_at = timezone.now()
        attempt.save(update_fields=["responses", "submitted_at"])

        save_url = reverse("bank:api_exam_save_template", args=[attempt.pk])
        saved = self.post_json(save_url, {})
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["saved"])
        self.assertTrue(saved.json()["created"])
        template = ExamTemplate.objects.get(source_attempt=attempt)
        attempt.refresh_from_db()
        self.assertEqual(template.snapshot["question_ids"], attempt.question_ids)
        self.assertEqual(template.snapshot["configuration"], attempt.configuration)
        self.assertEqual(template.question_count, len(attempt.question_ids))
        self.assertFalse(template.is_published)

        saved_again = self.post_json(save_url, {})
        self.assertEqual(saved_again.status_code, 200)
        self.assertFalse(saved_again.json()["created"])
        self.assertEqual(ExamTemplate.objects.filter(source_attempt=attempt).count(), 1)

        started = self.client.post(reverse("bank:exam_template_start", args=[template.pk]))
        self.assertEqual(started.status_code, 302)
        new_attempt = ExamAttempt.objects.get(user=self.student, template=template)
        self.assertEqual(new_attempt.question_ids, attempt.question_ids)
        self.assertEqual(new_attempt.responses, {})
        self.assertContains(self.client.get(reverse("bank:deck_exams", args=[self.deck.slug])), "Saved question set")

    def test_saved_subject_attempt_keeps_its_chapter_selection(self):
        template = ExamTemplate.objects.create(
            user=self.student,
            subject=self.subject,
            name="Subject snapshot source",
            question_count=1,
            mix={"mcq": 100, "tf": 0, "descriptive": 0, "identify": 0},
        )
        ExamTemplateChapter.objects.create(template=template, bank=self.deck, percentage=100)
        from .views import _start_exam
        attempt = _start_exam(
            self.student,
            self.deck,
            template.name,
            template.question_count,
            template.mix,
            template=template,
            chapter_weights=[(self.deck, 100)],
            subject=self.subject,
        )
        attempt.responses = {
            str(attempt.question_ids[0]): {"selected": self.mcq.options.get(is_correct=True).pk},
        }
        attempt.submitted_at = timezone.now()
        attempt.save(update_fields=["responses", "submitted_at"])

        saved = self.post_json(reverse("bank:api_exam_save_template", args=[attempt.pk]), {})
        self.assertEqual(saved.status_code, 200)
        snapshot_template = ExamTemplate.objects.get(source_attempt=attempt)
        self.assertEqual(
            list(snapshot_template.chapters.values_list("bank_id", "percentage")),
            [(self.deck.pk, 100)],
        )
        started = self.client.post(
            reverse("bank:exam_template_start", args=[snapshot_template.pk]),
        )
        self.assertEqual(started.status_code, 302)
        replay = ExamAttempt.objects.get(user=self.student, template=snapshot_template)
        self.assertEqual(replay.question_ids, attempt.question_ids)
        self.assertEqual(replay.responses, {})

    def test_attempt_owner_can_delete_attempt_and_other_users_cannot(self):
        attempt = self.create_attempt()
        delete_url = reverse("bank:exam_attempt_delete", args=[attempt.pk])
        other_user = User.objects.create_user("attempt-delete-outsider", "outsider@example.com", "secret")
        self.client.force_login(other_user)
        self.assertEqual(self.client.post(delete_url).status_code, 404)
        self.assertTrue(ExamAttempt.objects.filter(pk=attempt.pk).exists())

        self.client.force_login(self.student)
        deleted = self.client.post(delete_url)
        self.assertEqual(deleted.status_code, 302)
        self.assertEqual(deleted.url, reverse("bank:deck_exams", args=[self.deck.slug]))
        self.assertFalse(ExamAttempt.objects.filter(pk=attempt.pk).exists())

    def make_section_decks(self):
        """A sub-subject "Gross" with two chapters; only the first has identify questions."""
        section = SubjectSection.objects.create(subject=self.subject, name="Gross")
        self.deck.section = section
        self.deck.save(update_fields=["section"])
        second = self.import_sample(title="Thorax", semester=1)
        second.section = section
        second.save(update_fields=["section"])
        self.client.force_login(self.student)       # import_sample logs the client out
        return section, second

    def test_whole_sub_subject_with_equal_shares_and_automatic_name(self):
        section, second = self.make_section_decks()
        page = self.client.get(reverse("bank:subject_exams", args=[self.subject.slug]) + "?section=gross")
        self.assertContains(page, f'id="id_chapter_{self.deck.pk}_selected" checked')
        self.assertContains(page, f'id="id_chapter_{second.pk}_selected" checked')
        self.assertContains(page, "<b>Gross</b>", html=False)
        # No shares and no name sent: equal shares, and the exam is named after the sub-subject.
        # The second chapter has no identify question, so its place goes to the first one.
        started = self.client.post(reverse("bank:subject_exams", args=[self.subject.slug]), {
            "question_count": 4, "mcq_percent": 25, "tf_percent": 25,
            "descriptive_percent": 25, "identify_percent": 25, "time_limit_minutes": "",
            f"chapter_{self.deck.pk}_selected": "on", f"chapter_{second.pk}_selected": "on",
        })
        self.assertEqual(started.status_code, 302)
        template = ExamTemplate.objects.get(user=self.student, subject=self.subject)
        self.assertEqual(template.name, "Gross · 4 questions")
        self.assertEqual(set(template.chapters.values_list("percentage", flat=True)), {50})
        attempt = template.attempts.get()
        self.assertIn(self.identify.pk, attempt.question_ids)
        self.assertEqual(len(attempt.question_ids), 4)
        # Starting the same setup again gets its own name instead of an error.
        again = self.client.post(reverse("bank:subject_exams", args=[self.subject.slug]), {
            "question_count": 4, "mcq_percent": 25, "tf_percent": 25,
            "descriptive_percent": 25, "identify_percent": 25, "time_limit_minutes": "",
            f"chapter_{self.deck.pk}_selected": "on", f"chapter_{second.pk}_selected": "on",
        })
        self.assertEqual(again.status_code, 302)
        self.assertTrue(ExamTemplate.objects.filter(name="Gross · 4 questions (2)").exists())

    def test_section_page_links_to_its_mock_exam(self):
        section, _ = self.make_section_decks()
        page = self.client.get(section.get_absolute_url())
        self.assertContains(page, reverse("bank:subject_exams", args=[self.subject.slug]) + "?section=gross")

    def test_question_source_limits_exam_questions(self):
        Question.objects.filter(pk__in=[self.mcq.pk, self.tf.pk]).update(label="U of K")
        other_mcq = Question.objects.create(bank=self.deck, kind=Question.MCQ, stem="Other MCQ", label="Gezira")
        Option.objects.create(question=other_mcq, text="A", is_correct=True, order=0)
        page = self.client.get(reverse("bank:deck_exams", args=[self.deck.slug]))
        self.assertContains(page, "U of K only")
        started = self.client.post(reverse("bank:deck_exams", args=[self.deck.slug]), {
            "source": "uofk", "question_count": 2, "mcq_percent": 50, "tf_percent": 50,
            "descriptive_percent": 0, "identify_percent": 0, "time_limit_minutes": "",
        })
        self.assertEqual(started.status_code, 302)
        template = ExamTemplate.objects.get(user=self.student, bank=self.deck)
        self.assertEqual(template.source, "uofk")
        attempt = template.attempts.get()
        self.assertEqual(set(attempt.question_ids), {self.mcq.pk, self.tf.pk})
        self.assertEqual(attempt.configuration["source"], "uofk")
        # Two U of K multiple-choice questions are not there, so it explains instead of starting.
        refused = self.client.post(reverse("bank:deck_exams", args=[self.deck.slug]), {
            "source": "uofk", "question_count": 2, "mcq_percent": 100, "tf_percent": 0,
            "descriptive_percent": 0, "identify_percent": 0, "time_limit_minutes": "",
        })
        self.assertContains(refused, "Not enough eligible questions")
        # Students can't pick another university's questions.
        hidden = self.client.post(reverse("bank:deck_exams", args=[self.deck.slug]), {
            "source": "gezira", "question_count": 1, "mcq_percent": 100, "tf_percent": 0,
            "descriptive_percent": 0, "identify_percent": 0, "time_limit_minutes": "",
        })
        self.assertEqual(hidden.status_code, 200)
        self.assertEqual(ExamTemplate.objects.filter(source="gezira").count(), 0)


def _start_exam_for_test(template):
    from .views import _start_exam
    return _start_exam(
        template.user, template.bank, template.name, template.question_count,
        template.mix, template.time_limit_minutes, template=template,
    )




class SavedTests(Base):
    def test_note_is_private_saves_the_question_and_filters_saved_pages(self):
        deck = self.import_sample()
        noted, plain = deck.questions.order_by("id")[:2]
        url = reverse("bank:api_note", args=[noted.pk])
        self.assertEqual(self.post_json(url, {"text": "x"}).status_code, 401)
        self.client.force_login(self.student)
        self.post_json(reverse("bank:api_save", args=[plain.pk]), {})
        reply = self.post_json(url, {"text": "  Haem = iron + protoporphyrin  "}).json()
        self.assertEqual(reply, {"note": "Haem = iron + protoporphyrin", "saved": True})
        self.assertTrue(SavedQuestion.objects.filter(user=self.student, question=noted).exists())
        self.post_json(url, {"text": "Edited note"})
        self.assertEqual(QuestionNote.objects.get().text, "Edited note")

        api = self.client.get(reverse("bank:api_questions", args=[deck.slug])).json()["questions"]
        self.assertEqual({q["id"]: q["my_note"] for q in api}[noted.pk], "Edited note")
        self.assertEqual({q["id"]: q["my_note"] for q in api}[plain.pk], "")

        saved = self.client.get(reverse("bank:saved_deck", args=[deck.slug]))
        self.assertContains(saved, plain.stem)
        self.assertContains(saved, "Edited note")
        only = self.client.get(reverse("bank:saved_deck", args=[deck.slug]) + "?notes=1")
        self.assertContains(only, noted.stem)
        self.assertNotContains(only, plain.stem)
        self.assertContains(self.client.get(reverse("bank:saved_home") + "?notes=1"), "1 question with notes")
        self.assertContains(self.client.get(reverse("bank:saved_home")), "2 saved questions")
        practice = self.client.get(reverse("bank:api_saved_questions", args=[deck.slug]) + "?notes=1").json()
        self.assertEqual([q["id"] for q in practice["questions"]], [noted.pk])
        quiz = self.client.get(reverse("bank:saved_quiz", args=[deck.slug]) + "?notes=1")
        self.assertContains(quiz, f'data-api="{reverse("bank:api_saved_questions", args=[deck.slug])}?notes=1"')

        other = User.objects.create_user("other", password="pw")
        self.client.force_login(other)
        api = self.client.get(reverse("bank:api_questions", args=[deck.slug])).json()["questions"]
        self.assertEqual({q["id"]: q["my_note"] for q in api}[noted.pk], "")

        self.client.force_login(self.student)
        self.assertEqual(self.post_json(url, {"text": ""}).json(), {"note": "", "saved": True})
        self.assertFalse(QuestionNote.objects.exists())

        self.post_json(url, {"text": "Back again"})
        self.assertFalse(self.post_json(reverse("bank:api_save", args=[noted.pk]), {}).json()["saved"])
        self.assertFalse(QuestionNote.objects.exists())          # un-saving drops the note too
        self.post_json(url, {"text": "Once more"})
        self.client.post(reverse("bank:saved_remove", args=[noted.pk]))
        self.assertFalse(QuestionNote.objects.exists())

    def test_save_toggle_and_saved_section(self):
        deck = self.import_sample()
        q = deck.questions.first()
        url = reverse("bank:api_save", args=[q.pk])
        self.assertEqual(self.post_json(url, {}).status_code, 401)        # anonymous
        self.client.force_login(self.student)
        self.assertTrue(self.post_json(url, {}).json()["saved"])
        self.assertEqual(SavedQuestion.objects.count(), 1)
        self.assertContains(self.client.get(reverse("bank:saved_home")), "1 saved question")
        self.assertContains(self.client.get(reverse("bank:saved_subject", args=[self.subject.slug])), "Semester 1")
        self.assertContains(self.client.get(reverse("bank:saved_deck", args=[deck.slug])), q.stem)
        api = self.client.get(reverse("bank:api_saved_questions", args=[deck.slug])).json()
        self.assertEqual([x["id"] for x in api["questions"]], [q.pk])
        self.assertEqual(api["questions"][0]["correct_ids"], list(
            q.options.filter(is_correct=True).values_list("id", flat=True)
        ))
        practice = self.client.get(reverse("bank:saved_quiz", args=[deck.slug]))
        self.assertContains(practice, 'id="quiz"')
        self.assertContains(practice, f'data-api="{reverse("bank:api_saved_questions", args=[deck.slug])}"')
        self.assertFalse(self.post_json(url, {}).json()["saved"])          # toggles off
        self.assertEqual(SavedQuestion.objects.count(), 0)

    def test_saved_descriptive_practice_uses_quiz_mode_and_includes_feedback(self):
        deck = self.import_sample()
        question = Question.objects.create(
            bank=deck, kind=Question.DESC, stem="Describe resting potential.",
            answer="A voltage difference at rest.", explanation="Ions are distributed unequally.",
            note="Remember selective permeability.",
        )
        self.client.force_login(self.student)
        self.post_json(reverse("bank:api_save", args=[question.pk]), {})

        page = self.client.get(reverse("bank:saved_quiz", args=[deck.slug]))
        self.assertContains(page, 'data-question-type="saved"')
        self.assertContains(page, "?v=20261110-1")

        practice = self.client.get(reverse("bank:api_saved_questions", args=[deck.slug])).json()["questions"]
        self.assertEqual([item["id"] for item in practice], [question.pk])
        self.assertEqual(
            (practice[0]["answer"], practice[0]["explanation"], practice[0]["note"]),
            (question.answer, question.explanation, question.note),
        )

    def test_saved_are_per_user(self):
        deck = self.import_sample()
        q = deck.questions.first()
        self.client.force_login(self.student)
        self.post_json(reverse("bank:api_save", args=[q.pk]), {})
        self.client.force_login(self.editor)
        api = self.client.get(reverse("bank:api_saved_questions", args=[deck.slug])).json()
        self.assertEqual(api["questions"], [])


class CommentAndEditTests(Base):
    def test_editing_from_quiz_returns_to_that_question_without_resetting_other_progress(self):
        deck = self.import_sample()
        question = deck.questions.filter(kind=Question.MCQ).first()
        self.client.force_login(self.editor)
        return_url = reverse("bank:quiz", args=[deck.slug]) + (
            f"?type=mcq_tf&resume_question={question.pk}"
        )
        edit_url = reverse("bank:question_edit", args=[question.pk])

        edit_page = self.client.get(edit_url, {"next": return_url})
        self.assertEqual(edit_page.status_code, 200)
        self.assertEqual(edit_page.context["next"], return_url)

        options = list(question.options.all())
        response = self.client.post(edit_url, {
            "next": return_url,
            "bank": deck.pk,
            "number": question.number,
            "kind": Question.MCQ,
            "stem": "Edited question from the quiz.",
            "answer": "",
            "explanation": question.explanation,
            "note": question.note,
            "options-TOTAL_FORMS": str(len(options) + 2),
            "options-INITIAL_FORMS": str(len(options)),
            "options-MIN_NUM_FORMS": "0",
            "options-MAX_NUM_FORMS": "1000",
            **{
                f"options-{index}-{field}": value
                for index, option in enumerate(options)
                for field, value in (
                    ("id", option.pk),
                    ("question", question.pk),
                    ("text", option.text),
                    ("is_correct", "on" if option.is_correct else ""),
                )
            },
        })

        self.assertRedirects(response, return_url, fetch_redirect_response=False)
        question.refresh_from_db()
        self.assertEqual(question.stem, "Edited question from the quiz.")

    def test_add_descriptive_question_without_options(self):
        deck = self.import_sample()
        self.client.force_login(self.editor)
        response = self.client.post(reverse("bank:question_add", args=[deck.slug]), {
            "bank": deck.pk, "number": "", "kind": Question.DESC,
            "stem": "Describe a membrane.", "answer": "A lipid bilayer.",
            "explanation": "", "note": "",
            "options-TOTAL_FORMS": "4", "options-INITIAL_FORMS": "0",
            "options-MIN_NUM_FORMS": "0", "options-MAX_NUM_FORMS": "1000",
        })
        self.assertEqual(response.status_code, 302)
        question = Question.objects.get(kind=Question.DESC)
        self.assertEqual(question.answer, "A lipid bilayer.")
        self.assertEqual(question.options.count(), 0)

    def test_comment_flow_and_editor_resolves(self):
        deck = self.import_sample()
        q = deck.questions.filter(kind="mcq").first()
        curl = reverse("bank:api_comment", args=[q.pk])
        self.assertEqual(self.post_json(curl, {"text": "typo"}).status_code, 401)
        self.client.force_login(self.student)
        self.assertEqual(self.post_json(curl, {"text": "  "}).status_code, 400)
        self.assertTrue(self.post_json(curl, {"text": "Option B looks wrong"}).json()["ok"])
        self.assertEqual(self.client.get(reverse("bank:comments")).status_code, 403)   # students can't read inbox

        self.client.force_login(self.editor)
        self.assertContains(self.client.get(reverse("bank:comments")), "Option B looks wrong")
        c = Comment.objects.get()
        opts = list(q.options.all())
        payload = {
            "bank": deck.pk, "number": "1", "kind": "mcq", "stem": "Edited stem",
            "explanation": "", "note": "", "resolve": [c.pk],
            "options-TOTAL_FORMS": "3", "options-INITIAL_FORMS": "2",
            "options-MIN_NUM_FORMS": "0", "options-MAX_NUM_FORMS": "1000",
            "options-0-id": opts[0].pk, "options-0-question": q.pk, "options-0-text": "one",
            "options-1-id": opts[1].pk, "options-1-question": q.pk, "options-1-text": "two",
            "options-1-is_correct": "on",
            "options-2-id": "", "options-2-question": q.pk, "options-2-text": "three",
        }
        r = self.client.post(reverse("bank:question_edit", args=[q.pk]), payload)
        self.assertEqual(r.status_code, 302)
        q.refresh_from_db()
        self.assertEqual(q.stem, "Edited stem")
        self.assertEqual([o.text for o in q.options.all()], ["one", "two", "three"])
        c.refresh_from_db()
        self.assertTrue(c.resolved)
        self.assertEqual(c.resolved_by, self.editor)

    def test_edit_requires_permission(self):
        deck = self.import_sample()
        q = deck.questions.first()
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("bank:question_edit", args=[q.pk])).status_code, 403)

    def test_edit_rejects_mcq_without_correct_option(self):
        deck = self.import_sample()
        q = deck.questions.filter(kind="mcq").first()
        opts = list(q.options.all())
        self.client.force_login(self.editor)
        r = self.client.post(reverse("bank:question_edit", args=[q.pk]), {
            "bank": deck.pk, "number": "1", "kind": "mcq", "stem": "x",
            "explanation": "", "note": "",
            "options-TOTAL_FORMS": "2", "options-INITIAL_FORMS": "2",
            "options-MIN_NUM_FORMS": "0", "options-MAX_NUM_FORMS": "1000",
            "options-0-id": opts[0].pk, "options-0-question": q.pk, "options-0-text": "one",
            "options-1-id": opts[1].pk, "options-1-question": q.pk, "options-1-text": "two",
        })
        self.assertEqual(r.status_code, 200)                                # re-rendered with error
        self.assertContains(r, "correct option")

    def test_add_question_and_signup_disabled(self):
        deck = self.import_sample()
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse("bank:question_add", args=[deck.slug])).status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get("/accounts/signup/").status_code, 404)


class ImageQuestionTests(Base):
    def setUp(self):
        super().setUp()
        self.deck = QuestionBank.objects.create(
            subject=self.subject, semester=1, title="Image quiz",
        )

    @staticmethod
    def image_file():
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (16, 12), color="navy").save(output, format="PNG")
        return SimpleUploadedFile("diagram.png", output.getvalue(), content_type="image/png")

    def make_image(self):
        return QuestionImage.objects.create(
            bank=self.deck,
            public_id="supabase:diagram.png",
            secure_url="https://example.supabase.co/storage/v1/object/public/rawasi-question-images/diagram.png",
            original_name="diagram.png",
            width=1600,
            height=1200,
        )

    def make_identification(self, image, stem="Identify the marked structures."):
        question = Question.objects.create(
            bank=self.deck, kind=Question.IMAGE, image_asset=image, stem=stem,
        )
        target = ImageTarget.objects.create(
            question=question, label="A", answer="Test structure",
            x_percent=25, y_percent=60,
        )
        return question, target

    def test_deck_exposes_image_filters_authoring_and_staff_review_links(self):
        image = self.make_image()
        self.make_identification(image)
        mcq = Question.objects.create(
            bank=self.deck, kind=Question.IMAGE, image_asset=image,
            image_answer_mode=Question.IMAGE_MCQ, stem="Choose the structure.",
        )
        Option.objects.create(question=mcq, text="Correct", is_correct=True, order=0)
        Option.objects.create(question=mcq, text="Incorrect", is_correct=False, order=1)
        self.client.force_login(self.editor)
        page = self.client.get(self.deck.get_absolute_url())
        self.assertContains(page, "Identify")
        self.assertContains(page, "2</strong> image questions")
        self.assertContains(page, "?kind=mcq")
        self.assertContains(page, "?kind=desc")
        self.assertContains(page, "?kind=identify")
        self.assertContains(page, "?type=image")
        self.assertContains(page, reverse("bank:image_question_upload", args=[self.deck.slug]))

        reviewer = User.objects.create_user("image-card-reviewer", "test-password", is_staff=True)
        self.client.force_login(reviewer)
        staff_page = self.client.get(self.deck.get_absolute_url())
        self.assertContains(staff_page, "Review Identify")

    def test_editing_identify_question_to_mcq_keeps_it_in_identify(self):
        image = self.make_image()
        question, target = self.make_identification(image)
        self.client.force_login(self.editor)
        editor_page = self.client.get(reverse("bank:question_edit", args=[question.pk]))
        self.assertContains(editor_page, 'name="destination" value="identify"')
        response = self.client.post(
            reverse("bank:question_edit", args=[question.pk]),
            {
                "destination": "identify",
                "answer_mode": Question.MCQ,
                "stem": "Which structure is marked?",
                "targets_json": "[]",
                "options_json": json.dumps([
                    {"text": "Correct structure", "is_correct": True},
                    {"text": "Incorrect structure", "is_correct": False},
                ]),
            },
            follow=True,
        )
        self.assertContains(response, "Image MCQ saved to Identify.")
        question.refresh_from_db()
        self.assertEqual(question.kind, Question.IMAGE)
        self.assertEqual(question.image_answer_mode, Question.IMAGE_MCQ)
        self.assertFalse(question.image_targets.exists())
        self.assertEqual(question.options.count(), 2)
        self.assertFalse(ImageTarget.objects.filter(pk=target.pk).exists())

        reviewer = User.objects.create_user("image-mcq-reviewer", "test-password", is_staff=True)
        self.client.force_login(reviewer)
        review_response = self.post_json(reverse("bank:api_question_edit", args=[question.pk]), {
            "kind": Question.MCQ,
            "image_answer_mode": Question.IMAGE_MCQ,
            "stem": "Which revised structure is marked?",
            "options": [
                {"text": "Revised correct structure", "is_correct": True},
                {"text": "Revised incorrect structure", "is_correct": False},
            ],
        })
        self.assertEqual(review_response.status_code, 200)
        question.refresh_from_db()
        self.assertEqual(question.kind, Question.IMAGE)
        self.assertEqual(question.image_answer_mode, Question.IMAGE_MCQ)
        self.assertEqual(question.stem, "Which revised structure is marked?")

        self.client.force_login(self.student)
        api_url = reverse("bank:api_questions", args=[self.deck.slug])
        identify_questions = self.client.get(api_url, {"type": "image"}).json()["questions"]
        self.assertEqual([item["id"] for item in identify_questions], [question.pk])
        self.assertEqual(identify_questions[0]["kind"], Question.MCQ)
        self.assertEqual(identify_questions[0]["image_answer_mode"], Question.IMAGE_MCQ)
        self.assertEqual(len(identify_questions[0]["correct_ids"]), 1)
        mcq_questions = self.client.get(api_url, {"type": "mcq_tf"}).json()["questions"]
        self.assertEqual(mcq_questions, [])

    def test_image_question_has_dedicated_editor_and_review_edit_support(self):
        image = self.make_image()
        question, target = self.make_identification(image)
        self.client.force_login(self.editor)
        editor = self.client.get(reverse("bank:question_edit", args=[question.pk]))
        self.assertEqual(editor.status_code, 200)
        self.assertContains(editor, reverse("bank:question_delete", args=[question.pk]))
        self.assertContains(editor, "Identification targets")
        self.assertContains(editor, target.answer)
        self.assertContains(editor, "is-calibrated")
        self.assertContains(editor, "--image-aspect-ratio:1600/1200")
        self.assertContains(editor, "--image-max-width:104vh")

        reviewer = User.objects.create_user("image-edit-reviewer", "test-password", is_staff=True)
        self.client.force_login(reviewer)
        response = self.post_json(reverse("bank:api_question_edit", args=[question.pk]), {
            "kind": Question.IMAGE,
            "image_answer_mode": Question.IMAGE_IDENTIFY,
            "stem": "Identify the revised structure.",
            "targets": [{
                "label": "B", "answer": "Revised structure", "x_percent": 55, "y_percent": 70,
            }],
        })
        self.assertEqual(response.status_code, 200)
        question.refresh_from_db()
        self.assertEqual(question.stem, "Identify the revised structure.")
        self.assertEqual(question.image_asset, image)
        self.assertEqual(list(question.image_targets.values_list("label", "answer")), [("B", "Revised structure")])

    def test_image_question_can_be_deleted_from_its_editor(self):
        image = self.make_image()
        question, _ = self.make_identification(image)
        self.client.force_login(self.editor)

        with patch("bank.views.delete_question_image") as delete_image:
            response = self.client.post(reverse("bank:question_delete", args=[question.pk]))

        self.assertRedirects(response, reverse("bank:manage_deck", args=[self.deck.slug]))
        self.assertFalse(Question.objects.filter(pk=question.pk).exists())
        self.assertFalse(QuestionImage.objects.filter(pk=image.pk).exists())
        delete_image.assert_called_once_with(image.public_id)

    def test_upload_and_create_multiple_questions_from_one_image(self):
        self.client.force_login(self.editor)
        upload_url = reverse("bank:image_question_upload", args=[self.deck.slug])
        with patch("bank.views.upload_question_image", return_value={
            "public_id": "supabase:diagram.png",
            "secure_url": "https://example.supabase.co/storage/v1/object/public/rawasi-question-images/diagram.png",
            "width": 1600,
            "height": 1200,
        }) as upload:
            response = self.client.post(upload_url, {"image": self.image_file()})
        self.assertRedirects(
            response,
            reverse("bank:image_question_add", args=[self.deck.slug, QuestionImage.objects.get().pk])
            + "?destination=identify",
        )
        upload.assert_called_once()
        image = QuestionImage.objects.get()
        add_url = reverse("bank:image_question_add", args=[self.deck.slug, image.pk])
        self.assertEqual(self.client.get(add_url).status_code, 200)

        first = self.client.post(add_url, {
            "answer_mode": Question.IMAGE_IDENTIFY,
            "stem": "Identify structure A.",
            "targets_json": json.dumps([{
                "label": "A", "answer": "Structure A", "x_percent": 30, "y_percent": 45,
            }]),
            "options_json": "[]",
        })
        self.assertRedirects(first, self.deck.get_absolute_url())
        first_question = Question.objects.get()
        self.assertEqual(first_question.image_asset, image)
        first_target = first_question.image_targets.get()
        self.assertEqual(first_target.answer, "Structure A")
        self.assertEqual((float(first_target.x_percent), float(first_target.y_percent)), (30, 45))

        mcq_add_url = add_url + "?kind=mcq"
        second = self.client.post(mcq_add_url, {
            "answer_mode": Question.IMAGE_MCQ,
            "stem": "Which structure is shown?",
            "targets_json": "[]",
            "options_json": json.dumps([
                {"text": "Structure A", "is_correct": True},
                {"text": "Structure B", "is_correct": False},
            ]),
            "add_another": "1",
        })
        self.assertRedirects(second, mcq_add_url)
        self.assertEqual(Question.objects.count(), 2)
        mcq = Question.objects.get(stem="Which structure is shown?")
        self.assertEqual(mcq.kind, Question.MCQ)
        self.assertEqual(mcq.image_asset, image)
        self.assertEqual(image.questions.count(), 2)

    def test_answer_image_can_be_added_to_regular_and_identify_descriptive_questions(self):
        self.client.force_login(self.editor)
        upload_result = {
            "public_id": "supabase:answer-diagram.png",
            "secure_url": "https://example.supabase.co/answer-diagram.png",
            "width": 640,
            "height": 480,
        }
        regular_url = reverse("bank:question_add", args=[self.deck.slug])
        with patch("bank.views.upload_question_image", return_value=upload_result):
            regular_response = self.client.post(regular_url, {
                "bank": self.deck.pk, "number": "", "label": "", "kind": Question.DESC,
                "stem": "Describe this structure.", "answer": "", "answer_image": self.image_file(),
                "explanation": "", "note": "",
                "options-TOTAL_FORMS": "4", "options-INITIAL_FORMS": "0",
                "options-MIN_NUM_FORMS": "0", "options-MAX_NUM_FORMS": "1000",
            })
        self.assertEqual(regular_response.status_code, 302)
        regular_question = Question.objects.get(kind=Question.DESC)
        self.assertEqual(regular_question.answer, "")
        self.assertEqual(regular_question.answer_image_asset.secure_url, upload_result["secure_url"])

        prompt_image = self.make_image()
        identify_url = reverse("bank:image_question_add", args=[self.deck.slug, prompt_image.pk])
        with patch("bank.views.upload_question_image", return_value={
            **upload_result, "public_id": "supabase:identify-answer.png",
            "secure_url": "https://example.supabase.co/identify-answer.png",
        }):
            identify_response = self.client.post(
                identify_url + "?kind=desc&destination=identify",
                {
                    "destination": "identify", "answer_mode": Question.DESC,
                    "stem": "Describe the marked image.", "answer": "",
                    "answer_image": self.image_file(), "targets_json": "[]", "options_json": "[]",
                },
            )
        self.assertRedirects(identify_response, self.deck.get_absolute_url())
        identify_question = Question.objects.get(kind=Question.IMAGE)
        self.assertEqual(identify_question.image_answer_mode, Question.DESC)
        self.assertEqual(
            identify_question.answer_image_asset.secure_url,
            "https://example.supabase.co/identify-answer.png",
        )

        self.client.force_login(self.student)
        api_url = reverse("bank:api_questions", args=[self.deck.slug])
        regular_data = self.client.get(api_url, {"type": "descriptive"}).json()["questions"]
        self.assertEqual(regular_data[0]["answer_image_url"], upload_result["secure_url"])
        identify_data = self.client.get(api_url, {"type": "image"}).json()["questions"]
        self.assertEqual(identify_data[0]["kind"], Question.DESC)
        self.assertEqual(identify_data[0]["answer_image_url"], "https://example.supabase.co/identify-answer.png")

    def test_lab_destination_survives_image_upload_and_question_creation(self):
        self.client.force_login(self.editor)
        upload_url = reverse("bank:image_question_upload", args=[self.deck.slug])
        with patch("bank.views.upload_question_image", return_value={
            "public_id": "supabase:lab-diagram.png",
            "secure_url": "https://example.supabase.co/lab-diagram.png",
            "width": 800,
            "height": 600,
        }):
            upload_response = self.client.post(upload_url, {
                "image": self.image_file(), "kind": "identify", "is_lab": "1",
            })
        image = QuestionImage.objects.get()
        add_url = (
            reverse("bank:image_question_add", args=[self.deck.slug, image.pk])
            + "?destination=identify&is_lab=1"
        )
        self.assertRedirects(upload_response, add_url)
        self.assertContains(self.client.get(add_url), 'name="is_lab"')
        self.assertContains(self.client.get(add_url), "checked")

        response = self.client.post(add_url, {
            "is_lab": "on",
            "answer_mode": Question.IMAGE_IDENTIFY,
            "stem": "Identify the lab structure.",
            "targets_json": json.dumps([{"label": "A", "answer": "Lab structure"}]),
            "options_json": "[]",
        })
        self.assertEqual(response.status_code, 302)
        question = Question.objects.get()
        self.assertTrue(question.is_lab)
        self.assertEqual(question.image_asset, image)

    def test_image_questions_are_created_in_their_selected_quiz_categories(self):
        image = self.make_image()
        self.client.force_login(self.editor)
        add_url = reverse("bank:image_question_add", args=[self.deck.slug, image.pk]) + "?kind=mcq"

        response = self.client.post(add_url, {
            "answer_mode": Question.MCQ,
            "stem": "Which structure is shown?",
            "targets_json": "[]",
            "options_json": json.dumps([
                {"text": "Correct", "is_correct": True},
                {"text": "Incorrect", "is_correct": False},
            ]),
        })
        self.assertEqual(response.status_code, 302)
        mcq = Question.objects.get()
        self.assertEqual(mcq.kind, Question.MCQ)
        self.assertEqual(mcq.image_asset, image)

        response = self.client.post(add_url, {
            "answer_mode": Question.TF,
            "stem": "This structure is present.",
            "targets_json": "[]",
            "options_json": json.dumps([
                {"text": "Statement", "is_correct": True},
            ]),
        })
        self.assertEqual(response.status_code, 302)
        image_tf = Question.objects.get(kind=Question.TF, image_asset=image)
        edit_page = self.client.get(reverse("bank:question_edit", args=[image_tf.pk]))
        self.assertContains(edit_page, 'name="destination" value="regular"')
        edited = self.client.post(
            reverse("bank:question_edit", args=[image_tf.pk]),
            {
                "destination": "regular",
                "answer_mode": Question.TF,
                "stem": image_tf.stem,
                "targets_json": "[]",
                "options_json": json.dumps([
                    {"text": option.text, "is_correct": option.is_correct}
                    for option in image_tf.options.all()
                ]),
            },
            follow=True,
        )
        self.assertContains(edited, "Image True / False question saved to MCQs")
        image_tf.refresh_from_db()
        self.assertEqual(image_tf.kind, Question.TF)

        response = self.client.post(add_url, {
            "answer_mode": Question.DESC,
            "stem": "Describe the image.",
            "answer": "The answer is shown here.",
            "targets_json": "[]",
            "options_json": "[]",
        })
        self.assertEqual(response.status_code, 302)
        descriptive = Question.objects.get(kind=Question.DESC)
        self.assertEqual(descriptive.answer, "The answer is shown here.")
        self.assertEqual(descriptive.image_asset, image)

        lab_add_url = f"{add_url}?kind=identify&is_lab=1"
        lab_page = self.client.get(lab_add_url)
        self.assertContains(lab_page, "Import this question to Labs")
        response = self.client.post(lab_add_url, {
            "answer_mode": Question.IMAGE_IDENTIFY,
            "is_lab": "on",
            "stem": "Identify the lab image.",
            "targets_json": json.dumps([{"label": "A", "answer": "Lab target"}]),
            "options_json": "[]",
        })
        self.assertEqual(response.status_code, 302)
        lab_image_question = Question.objects.get(stem="Identify the lab image.")
        self.assertTrue(lab_image_question.is_lab)

        self.client.force_login(self.student)
        api_url = reverse("bank:api_questions", args=[self.deck.slug])
        mcq_tf = self.client.get(api_url, {"type": "mcq_tf"}).json()["questions"]
        self.assertEqual({question["kind"] for question in mcq_tf}, {Question.MCQ, Question.TF})
        self.assertTrue(all(question["image_url"] == image.secure_url for question in mcq_tf))
        descriptive_api = self.client.get(api_url, {"type": "descriptive"}).json()["questions"]
        self.assertEqual([question["id"] for question in descriptive_api], [descriptive.pk])
        self.assertEqual(descriptive_api[0]["image_url"], image.secure_url)
        self.assertEqual(descriptive_api[0]["answer"], "The answer is shown here.")
        lab_api = self.client.get(api_url, {"type": "labs"}).json()["questions"]
        self.assertEqual([question["id"] for question in lab_api], [lab_image_question.pk])

    def test_identify_destination_keeps_image_tf_and_descriptive_questions_in_identify(self):
        image = self.make_image()
        self.client.force_login(self.editor)
        add_url = reverse("bank:image_question_add", args=[self.deck.slug, image.pk])

        tf_response = self.client.post(add_url + "?kind=tf&destination=identify", {
            "destination": "identify",
            "answer_mode": Question.TF,
            "stem": "Assess the statements about this image.",
            "targets_json": "[]",
            "options_json": json.dumps([
                {"text": "The marked structure is present.", "is_correct": True},
                {"text": "The structure is absent.", "is_correct": False},
            ]),
        })
        self.assertEqual(tf_response.status_code, 302)
        image_tf = Question.objects.get(stem="Assess the statements about this image.")
        self.assertEqual(image_tf.kind, Question.IMAGE)
        self.assertEqual(image_tf.image_answer_mode, Question.TF)
        self.assertEqual(image_tf.options.count(), 2)
        identify_edit_page = self.client.get(reverse("bank:question_edit", args=[image_tf.pk]))
        self.assertContains(identify_edit_page, 'name="destination" value="identify"')

        desc_response = self.client.post(add_url + "?kind=desc&destination=identify", {
            "destination": "identify",
            "answer_mode": Question.DESC,
            "stem": "Describe the marked structure.",
            "answer": "It is the example structure.",
            "targets_json": "[]",
            "options_json": "[]",
        })
        self.assertEqual(desc_response.status_code, 302)
        image_desc = Question.objects.get(stem="Describe the marked structure.")
        self.assertEqual(image_desc.kind, Question.IMAGE)
        self.assertEqual(image_desc.image_answer_mode, Question.DESC)
        self.assertEqual(image_desc.answer, "It is the example structure.")

        self.client.force_login(self.student)
        api_url = reverse("bank:api_questions", args=[self.deck.slug])
        identify_questions = self.client.get(api_url, {"type": "image"}).json()["questions"]
        self.assertEqual(
            {item["id"]: item["kind"] for item in identify_questions},
            {image_tf.pk: Question.TF, image_desc.pk: Question.DESC},
        )
        self.assertTrue(all(item["image_url"] == image.secure_url for item in identify_questions))
        self.assertEqual(
            self.client.get(api_url, {"type": "mcq_tf"}).json()["questions"],
            [],
        )
        self.assertEqual(
            self.client.get(api_url, {"type": "descriptive"}).json()["questions"],
            [],
        )
        deck_page = self.client.get(self.deck.get_absolute_url())
        self.assertContains(deck_page, "2</strong> image questions")

        checked = self.post_json(reverse("bank:api_check", args=[image_tf.pk]), {
            "answers": {
                str(image_tf.options.first().pk): True,
                str(image_tf.options.last().pk): False,
            },
        })
        self.assertEqual(checked.status_code, 200)
        self.assertTrue(checked.json()["correct"])
        revealed = self.post_json(reverse("bank:api_check", args=[image_desc.pk]), {"reveal": True})
        self.assertEqual(revealed.json()["answer"], image_desc.answer)

        reviewer = User.objects.create_user("identify-format-reviewer", "test-password", is_staff=True)
        self.client.force_login(reviewer)
        edited = self.post_json(reverse("bank:api_question_edit", args=[image_tf.pk]), {
            "kind": Question.TF,
            "image_answer_mode": Question.TF,
            "stem": "Updated statements about this image.",
            "options": [
                {"text": "Updated true statement.", "is_correct": True},
                {"text": "Updated false statement.", "is_correct": False},
            ],
        })
        self.assertEqual(edited.status_code, 200)
        image_tf.refresh_from_db()
        self.assertEqual(image_tf.kind, Question.IMAGE)
        self.assertEqual(image_tf.image_answer_mode, Question.TF)
        self.assertEqual(image_tf.stem, "Updated statements about this image.")

        edited_desc = self.post_json(reverse("bank:api_question_edit", args=[image_desc.pk]), {
            "kind": Question.DESC,
            "image_answer_mode": Question.DESC,
            "stem": "Updated description prompt.",
            "answer": "Updated descriptive answer.",
        })
        self.assertEqual(edited_desc.status_code, 200)
        image_desc.refresh_from_db()
        self.assertEqual(image_desc.kind, Question.IMAGE)
        self.assertEqual(image_desc.image_answer_mode, Question.DESC)
        self.assertEqual(image_desc.answer, "Updated descriptive answer.")

    def test_debug_upload_saves_locally_and_removes_file_after_last_question(self):
        self.client.force_login(self.editor)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(DEBUG=True, SUPABASE_URL="", SUPABASE_SERVICE_ROLE_KEY="", MEDIA_ROOT=media_root):
                response = self.client.post(
                    reverse("bank:image_question_upload", args=[self.deck.slug]),
                    {"image": self.image_file()},
                )
                image = QuestionImage.objects.get()
                local_file = Path(media_root) / "question-images" / image.public_id[6:]
                self.assertTrue(local_file.is_file())
                self.assertEqual(image.public_id, f"local:{local_file.name}")
                self.assertTrue(image.secure_url.startswith("/media/question-images/"))
                self.assertEqual((image.width, image.height), (16, 12))
                question, _ = self.make_identification(image)

                deleted = self.client.post(reverse("bank:question_delete", args=[question.pk]))
                self.assertEqual(deleted.status_code, 302)
                self.assertFalse(local_file.exists())
                self.assertFalse(QuestionImage.objects.filter(pk=image.pk).exists())

    @override_settings(DEBUG=False, SUPABASE_URL="", SUPABASE_SERVICE_ROLE_KEY="")
    def test_upload_page_reports_missing_supabase_configuration(self):
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse("bank:image_question_upload", args=[self.deck.slug]),
            {"image": self.image_file()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SUPABASE_URL")
        self.assertEqual(QuestionImage.objects.count(), 0)

    @override_settings(
        DEBUG=False,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="server-only-test-key",
        SUPABASE_STORAGE_BUCKET="rawasi-question-images",
    )
    def test_supabase_upload_uses_private_server_key_and_returns_public_url(self):
        with patch("bank.image_storage.urlopen", return_value=io.BytesIO(b"{}")) as open_url:
            result = upload_question_image(self.image_file())

        request = open_url.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertTrue(request.full_url.startswith(
            "https://example.supabase.co/storage/v1/object/rawasi-question-images/"
        ))
        self.assertEqual(request.get_header("Apikey"), "server-only-test-key")
        self.assertEqual(request.get_header("Authorization"), "Bearer server-only-test-key")
        self.assertEqual(request.get_header("Content-type"), "image/png")
        self.assertEqual(request.get_header("X-upsert"), "false")
        self.assertTrue(result["public_id"].startswith("supabase:"))
        self.assertTrue(result["secure_url"].startswith(
            "https://example.supabase.co/storage/v1/object/public/rawasi-question-images/"
        ))
        self.assertEqual((result["width"], result["height"]), (16, 12))

    @override_settings(
        DEBUG=False,
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="server-only-test-key",
        SUPABASE_STORAGE_BUCKET="rawasi-question-images",
    )
    def test_supabase_delete_requests_only_the_referenced_object(self):
        with patch("bank.image_storage.urlopen", return_value=io.BytesIO(b"{}")) as open_url:
            delete_question_image("supabase:diagram.png")

        request = open_url.call_args.args[0]
        self.assertEqual(request.method, "DELETE")
        self.assertEqual(json.loads(request.data), {"prefixes": ["diagram.png"]})
        self.assertEqual(request.get_header("Apikey"), "server-only-test-key")

    def test_practice_routes_identification_and_image_mcqs_to_identify(self):
        image = self.make_image()
        identification, target = self.make_identification(image)
        mcq = Question.objects.create(
            bank=self.deck, kind=Question.IMAGE, image_asset=image,
            image_answer_mode=Question.IMAGE_MCQ, stem="Choose the correct structure.",
        )
        Option.objects.create(question=mcq, text="Correct", is_correct=True, order=0)
        Option.objects.create(question=mcq, text="Incorrect", is_correct=False, order=1)
        self.client.force_login(self.student)
        api_url = reverse("bank:api_questions", args=[self.deck.slug])

        identify_questions = self.client.get(api_url, {"type": "image"}).json()["questions"]
        self.assertEqual(
            [question["id"] for question in identify_questions],
            [identification.pk, mcq.pk],
        )
        practice_identification = identify_questions[0]
        self.assertEqual(practice_identification["targets"][0]["answer"], target.answer)
        self.assertEqual(identify_questions[0]["image_url"], image.secure_url)
        self.assertEqual(
            (identify_questions[0]["image_width"], identify_questions[0]["image_height"]),
            (image.width, image.height),
        )
        mcq_questions = self.client.get(api_url, {"type": "mcq_tf"}).json()["questions"]
        self.assertEqual(mcq_questions, [])
        practice_mcq = identify_questions[1]
        self.assertEqual(practice_mcq["kind"], Question.MCQ)
        self.assertEqual(practice_mcq["correct_ids"], [mcq.options.get(is_correct=True).pk])

        reveal_url = reverse("bank:api_image_target", args=[identification.pk, target.pk])
        revealed = self.post_json(reveal_url, {"action": "reveal"})
        self.assertEqual(revealed.json(), {"answer": target.answer, "target_id": target.pk})
        self.assertEqual(self.post_json(reveal_url, {"action": "grade", "grade": "known"}).status_code, 200)
        self.assertEqual(
            self.post_json(reverse("bank:api_check", args=[identification.pk]), {}).status_code, 400,
        )

        reviewer = User.objects.create_user("image-reviewer", "test-password", is_staff=True)
        self.client.force_login(reviewer)
        review = self.client.get(api_url, {"type": "image", "review": "1"}).json()["questions"]
        review_identification = next(question for question in review if question["id"] == identification.pk)
        self.assertEqual(review_identification["targets"][0]["answer"], target.answer)

        self.client.force_login(self.student)
        self.assertEqual(self.post_json(reveal_url, {"action": "invalid"}).status_code, 400)
        self.assertEqual(
            self.post_json(
                reverse("bank:api_check", args=[mcq.pk]),
                {"selected": mcq.options.get(is_correct=True).pk},
            ).json()["correct"],
            True,
        )

    def test_deck_and_bin_text_exports_exclude_image_questions(self):
        image = self.make_image()
        question, _ = self.make_identification(image, stem="Export must not flatten this image question.")
        question.is_in_review_bin = True
        question.save(update_fields=["is_in_review_bin"])
        self.client.force_login(self.editor)
        deck_export = self.client.get(reverse("bank:export", args=[self.deck.slug])).content.decode()
        self.assertNotIn(question.stem, deck_export)
        self.assertNotIn("TYPE: IMAGE", deck_export)

        reviewer = User.objects.create_user("image-exporter", "test-password", is_staff=True)
        self.client.force_login(reviewer)
        for name in ("sorting_bin_export", "review_bin_export"):
            text = self.client.get(reverse(f"bank:{name}")).content.decode()
            self.assertNotIn(question.stem, text)
            self.assertIn("Image-dependent questions are not included", text)

    def test_text_export_warns_when_attached_images_are_omitted(self):
        image = self.make_image()
        question = Question.objects.create(
            bank=self.deck, kind=Question.DESC, image_asset=image,
            stem="Describe this image.", answer="An example image.",
        )
        self.client.force_login(self.student)
        response = self.client.get(reverse("bank:export", args=[self.deck.slug]))
        self.assertContains(response, "Attached image files are not included in this text export.")
        self.assertContains(response, question.stem)

    def test_shared_image_is_deleted_only_after_last_question(self):
        image = self.make_image()
        first, _ = self.make_identification(image, "First image question.")
        second, _ = self.make_identification(image, "Second image question.")
        self.client.force_login(self.editor)
        with patch("bank.views.delete_question_image") as delete:
            first_response = self.client.post(reverse("bank:question_delete", args=[first.pk]))
            self.assertRedirects(first_response, reverse("bank:manage_deck", args=[self.deck.slug]))
            delete.assert_not_called()
            self.assertTrue(QuestionImage.objects.filter(pk=image.pk).exists())

            second_response = self.client.post(reverse("bank:question_delete", args=[second.pk]))
            self.assertRedirects(second_response, reverse("bank:manage_deck", args=[self.deck.slug]))
            delete.assert_called_once_with(image.public_id)
        self.assertFalse(QuestionImage.objects.filter(pk=image.pk).exists())


IMAGE_IMPORT_SAMPLE = """Q: Which structure is shown?
NO: 17
A. Femoral nerve
B. Femoral artery
ANSWER: B

Q: What is the pathology shown?
NO: 18
TYPE: DESC
ANSWER: Fatty change

Q: What is the function of the labelled organ?
NO: 19
IMAGE: fig_4_12.png
TYPE: DESC
ANSWER: Bile storage
"""


@override_settings(SUPABASE_URL="", SUPABASE_SERVICE_ROLE_KEY="")
class ImportImageTests(Base):
    def setUp(self):
        super().setUp()
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        settings_override = override_settings(DEBUG=True, MEDIA_ROOT=self.media.name)
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        self.client.force_login(self.editor)

    @staticmethod
    def image(name):
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (20, 10), color="teal").save(output, format="PNG")
        return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")

    def commit(self, text=IMAGE_IMPORT_SAMPLE, **files):
        return self.client.post(reverse("bank:import"), {
            "action": "commit", "text": text, "subject": self.subject.pk,
            "semester": 1, "deck_title": "Pathology figures", "skip_dupes": "1", **files,
        })

    def stored_files(self):
        folder = Path(self.media.name) / "question-images"
        return list(folder.iterdir()) if folder.exists() else []

    def test_images_attach_by_number_image_line_and_per_question_pick(self):
        response = self.commit(
            images=[self.image("Q17.png"), self.image("fig_4_12.png")],
            image_1=self.image("IMG_2034.png"),
        )
        self.assertEqual(response.status_code, 302)
        by_answer = {q.answer or q.options.get(is_correct=True).text: q for q in Question.objects.all()}
        self.assertEqual(by_answer["Femoral artery"].image_asset.original_name, "Q17.png")
        self.assertEqual(by_answer["Fatty change"].image_asset.original_name, "IMG_2034.png")
        self.assertEqual(by_answer["Bile storage"].image_asset.original_name, "fig_4_12.png")
        # Image questions keep their answer type, so they appear in the normal MCQ and descriptive tabs.
        self.assertEqual(by_answer["Femoral artery"].kind, Question.MCQ)
        self.assertEqual(by_answer["Fatty change"].kind, Question.DESC)
        self.assertEqual(len(self.stored_files()), 3)

    def test_missing_named_image_imports_nothing(self):
        response = self.commit(images=[self.image("17.png")])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no uploaded image matches")
        self.assertFalse(Question.objects.exists())
        self.assertEqual(self.stored_files(), [])

    def test_image_matching_no_question_imports_nothing(self):
        response = self.commit(images=[self.image("fig_4_12.png"), self.image("99.png")])
        self.assertContains(response, "99.png does not match any question number")
        self.assertFalse(Question.objects.exists())

    def test_failed_upload_removes_stored_images_and_questions(self):
        from .image_storage import ImageStorageError, upload_question_image as real_upload

        calls = []

        def flaky_upload(uploaded):
            calls.append(uploaded.name)
            if len(calls) == 2:
                raise ImageStorageError("storage is down")
            return real_upload(uploaded)

        with patch("bank.views.upload_question_image", side_effect=flaky_upload):
            response = self.commit(images=[self.image("17.png"), self.image("fig_4_12.png")])
        self.assertContains(response, "Nothing was imported: storage is down")
        self.assertFalse(Question.objects.exists())
        self.assertFalse(QuestionImage.objects.exists())
        self.assertEqual(self.stored_files(), [])

    def test_question_with_image_is_not_skipped_as_duplicate(self):
        text = "Q: Identify this tissue.\nTYPE: DESC\nANSWER: Simple squamous epithelium\n"
        self.commit(text=text)
        response = self.commit(text=text, image_0=self.image("slide.png"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Question.objects.count(), 2)
        self.assertEqual(Question.objects.exclude(image_asset=None).count(), 1)

    def test_preview_offers_image_pickers(self):
        response = self.client.post(reverse("bank:import"), {"action": "preview", "text": IMAGE_IMPORT_SAMPLE})
        self.assertContains(response, 'name="image_0"')
        self.assertContains(response, 'data-key="17"')
        self.assertContains(response, "Needs fig_4_12.png")


class ImageProcessingTests(Base):
    @staticmethod
    def chapter_pdf():
        """Page 1: a photo with an arrow and label drawn past its edge, plus a tiny icon. Page 2: blank."""
        import pymupdf
        from PIL import Image
        photo = io.BytesIO()
        Image.new("RGB", (400, 300), "pink").save(photo, "PNG")
        icon = io.BytesIO()
        Image.new("RGB", (40, 40), "blue").save(icon, "PNG")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_image(pymupdf.Rect(100, 100, 300, 250), stream=photo.getvalue())
        page.insert_image(pymupdf.Rect(20, 20, 40, 40), stream=icon.getvalue())
        page.draw_line((250, 180), (330, 180), color=(0, 0, 0), width=2)
        page.insert_text((335, 185), "X", fontsize=14)
        doc.new_page()
        return doc.tobytes()

    def extract(self, **fields):
        upload = SimpleUploadedFile("chapter.pdf", self.chapter_pdf(), content_type="application/pdf")
        return self.client.post(reverse("bank:image_processing"), {"pdf": upload, **fields})

    def test_figures_keep_the_arrows_and_labels_drawn_beside_them(self):
        import zipfile
        from PIL import Image
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(reverse("bank:image_processing")), "Extract figures")
        sizes = {}
        for margin in ("tight", "wide"):
            response = self.extract(margin=margin)
            self.assertEqual(response["Content-Type"], "application/zip")
            self.assertIn('filename="figures_p1-2.zip"', response["Content-Disposition"])
            archive = zipfile.ZipFile(io.BytesIO(response.content))
            self.assertEqual(archive.namelist(), ["p001_fig1.jpg"])  # the icon is skipped
            sizes[margin] = Image.open(archive.open("p001_fig1.jpg")).size
        # The photo is 200pt wide; the arrow and its "X" reach about 245pt from its left edge.
        # Even Tight follows them, which at 200 dpi is well over the photo's own 556px.
        self.assertGreater(sizes["tight"][0], 245 * 200 / 72)
        self.assertGreater(sizes["wide"][0], sizes["tight"][0] + 150)

    def test_body_text_beside_a_figure_is_left_out(self):
        import pymupdf
        from PIL import Image
        from . import figures
        photo = io.BytesIO()
        Image.new("RGB", (300, 300), "gray").save(photo, "PNG")
        doc = pymupdf.open()
        page = doc.new_page(width=600, height=800)
        page.insert_image(pymupdf.Rect(320, 100, 560, 340), stream=photo.getvalue())
        page.insert_text((40, 200), "A long paragraph line of body text in the left column", fontsize=11)
        page.insert_text((300, 352), "label", fontsize=11)  # overlapping the photo's bottom-left corner
        rects = figures.figure_rects(doc[0], figures.MARGINS["tight"])
        self.assertEqual(len(rects), 1)
        self.assertGreater(rects[0].x0, 280)       # the paragraph on the left is not pulled in
        self.assertLess(rects[0].x0, 300)          # the label under the corner is
        self.assertGreater(rects[0].y1, 352)

    def test_whole_pages_and_page_ranges(self):
        import zipfile
        self.client.force_login(self.editor)
        response = self.extract(mode="pages", pages="2")
        self.assertEqual(zipfile.ZipFile(io.BytesIO(response.content)).namelist(), ["p002.jpg"])
        self.assertContains(self.extract(pages="2"), "No pictures were found", status_code=400)
        self.assertContains(self.extract(pages="9"), "only 2 pages", status_code=400)

    def test_rejects_non_pdfs_and_users_who_cannot_import(self):
        self.client.force_login(self.editor)
        fake = SimpleUploadedFile("chapter.pdf", b"hello", content_type="application/pdf")
        self.assertContains(self.client.post(reverse("bank:image_processing"), {"pdf": fake}),
                            "not a PDF", status_code=400)
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("bank:image_processing")).status_code, 403)
        self.assertNotContains(self.client.get(reverse("bank:home")), reverse("bank:image_processing"))


class ResumeActivityTests(Base):
    def test_resume_button_is_tied_to_the_signed_in_account(self):
        login = self.client.get(reverse("bank:login"))
        self.assertNotContains(login, 'id="resume-activity"')
        self.assertNotContains(login, "bank/resume_activity")
        self.client.force_login(self.student)
        home = self.client.get(reverse("bank:home"))
        self.assertContains(home, f'data-user-id="{self.student.pk}"')
        self.assertContains(home, "bank/resume_activity")


class PasswordHashingTests(Base):
    def test_new_passwords_use_argon2_and_old_ones_upgrade_on_login(self):
        self.assertTrue(User.objects.create_user("fresh", password="pw-12345").password.startswith("argon2$argon2id$"))
        from django.contrib.auth.hashers import make_password
        old = User.objects.create_user("legacy")
        old.password = make_password("legacy-pass", hasher="pbkdf2_sha256")
        old.save(update_fields=["password"])
        response = self.client.post(reverse("bank:login"), {"username": "legacy", "password": "legacy-pass"})
        self.assertEqual(response.status_code, 302)
        old.refresh_from_db()
        self.assertTrue(old.password.startswith("argon2$"))
        self.assertTrue(old.check_password("legacy-pass"))


class LoginPageTests(Base):
    def test_login_page_shows_the_arabic_name_and_a_password_eye(self):
        login = self.client.get(reverse("bank:login"))
        self.assertContains(login, '<p class="auth-brand-name" lang="ar" dir="rtl">طب الخرطوم</p>', html=True)
        self.assertContains(login, "password-toggle")
        self.assertContains(login, 'name="password"')


class LibraryTests(Base):
    def make_book(self):
        self.client.force_login(self.editor)
        self.client.post(reverse("bank:library_home"), {"title": "Gray's Review"})
        book = Book.objects.get()
        self.client.post(reverse("bank:book_detail", args=[book.slug]), {"title": "Thorax"})
        self.client.post(reverse("bank:book_detail", args=[book.slug]), {"title": "Abdomen"})
        return book

    def test_books_and_numbered_chapters_are_added_by_editors_only(self):
        book = self.make_book()
        self.assertEqual([(c.number, c.title) for c in book.chapters.all()], [(1, "Thorax"), (2, "Abdomen")])
        self.assertContains(self.client.post(reverse("bank:book_detail", args=[book.slug]),
                                             {"number": "2", "title": "Again"}), "already exists", status_code=400)
        self.assertContains(self.client.post(reverse("bank:library_home"), {"title": "gray's review"}),
                            "already in the Library", status_code=400)
        self.client.force_login(self.student)
        self.assertContains(self.client.get(reverse("bank:library_home")), "Gray&#x27;s Review")
        self.assertNotContains(self.client.get(reverse("bank:library_home")), "Add a book")
        self.assertEqual(self.client.post(reverse("bank:library_home"), {"title": "Mine"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("bank:book_detail", args=[book.slug]),
                                          {"title": "Mine"}).status_code, 403)

    def test_library_questions_live_in_their_book_chapter_not_the_subject_chapter(self):
        book = self.make_book()
        thorax = book.chapters.get(number=1)
        self.client.post(reverse("bank:import"), {
            "action": "commit", "text": SAMPLE + "\n" + DESCRIPTIVE_SAMPLE, "subject": self.subject.pk,
            "semester": 1, "deck_title": "Thorax deck", "skip_dupes": "1", "book_chapter": thorax.pk})
        deck = QuestionBank.objects.get(title="Thorax deck")
        questions = list(deck.questions.order_by("id"))
        self.assertEqual(len(questions), 3)
        self.assertTrue(all(q.book_chapter_id == thorax.pk for q in questions))
        self.assertTrue(all(q.label == "Gray's Review" for q in questions))  # even the one marked U of K

        # Library questions stay out of the subject chapter: its quizzes and counts skip them.
        mcq_tf = self.client.get(reverse("bank:api_questions", args=[deck.slug]) + "?type=mcq_tf").json()
        self.assertEqual(len(mcq_tf["questions"]), 0)
        self.assertContains(self.client.get(deck.get_absolute_url()), "0 questions in this deck")
        # Importing the same questions into the subject chapter itself is not blocked as a duplicate.
        self.client.post(reverse("bank:import"), {
            "action": "commit", "text": SAMPLE, "subject": self.subject.pk,
            "semester": 1, "deck_title": "Thorax deck", "skip_dupes": "1"})
        mcq_tf = self.client.get(reverse("bank:api_questions", args=[deck.slug]) + "?type=mcq_tf").json()
        self.assertEqual(len(mcq_tf["questions"]), 2)
        # ...but importing them into the book chapter again is.
        self.client.post(reverse("bank:import"), {
            "action": "commit", "text": SAMPLE, "subject": self.subject.pk,
            "semester": 1, "deck_title": "Thorax deck", "skip_dupes": "1", "book_chapter": thorax.pk})
        self.assertEqual(thorax.questions.count(), 3)
        # The chapter mixes every type in one quiz.
        self.assertContains(self.client.get(reverse("bank:book_detail", args=[book.slug])), "3 questions")
        quiz = self.client.get(reverse("bank:book_chapter_quiz", args=[book.slug, thorax.pk]))
        self.assertContains(quiz, 'data-question-type="book"')
        api = self.client.get(reverse("bank:api_book_chapter_questions", args=[thorax.pk])).json()
        self.assertEqual(sorted(q["kind"] for q in api["questions"]), ["desc", "mcq", "tf"])
        self.assertEqual({q["label"] for q in api["questions"]}, {"Gray's Review"})

        # Moving a question to the review bin takes it out of the chapter too.
        questions[0].is_in_review_bin = True
        questions[0].save()
        api = self.client.get(reverse("bank:api_book_chapter_questions", args=[thorax.pk])).json()
        self.assertEqual(len(api["questions"]), 2)

    def test_editing_a_question_can_put_it_in_a_book_chapter(self):
        deck = self.import_sample()
        book = self.make_book()
        abdomen = book.chapters.get(number=2)
        q = deck.questions.get(kind="tf")
        opts = list(q.options.all())
        r = self.client.post(reverse("bank:question_edit", args=[q.pk]), {
            "bank": deck.pk, "number": "1", "kind": "tf", "stem": q.stem, "label": "",
            "book_chapter": abdomen.pk, "explanation": "", "note": "",
            "options-TOTAL_FORMS": "2", "options-INITIAL_FORMS": "2",
            "options-MIN_NUM_FORMS": "0", "options-MAX_NUM_FORMS": "1000",
            "options-0-id": opts[0].pk, "options-0-question": q.pk, "options-0-text": "yes",
            "options-0-is_correct": "on",
            "options-1-id": opts[1].pk, "options-1-question": q.pk, "options-1-text": "no",
        })
        self.assertEqual(r.status_code, 302)
        q.refresh_from_db()
        self.assertEqual((q.book_chapter_id, q.label), (abdomen.pk, "Gray's Review"))


class QuestionSourceTests(Base):
    """Admins filter by every university; everyone else only sees U of K and Other."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user("admin", password="pw", is_staff=True)
        self.deck = self.import_sample()
        self.deck.questions.update(label="U of K")
        make = lambda stem, label="": Question.objects.create(
            bank=self.deck, kind=Question.DESC, stem=stem, answer="Answer.", label=label)
        self.red_sea = make("Second source question.", "Red_Sea")
        self.gezira = make("Third source question.", "Gezira")
        self.unlabelled = make("Unlabelled question.")
        self.url = reverse("bank:api_questions", args=[self.deck.slug])

    def ids(self, label, kind="descriptive"):
        return {q["id"] for q in self.client.get(self.url, {"type": kind, "label": label}).json()["questions"]}

    def test_admin_source_menu_lists_universities_present_and_additional(self):
        self.client.force_login(self.admin)
        page = self.client.get(self.deck.get_absolute_url())
        for text in ("All questions (5)", "U of K (2)", "Gezira (1)", "Red Sea (1)", "Additional (1)"):
            self.assertContains(page, text)
        self.assertNotContains(page, "Neelain")  # no Neelain questions in this chapter
        self.assertNotContains(page, "Other questions")

    def test_student_source_menu_keeps_u_of_k_and_other_only(self):
        self.client.force_login(self.student)
        page = self.client.get(self.deck.get_absolute_url())
        self.assertContains(page, "U of K only (2)")
        self.assertContains(page, "Other questions (3)")
        for hidden in ("Gezira", "Red Sea", "Additional"):
            self.assertNotContains(page, hidden)

    def test_admin_filters_by_university_and_additional(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.ids("red_sea"), {self.red_sea.pk})
        self.assertEqual(self.ids("gezira"), {self.gezira.pk})
        self.assertEqual(self.ids("additional"), {self.unlabelled.pk})
        self.assertEqual(self.ids("other"), {self.red_sea.pk, self.gezira.pk, self.unlabelled.pk})

    def test_student_cannot_filter_by_a_university(self):
        self.client.force_login(self.student)
        self.assertEqual(self.ids("red_sea"), self.ids("all"))
        self.assertEqual(self.ids("other"), {self.red_sea.pk, self.gezira.pk, self.unlabelled.pk})

    def test_students_never_receive_other_universities_labels(self):
        self.client.force_login(self.student)
        labels = {q["id"]: q["label"] for q in self.client.get(self.url, {"type": "descriptive"}).json()["questions"]}
        self.assertEqual(labels[self.red_sea.pk], "")
        self.assertEqual(labels[self.gezira.pk], "")
        exported = self.client.get(reverse("bank:export", args=[self.deck.slug])).content.decode()
        self.assertNotIn("Red_Sea", exported)
        self.assertNotIn("Gezira", exported)
        self.assertIn("LABEL: U of K", exported)

    def test_admins_receive_the_stored_label(self):
        self.client.force_login(self.admin)
        labels = {q["id"]: q["label"] for q in self.client.get(self.url, {"type": "descriptive"}).json()["questions"]}
        self.assertEqual(labels[self.red_sea.pk], "Red_Sea")

    def test_library_questions_keep_their_book_label_for_students(self):
        book = Book.objects.create(title="Gray's Review")
        chapter = BookChapter.objects.create(book=book, number=1, title="Thorax")
        question = Question.objects.create(bank=self.deck, kind=Question.DESC, stem="Book question.",
                                           answer="Answer.", label="Gray's Review", book_chapter=chapter)
        self.client.force_login(self.student)
        data = self.client.get(reverse("bank:api_book_chapter_questions", args=[chapter.pk])).json()
        self.assertEqual({q["id"]: q["label"] for q in data["questions"]}[question.pk], "Gray's Review")


class LabelExistingUofKMigrationTests(Base):
    def run_migration(self):
        from importlib import import_module
        from django.apps import apps as django_apps
        import_module("bank.migrations.0024_label_existing_uofk_questions").label_existing_questions(django_apps, None)

    def test_labels_physiology_and_biochemistry_but_not_anatomy_or_universities(self):
        physiology = self.import_sample(title="Cardio")  # self.subject is Physiology
        biochemistry = QuestionBank.objects.create(title="Enzymes", subject=Subject.objects.create(name="Biochemistry"))
        anatomy = QuestionBank.objects.create(title="Thorax", subject=Subject.objects.create(name="Anatomy"))
        make = lambda bank, label="": Question.objects.create(
            bank=bank, kind=Question.DESC, stem=f"{bank.title} {label}", answer="A.", label=label)
        bio_blank, bio_lab, bio_red_sea = make(biochemistry), make(biochemistry, "LAB"), make(biochemistry, "Red_Sea")
        anatomy_blank = make(anatomy)
        self.run_migration()
        self.assertTrue(all(q.label == "U of K" for q in physiology.questions.all()))
        for question in (bio_blank, bio_lab):
            question.refresh_from_db()
            self.assertEqual(question.label, "U of K")
        bio_red_sea.refresh_from_db()
        anatomy_blank.refresh_from_db()
        self.assertEqual(bio_red_sea.label, "Red_Sea")
        self.assertEqual(anatomy_blank.label, "")


class FileDropTests(Base):
    def test_import_page_accepts_dropped_files(self):
        self.client.force_login(self.editor)
        page = self.client.get(reverse("bank:import"))
        self.assertContains(page, 'class="import-text-drop" data-drop-target')
        self.assertContains(page, "data-text-file")
        self.assertContains(page, "bank/drop-files")

    def test_import_preview_rows_accept_dropped_images(self):
        self.client.force_login(self.editor)
        page = self.client.post(reverse("bank:import"), {
            "action": "preview", "text": SAMPLE, "subject": self.subject.pk,
            "semester": 1, "deck_title": "Haematology"})
        self.assertContains(page, "data-drop-target data-ok=")


class ManualLabelTests(Base):
    """Questions added by hand pick their label from a dropdown of existing labels."""

    def setUp(self):
        super().setUp()
        self.deck = QuestionBank.objects.create(subject=self.subject, semester=1, title="Image quiz")

    make_image = ImageQuestionTests.make_image

    def add_form_data(self, **extra):
        return {
            "bank": self.deck.pk, "number": "", "kind": Question.DESC,
            "stem": "Describe a membrane.", "answer": "A lipid bilayer.",
            "explanation": "", "note": "",
            "options-TOTAL_FORMS": "4", "options-INITIAL_FORMS": "0",
            "options-MIN_NUM_FORMS": "0", "options-MAX_NUM_FORMS": "1000", **extra,
        }

    def test_question_form_offers_university_and_existing_labels(self):
        Question.objects.create(bank=self.deck, kind=Question.DESC, stem="x", answer="y", label="Gray's Review")
        self.client.force_login(self.editor)
        page = self.client.get(reverse("bank:question_add", args=[self.deck.slug]))
        self.assertContains(page, '<select name="label"')
        for label in ("U of K", "Gezira", "Neelain", "Nile_Valley", "Red_Sea", "Gray&#x27;s Review"):
            self.assertContains(page, f'value="{label}"')

    def test_manual_question_saves_chosen_label_and_rejects_unknown_ones(self):
        self.client.force_login(self.editor)
        url = reverse("bank:question_add", args=[self.deck.slug])
        response = self.client.post(url, self.add_form_data(label="Nile_Valley"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Question.objects.get().label, "Nile_Valley")
        response = self.client.post(url, self.add_form_data(label="Made up label"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Question.objects.count(), 1)

    def test_image_question_saves_label_and_keeps_it_for_the_next_one(self):
        image = self.make_image()
        self.client.force_login(self.editor)
        add_url = reverse("bank:image_question_add", args=[self.deck.slug, image.pk])
        self.assertContains(self.client.get(add_url), '<select name="label"')
        response = self.client.post(add_url + "?kind=desc", {
            "answer_mode": Question.DESC, "stem": "Name the structure.", "answer": "Liver",
            "label": "Red_Sea", "targets_json": "[]", "options_json": "[]", "add_another": "1",
        })
        self.assertEqual(Question.objects.get().label, "Red_Sea")
        self.assertIn("label=Red_Sea", response["Location"])
        next_page = self.client.get(response["Location"])
        self.assertContains(next_page, '<option value="Red_Sea" selected>')

    def test_editing_image_question_keeps_its_label(self):
        image = self.make_image()
        question = Question.objects.create(
            bank=self.deck, kind=Question.DESC, stem="Name it.", answer="Liver",
            image_asset=image, label="Gezira",
        )
        self.client.force_login(self.editor)
        page = self.client.get(reverse("bank:question_edit", args=[question.pk]))
        self.assertContains(page, '<option value="Gezira" selected>')


class ImportRedirectTests(Base):
    def test_successful_import_returns_to_the_import_page(self):
        self.client.force_login(self.editor)
        response = self.client.post(reverse("bank:import"), {
            "action": "commit", "text": SAMPLE, "subject": self.subject.pk,
            "semester": 1, "deck_title": "Haematology",
        })
        self.assertRedirects(response, reverse("bank:import"))
        self.assertTrue(Question.objects.exists())


class TopicPracticeTests(Base):
    """Students paste an AI's list of question numbers to practise one topic of a chapter."""

    def setUp(self):
        super().setUp()
        self.deck = self.import_sample()
        self.client.force_login(self.student)
        self.ids = list(self.deck.questions.order_by("id").values_list("pk", flat=True))

    def test_chapter_page_links_to_topic_practice(self):
        page = self.client.get(reverse("bank:bank_detail", args=[self.deck.slug]))
        self.assertContains(page, reverse("bank:topic_practice", args=[self.deck.slug]))

    def test_topic_page_lists_every_question_with_its_number(self):
        page = self.client.get(reverse("bank:topic_practice", args=[self.deck.slug]) + "?topic=anaemia")
        for pk in self.ids:
            self.assertContains(page, f"#{pk} | ")
        self.assertContains(page, 'value="anaemia"')
        self.assertContains(page, "data-copy")

    def test_long_chapters_are_split_into_parts(self):
        from bank import views
        with self.settings():
            old, views.TOPIC_PART_CHARS = views.TOPIC_PART_CHARS, 50
            try:
                page = self.client.get(reverse("bank:topic_practice", args=[self.deck.slug]))
            finally:
                views.TOPIC_PART_CHARS = old
        self.assertContains(page, f"Part 1 of {len(self.ids)}")

    def test_messy_ai_reply_finds_questions_and_offers_type_filter(self):
        reply = f"Sure! Matching: #{self.ids[0]}, {self.ids[1]}\nand 999999 and {self.ids[0]}"
        page = self.client.get(reverse("bank:topic_quiz", args=[self.deck.slug]),
                               {"numbers": reply, "topic": "blood"})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "2 questions in")
        self.assertContains(page, "MCQs &amp; True / False")
        self.assertContains(page, "All types together")
        self.assertContains(page, 'class="type-card type-all"')
        self.assertContains(page, f"ids={self.ids[0]}%2C{self.ids[1]}")

    def test_reply_without_matching_numbers_goes_back_with_a_message(self):
        response = self.client.get(reverse("bank:topic_quiz", args=[self.deck.slug]),
                                   {"numbers": "NONE", "topic": "blood"}, follow=True)
        self.assertContains(response, "None of the numbers you pasted")
        self.assertEqual(response.redirect_chain[0][0].split("?")[0],
                         reverse("bank:topic_practice", args=[self.deck.slug]))

    def test_chosen_type_starts_a_quiz_with_only_those_questions(self):
        mcq = self.deck.questions.filter(kind__in=[Question.MCQ, Question.TF], is_lab=False).first()
        other = self.deck.questions.exclude(pk=mcq.pk).first()
        ids = f"{mcq.pk},{other.pk}"
        page = self.client.get(reverse("bank:topic_quiz", args=[self.deck.slug]),
                               {"ids": ids, "type": "mcq_tf", "topic": "blood"})
        self.assertContains(page, 'data-question-type="mcq_tf"')
        api = reverse("bank:api_topic_questions", args=[self.deck.slug])
        data = self.client.get(api, {"ids": ids, "type": "mcq_tf"}).json()
        got = {q["id"] for q in data["questions"]}
        self.assertIn(mcq.pk, got)
        if other.kind not in (Question.MCQ, Question.TF) or other.is_lab:
            self.assertNotIn(other.pk, got)
        everything = self.client.get(api, {"ids": ids, "type": "all"}).json()
        self.assertEqual({q["id"] for q in everything["questions"]}, {mcq.pk, other.pk})

    def test_api_ignores_questions_from_other_chapters(self):
        other_deck = QuestionBank.objects.create(subject=self.subject, semester=2, title="Cardiology")
        foreign = Question.objects.create(bank=other_deck, kind=Question.DESC, stem="x", answer="y")
        data = self.client.get(reverse("bank:api_topic_questions", args=[self.deck.slug]),
                               {"ids": f"{foreign.pk},{self.ids[0]}", "type": "all"}).json()
        self.assertEqual([q["id"] for q in data["questions"]], [self.ids[0]])


@override_settings(GEMINI_API_KEY="test-key", TOPIC_AI_DAILY_LIMIT=150, TOPIC_AI_USER_DAILY_LIMIT=2)
class TopicAISearchTests(Base):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()
        self.deck = self.import_sample()
        self.ids = list(self.deck.questions.order_by("id").values_list("pk", flat=True))
        self.client.force_login(self.student)
        self.url = reverse("bank:topic_ai_search", args=[self.deck.slug])

    def search(self, topic="Anaemia"):
        return self.client.post(self.url, {"topic": topic, "label": "all"})

    def test_page_shows_ai_button_with_searches_left(self):
        page = self.client.get(reverse("bank:topic_practice", args=[self.deck.slug]))
        self.assertContains(page, "Find the questions with AI")
        self.assertContains(page, "<b>2</b> AI searches left for you today")
        self.assertContains(page, "data-ai-wait hidden")      # the waiting view, shown while the AI works

    @override_settings(GEMINI_API_KEY="")
    def test_no_ai_button_without_api_key(self):
        page = self.client.get(reverse("bank:topic_practice", args=[self.deck.slug]))
        self.assertNotContains(page, 'data-topic-ai>')
        self.assertNotContains(page, "AI searches left")

    def test_ai_reply_leads_to_type_choice_and_is_cached_for_everyone(self):
        reply = f"{self.ids[0]}, {self.ids[1]}, 999999"
        with patch("bank.ai_marking._gemini_generate", return_value=reply) as gemini:
            response = self.search()
            self.assertEqual(gemini.call_count, 1)
            prompt = gemini.call_args.args[0]
            self.assertIn('"Anaemia"', prompt)
            self.assertIn(f"#{self.ids[0]} |", prompt)
            self.assertIn(reverse("bank:topic_quiz", args=[self.deck.slug]), response["Location"])
            self.assertIn(f"ids={self.ids[0]}%2C{self.ids[1]}&", response["Location"])
            # Another student, different spacing and case: answered from the cache.
            self.client.force_login(self.editor)
            self.client.post(self.url, {"topic": "  anaemia ", "label": "all"})
            self.assertEqual(gemini.call_count, 1)
        self.assertEqual(TopicSearch.objects.count(), 1)

    def test_cache_is_ignored_after_the_chapter_changes(self):
        with patch("bank.ai_marking._gemini_generate", return_value=str(self.ids[0])) as gemini:
            self.search()
            Question.objects.create(bank=self.deck, kind=Question.DESC, stem="New", answer="x")
            self.search()
        self.assertEqual(gemini.call_count, 2)

    def test_student_daily_limit_falls_back_to_copy_paste(self):
        with patch("bank.ai_marking._gemini_generate", return_value=str(self.ids[0])) as gemini:
            self.search("one")
            self.search("two")
            response = self.client.post(self.url, {"topic": "three", "label": "all"}, follow=True)
        self.assertEqual(gemini.call_count, 2)
        self.assertContains(response, "used your 2 AI searches for today")

    @override_settings(TOPIC_AI_DAILY_LIMIT=1)
    def test_site_daily_limit(self):
        with patch("bank.ai_marking._gemini_generate", return_value=str(self.ids[0])):
            self.search("one")
            response = self.client.post(self.url, {"topic": "two", "label": "all"}, follow=True)
        self.assertContains(response, "The AI is busy for today")

    def test_ai_failure_and_empty_answer_show_messages(self):
        from bank.ai_marking import AIMarkingUnavailable
        with patch("bank.ai_marking._gemini_generate", side_effect=AIMarkingUnavailable("x")):
            response = self.client.post(self.url, {"topic": "one", "label": "all"}, follow=True)
        self.assertContains(response, "couldn&#x27;t answer right now")
        with patch("bank.ai_marking._gemini_generate", return_value="NONE"):
            response = self.client.post(self.url, {"topic": "two", "label": "all"}, follow=True)
        self.assertContains(response, "found no questions about")

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)


@override_settings(GEMINI_API_KEY="test-key", TOPIC_AI_DAILY_LIMIT=150, TOPIC_AI_USER_DAILY_LIMIT=10,
                   GEMINI_MODEL="test-model", TOPIC_AI_MODEL="")
class TopicAIRequestTests(Base):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.deck = self.import_sample()
        self.ids = list(self.deck.questions.order_by("id").values_list("pk", flat=True))
        self.client.force_login(self.student)
        self.url = reverse("bank:topic_ai_search", args=[self.deck.slug])

    def test_big_chapters_are_split_and_the_answers_merged(self):
        replies = iter([str(self.ids[0]), f"{self.ids[1]}"])
        with self.settings(TOPIC_AI_CHUNK_CHARACTERS=10), \
                patch("bank.ai_marking._gemini_generate", side_effect=lambda *a, **k: next(replies)) as gemini:
            response = self.client.post(self.url, {"topic": "blood", "label": "all"})
        self.assertEqual(gemini.call_count, 2)
        prompts = [call.args[0] for call in gemini.call_args_list]
        self.assertTrue(any("(part 1 of 2)" in p for p in prompts))
        self.assertEqual(sorted(TopicSearch.objects.get().question_ids), sorted(self.ids[:2]))
        quota = AIRequestQuota.objects.get(name="gemini-topic-search")
        self.assertEqual(len(quota.request_timestamps), 2)
        self.assertIn("ids=", response["Location"])

    def test_model_that_rejects_minimal_thinking_is_retried_and_remembered(self):
        from bank.ai_marking import AIMarkingUnavailable
        rejected = AIMarkingUnavailable("bad thinking setting")
        rejected.status = 400
        calls = []

        def fake(prompt, items, **kwargs):
            calls.append(kwargs["extra_config"])
            if kwargs["extra_config"] == {"thinkingConfig": {"thinkingLevel": "minimal"}}:
                raise rejected
            return str(self.ids[0])

        with patch("bank.ai_marking._gemini_generate", side_effect=fake):
            self.client.post(self.url, {"topic": "one", "label": "all"})
            self.client.post(self.url, {"topic": "two", "label": "all"})
        self.assertEqual(calls, [
            {"thinkingConfig": {"thinkingLevel": "minimal"}},
            {"thinkingConfig": {"thinkingBudget": 0}},
            {"thinkingConfig": {"thinkingBudget": 0}},      # second search skips the rejected setting
        ])
        self.assertEqual(TopicSearch.objects.count(), 2)

    def test_whole_chapter_goes_in_one_request_by_default(self):
        with patch("bank.ai_marking._gemini_generate", return_value="NONE") as gemini:
            self.client.post(self.url, {"topic": "one", "label": "all"})
        self.assertEqual(gemini.call_count, 1)
        self.assertNotIn("(part 1", gemini.call_args.args[0])

    @override_settings(TOPIC_AI_REQUESTS_PER_MINUTE=1)
    def test_per_minute_limit_protects_the_google_quota(self):
        with patch("bank.ai_marking._gemini_generate", return_value="NONE") as gemini:
            self.client.post(self.url, {"topic": "one", "label": "all"})
            response = self.client.post(self.url, {"topic": "two", "label": "all"}, follow=True)
        self.assertEqual(gemini.call_count, 1)
        self.assertContains(response, "The AI is busy right now")

    @override_settings(TOPIC_AI_MODEL="light-model")
    def test_topic_search_can_use_its_own_model(self):
        with patch("bank.ai_marking._gemini_generate", return_value="NONE") as gemini:
            self.client.post(self.url, {"topic": "one", "label": "all"})
        self.assertEqual(gemini.call_args.kwargs["model"], "light-model")


PUSH_KEYS = {"VAPID_PUBLIC_KEY": "pub", "VAPID_PRIVATE_KEY": "priv", "VAPID_SUBJECT": "mailto:x@example.com",
             "PUSH_BACKGROUND_TICKS": False}
SUBSCRIPTION = {"endpoint": "https://push.example.com/abc", "keys": {"p256dh": "key", "auth": "secret"}}


@override_settings(**PUSH_KEYS)
class PushNotificationTests(Base):
    def setUp(self):
        super().setUp()
        from .models import PushPhrase, PushSettings
        PushSettings.objects.create(pk=1, interval_hours=6)
        PushPhrase.objects.create(text="Study one chapter today")
        PushPhrase.objects.create(text="Ten MCQs before bed")

    def subscribe(self, user=None):
        self.client.force_login(user or self.student)
        return self.client.post(reverse("bank:push_subscribe"), json.dumps(SUBSCRIPTION), content_type="application/json")

    def test_settings_page_offers_notifications_and_subscribing_schedules_within_interval(self):
        from .models import PushPreference
        self.client.force_login(self.student)
        self.assertContains(self.client.get(reverse("bank:settings")), "Allow notifications")
        self.assertEqual(self.subscribe().status_code, 200)
        pref = PushPreference.objects.get(user=self.student)
        self.assertTrue(pref.enabled)
        wait = pref.next_send_at - timezone.now()
        self.assertTrue(timedelta(0) < wait <= timedelta(hours=6))

    def test_due_students_get_a_random_phrase_and_a_new_scattered_time(self):
        from . import push
        from .models import PushPreference
        self.subscribe()
        PushPreference.objects.filter(user=self.student).update(next_send_at=timezone.now() - timedelta(minutes=1))
        with patch("pywebpush.webpush") as send:
            self.assertEqual(push.send_due(), 1)
            self.assertEqual(push.send_due(), 0)          # not due again yet
        body = json.loads(send.call_args.kwargs["data"])["body"]
        self.assertIn(body, {"Study one chapter today", "Ten MCQs before bed"})
        pref = PushPreference.objects.get(user=self.student)
        wait = pref.next_send_at - timezone.now()
        self.assertTrue(timedelta(hours=4.7) < wait < timedelta(hours=7.3))
        # The next phrase is a different one.
        self.assertEqual(push.pick_phrase(pref.last_phrase_id)[1], ({"Study one chapter today", "Ten MCQs before bed"} - {body}).pop())

    def test_gone_devices_are_removed_and_the_student_switched_off(self):
        from pywebpush import WebPushException
        from . import push
        from .models import PushPreference, PushSubscription
        self.subscribe()
        PushPreference.objects.filter(user=self.student).update(next_send_at=timezone.now())
        gone = WebPushException("gone", response=type("R", (), {"status_code": 410})())
        with patch("pywebpush.webpush", side_effect=gone):
            push.send_due()
        self.assertFalse(PushSubscription.objects.exists())
        self.assertFalse(PushPreference.objects.get(user=self.student).enabled)

    def test_unsubscribe_turns_it_off(self):
        from .models import PushPreference
        self.subscribe()
        self.client.post(reverse("bank:push_unsubscribe"), json.dumps({"endpoint": SUBSCRIPTION["endpoint"]}),
                         content_type="application/json")
        self.assertFalse(PushPreference.objects.get(user=self.student).enabled)

    def test_admin_writes_phrases_and_sets_interval_which_respreads_everyone(self):
        from .models import PushPhrase, PushPreference, PushSettings
        self.subscribe()
        self.assertEqual(self.client.get(reverse("bank:push_admin")).status_code, 302)   # students can't
        admin = User.objects.create_superuser("push-admin", "a@example.com", "pw")
        self.client.force_login(admin)
        self.assertContains(self.client.get(reverse("bank:push_admin")), "What they say")
        self.client.post(reverse("bank:push_admin"), {"action": "add", "phrases": "One\n\nTwo  "})
        self.assertTrue(PushPhrase.objects.filter(text="Two").exists())
        self.client.post(reverse("bank:push_admin"), {"action": "settings", "interval_hours": "2", "enabled": "on", "title": "Rawasi"})
        self.assertEqual(float(PushSettings.load().interval_hours), 2)
        wait = PushPreference.objects.get(user=self.student).next_send_at - timezone.now()
        self.assertTrue(wait <= timedelta(hours=2))

    @override_settings(PUSH_TICK_KEY="s3cret")
    def test_tick_url_needs_the_secret(self):
        self.client.logout()
        self.assertEqual(self.client.get("/push/tick/wrong/").status_code, 404)
        with patch("bank.push.send_due", return_value=0):
            self.assertEqual(self.client.get("/push/tick/s3cret/").json(), {"sent": 0})
