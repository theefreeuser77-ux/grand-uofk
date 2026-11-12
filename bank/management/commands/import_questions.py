from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from bank import merging, parser, services
from bank.models import Subject, SubjectSection


class Command(BaseCommand):
    help = ("Import questions from text files (see README). Each file goes into one deck: "
            "pass --subject/--semester/--deck, or put SUBJECT:/SEMESTER:/DECK: lines at the top of the file.")

    def add_arguments(self, ap):
        ap.add_argument("files", nargs="+", help="One or more .txt files")
        ap.add_argument("--subject", help="Subject name (must already exist)")
        ap.add_argument("--section", help="Sub-subject name (must belong to the selected subject)")
        ap.add_argument("--semester", type=int, help="Semester number, e.g. 1")
        ap.add_argument("--deck", help="Deck title (only with a single file). Default: DECK: line, else file name.")
        ap.add_argument("--allow-duplicates", action="store_true")
        ap.add_argument("--dry-run", action="store_true", help="Parse and report only")

    def handle(self, *args, **o):
        if o["deck"] and len(o["files"]) > 1:
            raise CommandError("--deck can only be used with one file.")
        for f in o["files"]:
            path = Path(f)
            res = parser.parse(path.read_text(encoding="utf-8-sig"))
            title = o["deck"] or res.bank_title or path.stem
            semester = o["semester"] or res.semester
            subject_name = o["subject"] or res.subject
            if not subject_name or not semester:
                raise CommandError(f"{path.name}: need a subject and semester "
                                   "(--subject/--semester or SUBJECT:/SEMESTER: lines).")
            subject = Subject.objects.filter(name__iexact=subject_name).first()
            if not subject:
                names = ", ".join(Subject.objects.values_list("name", flat=True)) or "none yet"
                raise CommandError(f"Unknown subject “{subject_name}”. Existing subjects: {names}. "
                                   "Create it in /admin/ or run seed_subjects.")
            section_name = o["section"] or res.section
            section = None
            if section_name:
                section = SubjectSection.objects.filter(
                    subject=subject, name__iexact=section_name,
                ).first()
                if not section:
                    raise CommandError(
                        f"{path.name}: unknown sub-subject “{section_name}” under {subject.name}."
                    )
            deck = services.find_deck(subject, semester, title)
            if deck and section and deck.section_id not in (None, section.pk):
                raise CommandError(f"{path.name}: deck already belongs to another sub-subject.")
            if deck:
                parser.mark_duplicates(res.questions, services.existing_keys(deck))
                merging.mark_existing(res.questions, deck)
            errs = [q for q in res.questions if q.errors]
            for q in errs:
                self.stderr.write(f"{path.name} line {q.line} (Q{q.number or '?'}): " + " ".join(q.errors))
            if o["dry_run"]:
                self.stdout.write(f"{path.name}: {len(res.questions)} parsed, {len(errs)} with errors (dry run)")
                continue
            deck = services.get_or_create_deck(subject, semester, title, section)
            report = {}
            c, b, d = services.save_questions(deck, res.questions, not o["allow_duplicates"], report=report)
            self.stdout.write(self.style.SUCCESS(
                f"{path.name} -> {deck.path}: {c} created, {b} invalid, {d} duplicates"
                f" ({report.get('labelled', 0)} existing question(s) got the new label)"))
