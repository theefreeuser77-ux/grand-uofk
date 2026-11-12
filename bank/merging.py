"""The same question twice in one chapter: finding the copies and merging them.

Two questions count as the same when they have the same type and number of options, their
text (question + options, ignoring case, spaces and punctuation) is at least 93% alike, no
real word of the question changed, and they have the same answer. Importing uses that strict
level, since nobody checks each match; the Duplicates page also lists looser matches (85%)
for an admin to tick by hand. The answer check keeps apart look-alikes such as "The sural nerve
is a branch of…" and "The saphenous nerve is a branch of…".

A merged question keeps one copy and carries every university's label ("U of K, Neelain"),
so each university's filter still finds it while "All questions" shows it once.
"""
import difflib
import re

from django.db import transaction

from . import sources
from .models import Comment, ExamAttempt, ExamTemplate, Question, QuestionNote, SavedQuestion, TopicSearch

SIMILAR = 0.93
SIMILAR_LOOSE = 0.85
SIMILAR_ANSWER = 0.85
SIMILAR_WORD = 0.9
# A word only one copy has that flips the meaning ("…is found" / "…is NOT found").
FLIPPING_WORDS = {"not", "no", "except", "never", "false", "incorrect", "untrue", "least", "without"}


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _words(text):
    return re.findall(r"[a-z0-9]+", re.sub(r"n['’]t\b", " not", (text or "").lower()))


def _same_wording(a_words, b_words):
    """False when the question wording differs by a real word, not just spelling or spacing:
    "VLDL"/"HDL", "anterior"/"posterior", "DNA"/"RNA" differ; "oesophagus"/"esophagus" and
    "power house"/"powerhouse" do not. Extra filler words ("both", "(are)") are fine unless
    they flip the meaning ("not", "except")."""
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a_words, b_words, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        a_part, b_part = a_words[i1:i2], b_words[j1:j2]
        if FLIPPING_WORDS & (set(a_part) ^ set(b_part)):
            return False
        if a_part and b_part:
            a_text, b_text = "".join(a_part), "".join(b_part)
            if difflib.SequenceMatcher(None, a_text, b_text, autojunk=False).ratio() < SIMILAR_WORD:
                return False
    return True


class Fingerprint:
    """What two questions are compared by."""

    def __init__(self, kind, stem, options, correct):
        self.kind = kind
        self.size = len(options)
        self.words = _words(stem)
        self.options = "".join(sorted(_norm(o) for o in options))
        self.text = _norm(stem) + self.options
        if kind == Question.TF:
            self.answer = "".join("T" if c else "F" for c in correct)
        elif kind == Question.MCQ:
            self.answer = "".join(sorted(_norm(o) for o, c in zip(options, correct) if c))
        else:
            self.answer = ""  # descriptive answers are free text, written differently each time

    @classmethod
    def of_question(cls, question):
        options = list(question.options.all())
        return cls(question.kind, question.stem, [o.text for o in options], [o.is_correct for o in options])

    @classmethod
    def of_parsed(cls, parsed):
        return cls(parsed.kind, parsed.stem, parsed.options, parsed.correct)


def _alike_text(a, b, similar):
    if a == b:
        return True
    if abs(len(a) - len(b)) > (1 - similar) * max(len(a), len(b)) + 1:
        return False
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return (matcher.real_quick_ratio() >= similar and matcher.quick_ratio() >= similar
            and matcher.ratio() >= similar)


def alike(a, b, similar=SIMILAR):
    """Same type and size, text at least ``similar`` alike and no real word changed in the
    question (answers not compared). True/False statements must match at the strict level
    on their own: many share a stem like "Is the following statement true or false?", so
    the whole text can look alike while the statement is different."""
    if a.kind != b.kind or a.size != b.size or not a.text or not b.text:
        return False
    if a.text == b.text:
        return True
    if a.kind == Question.TF and not _alike_text(a.options, b.options, max(similar, SIMILAR)):
        return False
    return _alike_text(a.text, b.text, similar) and _same_wording(a.words, b.words)


def same_answer(a, b):
    if a.answer == b.answer:
        return True
    if a.kind != Question.MCQ:
        return False
    return difflib.SequenceMatcher(None, a.answer, b.answer, autojunk=False).ratio() >= SIMILAR_ANSWER


def same(a, b, similar=SIMILAR):
    return alike(a, b, similar) and same_answer(a, b)


def mergeable(queryset):
    """Questions that may be merged: chapter questions only (not Library books, which carry the
    book's name as their label), not in a bin, and not tied to an image, since the text of an
    image question ("Identify this structure") does not say which question it is."""
    return queryset.filter(
        book_chapter__isnull=True, is_in_sorting_bin=False, is_in_review_bin=False,
        image_asset__isnull=True, answer_image_asset__isnull=True,
    ).exclude(kind=Question.IMAGE)


# ------------------------------------------------------------------ importing
def mark_existing(parsed_questions, bank, is_lab=False):
    """Flag imported questions that are already in ``bank`` (the same question, possibly
    worded a little differently). Each gets ``same_as`` (the existing question) and a
    ``duplicate_of`` message saying whether its label will be added to that question."""
    existing = [
        (question, Fingerprint.of_question(question))
        for question in mergeable(bank.questions.filter(is_lab=is_lab)).prefetch_related("options")
    ]
    for parsed in parsed_questions:
        parsed.same_as = None
        if not parsed.ok or not parsed.stem or parsed.image or parsed.duplicate_of.startswith("same as"):
            continue
        mine = Fingerprint.of_parsed(parsed)
        match = next((question for question, theirs in existing if same(mine, theirs)), None)
        if match is None:
            continue
        parsed.same_as = match
        where = f"already in this chapter ({match.number or f'#{match.pk}'})"
        if parsed.label and sources.add_label(match.label, parsed.label) != match.label:
            where += f"; “{parsed.label}” will be added to its labels"
        parsed.duplicate_of = where


