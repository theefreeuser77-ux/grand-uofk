from django.core.management.base import BaseCommand
from django.db import transaction

from bank import merging
from bank.models import QuestionBank


class Command(BaseCommand):
    help = ("Find questions that appear more than once in the same chapter and merge each set into one "
            "question carrying every university's label. Without --apply it only lists what it would do.")

    def add_arguments(self, ap):
        ap.add_argument("--apply", action="store_true", help="Merge for real (default: list only).")
        ap.add_argument("--subject", help="Only chapters of this subject (name).")
        ap.add_argument("--skip", default="", help="Question ids to leave alone, comma separated.")
        ap.add_argument("--loose", action="store_true",
                        help="Also list looser matches (85%%). They are never merged here; tick them on the Duplicates page.")

    def handle(self, *args, **o):
        skip = {int(x) for x in o["skip"].replace(" ", "").split(",") if x.isdigit()}
        banks = QuestionBank.objects.select_related("subject").order_by("subject__name", "semester", "title")
        if o["subject"]:
            banks = banks.filter(subject__name__iexact=o["subject"])
        found = []
        for bank in banks:
            similar = merging.SIMILAR_LOOSE if o["loose"] else merging.SIMILAR
            groups, conflicts = merging.find_groups(bank, skip, similar)
            if groups or conflicts:
                found.append((bank, groups, conflicts))

        n_groups = sum(1 for _, g, _ in found for _, sure in g if sure)
        n_removed = sum(len(group) - 1 for _, g, _ in found for group, sure in g if sure)
        for bank, groups, conflicts in found:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{bank.path}"))
            for group, sure in groups:
                self.stdout.write(("  Merge into #%d:" if sure else "  Looser match, check by hand (#%d):") % group[0].pk)
                for q in group:
                    self.stdout.write(f"    #{q.pk} [{q.label or 'no label'}] {q.stem[:110]}")
            for a, b in conflicts:
                self.stdout.write(self.style.WARNING(
                    f"  Same text, different answer (not merged): #{a.pk} [{a.label}] and #{b.pk} [{b.label}] "
                    f"- {a.stem[:80]}"))
        self.stdout.write(f"\n{n_groups} set(s) of copies; {n_removed} question(s) would be removed.")
        if not o["apply"]:
            self.stdout.write("Dry run: nothing changed. Leave a set alone with --skip <ids>, then run with --apply.")
            return
        with transaction.atomic():
            moved = {}
            for _, groups, _ in found:
                for group, sure in groups:
                    if sure:
                        moved.update(merging.merge_group(group))
            merging.finish(moved, [bank for bank, groups, _ in found if groups])
        self.stdout.write(self.style.SUCCESS(f"Merged {n_groups} set(s); removed {len(moved)} copies."))
