"""Label the existing Physiology and Biochemistry questions as U of K.

They were all imported from U of K material before questions carried university labels.
Labelling them once (instead of treating "no label" as U of K) keeps later unlabelled
questions out of the U of K count. A few Biochemistry questions carried leftover topic
labels from that same material; they become U of K too.
"""
from django.db import migrations
from django.db.models import Q

SUBJECTS = ["Physiology", "Biochemistry"]
LEFTOVER_BIOCHEMISTRY_LABELS = ["LAB", "Practical question (pH meter)", "Jaundice"]


def label_existing_questions(apps, schema_editor):
    Question = apps.get_model("bank", "Question")
    in_subjects = Q()
    for name in SUBJECTS:
        in_subjects |= Q(bank__subject__name__iexact=name)
    leftover = Q()
    for label in LEFTOVER_BIOCHEMISTRY_LABELS:
        leftover |= Q(label__iexact=label)
    targets = Question.objects.filter(book_chapter__isnull=True).filter(
        (in_subjects & Q(label=""))
        | (Q(bank__subject__name__iexact="Biochemistry") & leftover)
    )
    Question.objects.filter(pk__in=list(targets.values_list("pk", flat=True))).update(label="U of K")


class Migration(migrations.Migration):
    dependencies = [("bank", "0023_backup_snapshots")]

    # Not reversible on purpose: the earlier labels were empty or leftovers, and restore points
    # from the Backup page can bring back the previous state if ever needed.
    operations = [migrations.RunPython(label_existing_questions, migrations.RunPython.noop)]