def add_labels(parsed_questions):
    """Give each existing question found by mark_existing() the imported copy's label.
    Returns how many questions got a new label."""
    added = 0
    for parsed in parsed_questions:
        question = getattr(parsed, "same_as", None)
        if question is None or not parsed.label:
            continue
        question.refresh_from_db(fields=["label"])
        label = sources.add_label(question.label, parsed.label)
        if label != question.label:
            question.label = label
            question.save(update_fields=["label", "updated_at"])
            added += 1
    return added


# ------------------------------------------------------------------ cleaning up
def find_groups(bank, skip=(), similar=SIMILAR):
    """Copies of one question in ``bank``: a list of (group, sure) with each group sorted
    keeper-first. ``sure`` is True when every copy matches at the strict level; looser
    groups (down to ``similar``) need a person to check them. Also returns pairs whose text
    matches strictly but whose answers differ, which are left for a person to check."""
    questions = list(
        mergeable(bank.questions.exclude(pk__in=skip)).prefetch_related("options").order_by("pk")
    )
    prints = {q.pk: Fingerprint.of_question(q) for q in questions}
    loose = {q.pk: q.pk for q in questions}
    strict = dict(loose)

    def root(parent, pk):
        while parent[pk] != pk:
            parent[pk] = parent[parent[pk]]
            pk = parent[pk]
        return pk

    conflicts = []
    for i, a in enumerate(questions):
        for b in questions[i + 1:]:
            pa, pb = prints[a.pk], prints[b.pk]
            if a.is_lab != b.is_lab or not alike(pa, pb, similar):
                continue
            is_strict = similar >= SIMILAR or alike(pa, pb)
            if same_answer(pa, pb):
                loose[root(loose, b.pk)] = root(loose, a.pk)
                if is_strict:
                    strict[root(strict, b.pk)] = root(strict, a.pk)
            elif is_strict:
                conflicts.append((a, b))
    groups = {}
    for q in questions:
        groups.setdefault(root(loose, q.pk), []).append(q)
    found = []
    for group in groups.values():
        if len(group) > 1:
            sure = len({root(strict, q.pk) for q in group}) == 1
            found.append((sorted(group, key=keeper_order), sure))
    return found, conflicts


def keeper_order(question):
    """U of K's copy is kept first (it went through the full review), then the oldest."""
    return (sources.UOFK not in sources.sources_of(question.label), question.pk)


@transaction.atomic
def merge_group(group):
    """Keep group[0], give it every label, move students' data from the others to it, and
    delete the others. Returns {removed id: kept id}."""
    keeper, copies = group[0], group[1:]
    label = keeper.label
    for copy in copies:
        label = sources.add_label(label, copy.label)
    if label != keeper.label:
        keeper.label = label
        keeper.save(update_fields=["label", "updated_at"])
    for copy in copies:
        for saved in SavedQuestion.objects.filter(question=copy):
            if SavedQuestion.objects.filter(user_id=saved.user_id, question=keeper).exists():
                saved.delete()
            else:
                saved.question = keeper
                saved.save(update_fields=["question"])
        for note in QuestionNote.objects.filter(question=copy):
            mine = QuestionNote.objects.filter(user_id=note.user_id, question=keeper).first()
            if mine:
                if note.text.strip() and note.text.strip() not in mine.text:
                    mine.text = (mine.text.rstrip() + "\n\n" + note.text.strip())[:5000]
                    mine.save(update_fields=["text", "updated_at"])
                note.delete()
            else:
                note.question = keeper
                note.save(update_fields=["question"])
        Comment.objects.filter(question=copy).update(question=keeper)
    return {copy.pk: keeper.pk for copy in copies}


def _remap_ids(ids, moved):
    out = []
    for pk in ids:
        pk = moved.get(pk, pk)
        if pk not in out:
            out.append(pk)
    return out


@transaction.atomic
def finish(moved, banks):
    """After merging: point mock exams at the kept questions, delete the removed copies and
    forget saved topic searches of the changed chapters (they are rebuilt on the next search)."""
    if not moved:
        return
    for attempt in ExamAttempt.objects.all().only("pk", "question_ids", "responses"):
        if not any(pk in moved for pk in attempt.question_ids):
            continue
        responses = dict(attempt.responses)
        for old, new in moved.items():
            if str(old) in responses:
                answer = responses.pop(str(old))
                responses.setdefault(str(new), answer)
        attempt.question_ids = _remap_ids(attempt.question_ids, moved)
        attempt.responses = responses
        attempt.save(update_fields=["question_ids", "responses"])
    for template in ExamTemplate.objects.exclude(snapshot=None).only("pk", "snapshot"):
        ids = (template.snapshot or {}).get("question_ids") or []
        if any(pk in moved for pk in ids):
            template.snapshot = {**template.snapshot, "question_ids": _remap_ids(ids, moved)}
            template.save(update_fields=["snapshot"])
    TopicSearch.objects.filter(bank__in=banks).delete()
    Question.objects.filter(pk__in=moved).delete()
