import re
from pathlib import PurePath

from django.db import transaction

from .models import Option, Question, QuestionBank
from .parser import question_key


def existing_keys(bank, is_lab=False):
    """{fingerprint: label} for questions already stored in this destination."""
    keys = {}
    for q in bank.questions.filter(
        is_in_sorting_bin=False, is_in_review_bin=False, is_lab=is_lab,
    ).prefetch_related("options"):
        keys[question_key(q.kind, q.stem, [o.text for o in q.options.all()], answer=q.answer)] = q.number or f"#{q.pk}"
    return keys


def image_key(value):
    """Matching key for an image file name, IMAGE: line, or question NO.

    "17.jpg", "Q17.png", "017", and "17" all become "17"; other names keep their
    lower-case stem ("Fig_4_12.PNG" -> "fig_4_12"). quiz import JS mirrors this.
    """
    stem = PurePath(value.strip()).stem if "." in value else value
    stem = stem.strip().lower()
    match = re.fullmatch(r"q?\s*[-_.]?\s*0*(\d+)", stem)
    return match.group(1) if match else stem


def match_import_images(parsed, files, picked=None):
    """Pair uploaded images with importable questions.

    ``files`` are bulk uploads matched by IMAGE: line, otherwise by NO; ``picked``
    maps a question index to a file chosen for that question, which wins.
    Returns ({index: file}, problems) where problems are messages that must be
    fixed before importing.
    """
    picked = picked or {}
    by_key, problems = {}, []
    for upload in files:
        key = image_key(upload.name)
        if key in by_key:
            problems.append(f"Two images match “{key}”: {by_key[key].name} and {upload.name}.")
        by_key.setdefault(key, upload)
    assigned, used = {}, set()
    for index, pq in enumerate(parsed):
        key = image_key(pq.image) if pq.image else image_key(pq.number) if pq.number else ""
        if not pq.ok:
            used.add(key)  # its image belongs to a question that is skipped, not to a wrong name
            continue
        if index in picked:
            assigned[index] = picked[index]
            used.add(key)
        elif key in by_key:
            assigned[index] = by_key[key]
            used.add(key)
        elif pq.image:
            problems.append(f"Line {pq.line}: no uploaded image matches “IMAGE: {pq.image}”.")
    problems += [f"Image {by_key[key].name} does not match any question number."
                 for key in by_key if key not in used]
    return assigned, problems


def find_deck(subject, semester, title):
    return QuestionBank.objects.filter(
        subject=subject, semester=semester, title__iexact=title.strip()).first()


def get_or_create_deck(subject, semester, title, section=None):
    deck = find_deck(subject, semester, title)
    if deck:
        if section and deck.section_id not in (None, section.pk):
            raise ValueError("This deck already belongs to a different sub-subject.")
        if section and deck.section_id is None:
            deck.section = section
            deck.full_clean()
            deck.save(update_fields=["section"])
        return deck
    deck = QuestionBank(subject=subject, section=section, semester=semester, title=title.strip())
    deck.full_clean()
    deck.save()
    return deck


def _number_group(kind):
    if kind == Question.IMAGE:
        return Question.IMAGE
    return Question.DESC if kind == Question.DESC else "mcq_tf"


@transaction.atomic
def save_questions(bank, parsed, skip_duplicates=True, is_lab=False, images=None):
    """Persist questions with sequential MCQ/TF and descriptive numbering.

    Source NO/number labels are deliberately ignored. Existing questions are
    normalized within their quiz type, then imports continue that type's sequence.
    ``images`` maps an index in ``parsed`` to the QuestionImage attached to it;
    those questions are never skipped as duplicates, since their text alone
    (e.g. "Identify this tissue") does not identify them.
    """
    images = images or {}
    existing = list(bank.questions.order_by("id"))
    next_numbers = {}
    for q in existing:
        group = _number_group(q.kind)
        key = (q.is_lab, group)
        next_numbers[key] = next_numbers.get(key, 0) + 1
        label = str(next_numbers[key])
        if q.number != label:
            q.number = label
            q.save(update_fields=["number"])

    created = bad = dup = 0
    for index, pq in enumerate(parsed):
        if not pq.ok:
            bad += 1
            continue
        if pq.duplicate_of and skip_duplicates and index not in images:
            dup += 1
            continue
        group = _number_group(pq.kind)
        key = (is_lab, group)
        next_numbers[key] = next_numbers.get(key, 0) + 1
        q = Question.objects.create(
            bank=bank, number=str(next_numbers[key]), label=pq.label, kind=pq.kind, stem=pq.stem,
            answer=pq.answer_raw if pq.kind == Question.DESC else "",
            explanation=pq.explanation, note=pq.note, is_lab=is_lab, image_asset=images.get(index),
        )
        Option.objects.bulk_create([
            Option(question=q, text=t, is_correct=c, order=i)
            for i, (t, c) in enumerate(zip(pq.options, pq.correct))
        ])
        created += 1
    return created, bad, dup


def bank_as_dicts(bank):
    for q in bank.questions.filter(
        is_in_sorting_bin=False, is_in_review_bin=False,
    ).exclude(kind=Question.IMAGE).prefetch_related("options"):
        yield {
            "number": q.number, "label": q.label, "kind": q.kind, "stem": q.stem,
            "options": [(o.text, o.is_correct) for o in q.options.all()],
            "answer": q.answer, "explanation": q.explanation, "note": q.note,
        }
