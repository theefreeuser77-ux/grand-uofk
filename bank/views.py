import json
import math
import random
import uuid
from functools import wraps
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, redirect_to_login
from django.contrib.admin.views.decorators import staff_member_required
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.core.cache import cache
from django.contrib.sessions.models import Session
from django.http import FileResponse, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods, require_POST

from . import parser, services, sources
from . import ai_marking, figures
from .forms import (
    ExamTemplateForm, ImageQuestionForm, ImageUploadForm, QuestionForm, label_for_book, option_formset,
    validate_image_file,
)
from .image_storage import (
    ImageStorageError, delete_question_image, image_uploads_enabled, upload_question_image,
)
from .models import (
    AIExamAssessment, BackupSnapshot, Book, BookChapter, Comment, ImageTarget, Option, Question, QuestionBank, QuestionImage,
    ExamAttempt, ExamTemplate, ExamTemplateChapter, QuestionNote, SavedQuestion, Subject, SubjectSection,
    SEMESTER_CHOICES,
)
from . import full_backup
from .subject_backup import BackupError, restore_backup


# ---------------------------------------------------------------- helpers
def perm_required(perm):
    """Anonymous -> login page; logged in without the permission -> 403."""
    def deco(view):
        @wraps(view)
        def wrapped(request, *a, **kw):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
            if not request.user.has_perm(perm):
                return HttpResponseForbidden("You don't have permission to do this.")
            return view(request, *a, **kw)
        return wrapped
    return deco


def staff_required(view):
    """Require a staff account for curation workflows, independent of model perms."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        if not request.user.is_staff:
            return HttpResponseForbidden("Staff access is required.")
        return view(request, *args, **kwargs)
    return wrapped


def safe_next(request, default):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    ok = url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()})
    return nxt if nxt and ok else default


def app_manifest(request):
    path = settings.BASE_DIR / "bank" / "static" / "bank" / "manifest.webmanifest"
    response = FileResponse(path.open("rb"), content_type="application/manifest+json")
    response["Cache-Control"] = "no-cache"
    return response


def service_worker(request):
    path = settings.BASE_DIR / "bank" / "static" / "bank" / "service-worker.js"
    response = FileResponse(path.open("rb"), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


def _user_notes(user, question_ids):
    return dict(QuestionNote.objects.filter(user=user, question_id__in=question_ids)
                .values_list("question_id", "text"))


def serialize(questions, user, review=False, include_answers=False):
    qs = list(questions)
    saved, notes = set(), {}
    if user.is_authenticated and qs:
        saved = set(SavedQuestion.objects.filter(user=user, question_id__in=[q.id for q in qs])
                    .values_list("question_id", flat=True))
        notes = _user_notes(user, [q.id for q in qs])
    data = []
    for q in qs:
        opts = list(q.options.all())
        kind = (
            q.image_answer_mode
            if q.kind == Question.IMAGE and q.image_answer_mode != Question.IMAGE_IDENTIFY
            else q.kind
        )
        question = {
            "id": q.id, "number": q.number or str(len(data) + 1),
            "label": sources.visible_label(q.label, user, from_book=q.book_chapter_id is not None), "kind": kind,
            "stem": q.stem, "saved": q.id in saved, "my_note": notes.get(q.id, ""), "is_lab": q.is_lab,
            "image_answer_mode": q.image_answer_mode if q.kind == Question.IMAGE else None,
            "multi": kind == Question.MCQ and sum(o.is_correct for o in opts) > 1,
            "options": [{"id": o.id, "letter": chr(65 + i), "text": o.text} for i, o in enumerate(opts)],
        }
        if q.image_asset_id:
            question.update({
                "image_url": q.image_asset.secure_url if q.image_asset_id else "",
                "image_width": q.image_asset.width if q.image_asset_id else 0,
                "image_height": q.image_asset.height if q.image_asset_id else 0,
            })
        if q.answer_image_asset_id:
            question["answer_image_url"] = q.answer_image_asset.secure_url
        if kind == Question.IMAGE:
            question.update({
                "targets": [
                    {
                        "id": target.pk,
                        "label": target.label,
                        "x_percent": float(target.x_percent) if target.x_percent is not None else None,
                        "y_percent": float(target.y_percent) if target.y_percent is not None else None,
                        **({"answer": target.answer} if review or include_answers else {}),
                    }
                    for target in q.image_targets.all()
                ],
            })
        if include_answers:
            question.update({
                "answer": q.answer,
                "explanation": q.explanation,
                "note": q.note,
            })
            if kind == Question.TF:
                question["answer_key"] = {str(o.id): o.is_correct for o in opts}
            elif kind == Question.MCQ:
                question["correct_ids"] = [o.id for o in opts if o.is_correct]
        if review:
            question.update({
                "answer": q.answer,
                "explanation": q.explanation,
                "note": q.note,
                "is_in_sorting_bin": q.is_in_sorting_bin,
                "is_in_review_bin": q.is_in_review_bin,
                "source_chapter": q.bank.path if q.is_in_sorting_bin or q.is_in_review_bin else "",
                "options": [
                    {"id": o.id, "letter": chr(65 + i), "text": o.text, "is_correct": o.is_correct}
                    for i, o in enumerate(opts)
                ],
            })
        data.append(question)
    return data


def json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return None


# ---------------------------------------------------------------- browsing
HOME_COUNTS_KEY = "bank:home-counts"
HOME_COUNTS_SECONDS = 7 * 24 * 60 * 60


def _home_counts():
    """Chapter and question counts per subject, plus the number of books, recounted once a week.

    The exact numbers matter to nobody, so the home page reuses one count instead of
    counting every question on every visit.
    """
    counts = cache.get(HOME_COUNTS_KEY)
    if counts is None:
        rows = Subject.objects.annotate(
            n_decks=Count("decks", distinct=True),
            n_q=Count(
                "decks__questions",
                filter=Q(decks__questions__is_in_sorting_bin=False, decks__questions__is_in_review_bin=False,
                         decks__questions__book_chapter__isnull=True),
                distinct=True,
            ),
        ).values_list("pk", "n_decks", "n_q")
        counts = {"subjects": {pk: (decks, questions) for pk, decks, questions in rows},
                  "books": Book.objects.count()}
        cache.set(HOME_COUNTS_KEY, counts, HOME_COUNTS_SECONDS)
    return counts


@login_required
def home(request):
    counts = _home_counts()
    subjects = list(Subject.objects.order_by("order", "name"))
    for subject in subjects:
        subject.n_decks, subject.n_q = counts["subjects"].get(subject.pk, (0, 0))
    unfiled = QuestionBank.objects.filter(subject__isnull=True).annotate(
        n=Count("questions", filter=Q(questions__is_in_sorting_bin=False, questions__is_in_review_bin=False,
                                      questions__book_chapter__isnull=True))
    )
    return render(request, "bank/home.html", {
        "subjects": subjects, "unfiled": unfiled,
        "total_questions": sum(s.n_q for s in subjects),
        "total_chapters": sum(s.n_decks for s in subjects),
        "saved_count": request.user.saved_questions.count() if request.user.is_authenticated else 0,
        "book_count": counts["books"],
    })


@login_required
def subject_detail(request, slug):
    subject = get_object_or_404(Subject, slug=slug)
    decks = subject.decks.annotate(
        n=Count("questions", filter=Q(questions__is_in_sorting_bin=False, questions__is_in_review_bin=False,
                                      questions__book_chapter__isnull=True))
    ).order_by("semester", "title")
    sections = subject.sections.annotate(
        n_decks=Count("decks", distinct=True),
        n_q=Count(
            "decks__questions",
            filter=Q(decks__questions__is_in_sorting_bin=False, decks__questions__is_in_review_bin=False,
                         decks__questions__book_chapter__isnull=True),
            distinct=True,
        ),
    )
    return render(request, "bank/subject_detail.html", {
        "subject": subject, "decks": decks,
        "sections": sections, "unsectioned_decks": decks.filter(section__isnull=True),
    })


@login_required
def section_detail(request, slug, section_slug):
    subject = get_object_or_404(Subject, slug=slug)
    section = get_object_or_404(SubjectSection, subject=subject, slug=section_slug)
    decks = section.decks.annotate(
        n=Count("questions", filter=Q(questions__is_in_sorting_bin=False, questions__is_in_review_bin=False,
                                      questions__book_chapter__isnull=True))
    ).order_by("semester", "title")
    return render(request, "bank/section_detail.html", {
        "subject": subject, "section": section, "decks": decks,
    })


@login_required
def bank_detail(request, slug):
    bank = get_object_or_404(QuestionBank.objects.select_related("subject", "section"), slug=slug)
    questions = bank.questions.filter(is_in_sorting_bin=False, is_in_review_bin=False, book_chapter__isnull=True)
    type_filters = {
        "total": Q(),
        "mcq_tf": Q(is_lab=False, kind__in=[Question.MCQ, Question.TF]),
        "descriptive": Q(is_lab=False, kind=Question.DESC),
        "image": Q(is_lab=False, kind=Question.IMAGE, image_answer_mode__in=[
            choice[0] for choice in Question.IMAGE_ANSWER_CHOICES
        ]),
        "labs": Q(is_lab=True),
    }
    keys = sources.filters_for(request.user)
    aggregates = {}
    for type_key, type_q in type_filters.items():
        for key in keys:
            condition = type_q & sources.filter_q(key)
            aggregates[f"{type_key}_in_{key}"] = Count("id", filter=condition) if condition else Count("id")
    totals = questions.aggregate(**aggregates)
    counts = {type_key: {key: totals[f"{type_key}_in_{key}"] for key in keys} for type_key in type_filters}
    selected = sources.normalize(request.GET.get("label", sources.ALL), request.user)
    source_options = [
        {"key": key, "name": sources.filter_name(key, request.user), "count": counts["total"][key]}
        for key in keys
        # Admins only see sources this chapter has; students always get the same three choices.
        if not request.user.is_staff or key in (sources.ALL, selected) or counts["total"][key]
    ]
    return render(request, "bank/bank_detail.html", {
        "bank": bank,
        "count": counts["total"][sources.ALL],
        "mcq_tf_count": counts["mcq_tf"][sources.ALL],
        "descriptive_count": counts["descriptive"][sources.ALL],
        "image_count": counts["image"][sources.ALL],
        "lab_count": counts["labs"][sources.ALL],
        "count_json": {type_key: json.dumps(value) for type_key, value in counts.items()},
        "source_options": source_options,
        "selected_label_filter": selected,
    })


def _quiz_context(request, **kw):
    ctx = {"shuffle": "1" if request.GET.get("shuffle") else "0"}
    ctx.update(kw)
    return ctx


EXAM_FORMATS = ("mcq", "tf", "descriptive", "identify")


def _exam_questions(bank=None):
    questions = Question.objects.filter(
        is_in_sorting_bin=False, is_in_review_bin=False, book_chapter__isnull=True,
    )
    return questions.filter(bank=bank) if bank else questions


def _exam_format_query(question_format):
    if question_format == "mcq":
        return Q(kind=Question.MCQ) | Q(kind=Question.IMAGE, image_answer_mode=Question.IMAGE_MCQ)
    if question_format == "tf":
        return Q(kind=Question.TF) | Q(kind=Question.IMAGE, image_answer_mode=Question.TF)
    if question_format == "descriptive":
        return Q(kind=Question.DESC) | Q(kind=Question.IMAGE, image_answer_mode=Question.DESC)
    return Q(
        kind=Question.IMAGE,
        image_answer_mode=Question.IMAGE_IDENTIFY,
        image_targets__isnull=False,
    )


def _exam_question_counts(bank):
    return _exam_question_counts_by_bank([bank]).get(
        bank.pk, {question_format: 0 for question_format in EXAM_FORMATS},
    )


def _exam_question_counts_by_bank(banks):
    banks = list(banks)
    counts = {
        bank.pk: {question_format: 0 for question_format in EXAM_FORMATS}
        for bank in banks
    }
    if not banks:
        return counts
    annotations = {
        question_format: Count(
            "pk", filter=_exam_format_query(question_format), distinct=True,
        )
        for question_format in EXAM_FORMATS
    }
    for row in _exam_questions().filter(bank__in=banks).values("bank_id").annotate(**annotations):
        counts[row["bank_id"]] = {
            question_format: row[question_format]
            for question_format in EXAM_FORMATS
        }
    return counts


def _allocate_weighted(total, weights):
    raw = {key: total * percentage / 100 for key, percentage in weights}
    quotas = {key: math.floor(amount) for key, amount in raw.items()}
    remaining = total - sum(quotas.values())
    priority = sorted(
        raw,
        key=lambda key: raw[key] - quotas[key],
        reverse=True,
    )
    for key in priority[:remaining]:
        quotas[key] += 1
    return quotas


def _exam_quotas(total, mix):
    return _allocate_weighted(
        total, [(question_format, mix.get(question_format, 0)) for question_format in EXAM_FORMATS],
    )


def _start_exam(
    user, bank, name, question_count, mix, time_limit_minutes=None, template=None,
    chapter_weights=None, subject=None,
):
    if chapter_weights is None:
        chapter_weights = [(bank, 100)] if bank else []
    if template and template.snapshot:
        question_ids = template.snapshot.get("question_ids")
        configuration = template.snapshot.get("configuration")
        if (
            not isinstance(question_ids, list)
            or not question_ids
            or any(isinstance(question_id, bool) or not isinstance(question_id, int) for question_id in question_ids)
            or len(set(question_ids)) != len(question_ids)
            or not isinstance(configuration, dict)
        ):
            raise ValueError("This saved exam snapshot is invalid.")
        if not chapter_weights or sum(percentage for _, percentage in chapter_weights) != 100:
            raise ValueError("Saved exam chapters are no longer available.")
        available_ids = set(
            _exam_questions().filter(
                pk__in=question_ids,
                bank_id__in=[chapter.pk for chapter, _ in chapter_weights],
            ).values_list("pk", flat=True)
        )
        if available_ids != set(question_ids):
            raise ValueError("Some saved exam questions are no longer available.")
        saved_configuration = configuration.copy()
        saved_configuration["question_count"] = len(question_ids)
        saved_configuration["time_limit_minutes"] = time_limit_minutes
        expires_at = (
            timezone.now() + timedelta(minutes=time_limit_minutes)
            if time_limit_minutes else None
        )
        return ExamAttempt.objects.create(
            user=user,
            bank=chapter_weights[0][0],
            subject=subject,
            template=template,
            title=name,
            configuration=saved_configuration,
            question_ids=question_ids,
            expires_at=expires_at,
        )
    if not chapter_weights or sum(percentage for _, percentage in chapter_weights) != 100:
        raise ValueError("Chapter percentages must add up to 100%.")
    chapter_quotas = _allocate_weighted(
        question_count, [(chapter.pk, percentage) for chapter, percentage in chapter_weights],
    )
    available_by_chapter = _exam_question_counts_by_bank(
        [chapter for chapter, _ in chapter_weights],
    )
    question_ids_by_format = {question_format: [] for question_format in EXAM_FORMATS}
    chapter_format_quotas = {}
    for chapter, _ in chapter_weights:
        chapter_count = chapter_quotas[chapter.pk]
        format_quotas = _exam_quotas(chapter_count, mix)
        chapter_format_quotas[str(chapter.pk)] = format_quotas
        available = available_by_chapter[chapter.pk]
        unavailable = {
            question_format: quota
            for question_format, quota in format_quotas.items()
            if quota > available[question_format]
        }
        if unavailable:
            details = ", ".join(
                f"{question_format}: need {quota}, have {available[question_format]}"
                for question_format, quota in unavailable.items()
            )
            raise ValueError(
                f"Not enough eligible questions in {chapter.title} for its exam allocation ({details})."
            )
        for question_format, quota in format_quotas.items():
            if quota:
                ids = list(
                    _exam_questions(chapter).filter(_exam_format_query(question_format))
                    .distinct().order_by("?").values_list("pk", flat=True)[:quota]
                )
                question_ids_by_format[question_format].extend(ids)
    question_ids = []
    for question_format in EXAM_FORMATS:
        random.shuffle(question_ids_by_format[question_format])
        question_ids.extend(question_ids_by_format[question_format])

    now = timezone.now()
    expires_at = (
        now + timedelta(minutes=time_limit_minutes) if time_limit_minutes else None
    )
    return ExamAttempt.objects.create(
        user=user,
        bank=chapter_weights[0][0],
        subject=subject,
        template=template,
        title=name,
        configuration={
            "question_count": question_count,
            "mix": mix,
            "time_limit_minutes": time_limit_minutes,
            "quotas": {
                question_format: len(question_ids_by_format[question_format])
                for question_format in EXAM_FORMATS
            },
            "chapter_weights": [
                {"bank_id": chapter.pk, "title": chapter.title, "percentage": percentage}
                for chapter, percentage in chapter_weights
            ],
            "chapter_quotas": chapter_quotas,
            "chapter_format_quotas": chapter_format_quotas,
        },
        question_ids=question_ids,
        expires_at=expires_at,
    )


@login_required
def deck_exams(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    form = ExamTemplateForm(request.POST or None, show_publish=request.user.is_staff)
    if request.method == "POST" and form.is_valid():
        try:
            template = ExamTemplate.objects.create(
                user=request.user,
                bank=bank,
                name=form.cleaned_data["name"],
                question_count=form.cleaned_data["question_count"],
                mix=form.mix(),
                time_limit_minutes=form.cleaned_data["time_limit_minutes"],
                is_published=bool(form.cleaned_data.get("is_published")),
            )
        except IntegrityError:
            form.add_error("name", "You already have an exam setup with this name for this deck.")
        else:
            try:
                attempt = _start_exam(
                    request.user, bank, template.name, template.question_count,
                    template.mix, template.time_limit_minutes, template=template,
                )
            except ValueError as error:
                template.delete()
                form.add_error(None, str(error))
            else:
                return redirect("bank:exam_attempt", attempt_id=attempt.pk)
    templates = ExamTemplate.objects.filter(bank=bank).select_related("user")
    admin_templates = templates.filter(user__is_staff=True, is_published=True)
    my_templates = templates.filter(user=request.user).exclude(
        user__is_staff=True, is_published=True,
    )
    recent_attempts = ExamAttempt.objects.filter(user=request.user, bank=bank)[:10]
    return render(request, "bank/deck_exams.html", {
        "bank": bank,
        "form": form,
        "admin_templates": admin_templates,
        "my_templates": my_templates,
        "recent_attempts": recent_attempts,
        "question_counts": _exam_question_counts(bank),
    })


@login_required
def subject_exams(request, slug):
    subject = get_object_or_404(Subject, slug=slug)
    banks = list(subject.decks.select_related("section").order_by("semester", "title"))
    form = ExamTemplateForm(request.POST or None, banks=banks, show_publish=request.user.is_staff)
    counts_by_bank = _exam_question_counts_by_bank(banks)
    if request.method == "POST" and form.is_valid():
        template = None
        try:
            template = ExamTemplate.objects.create(
                user=request.user,
                subject=subject,
                name=form.cleaned_data["name"],
                question_count=form.cleaned_data["question_count"],
                mix=form.mix(),
                time_limit_minutes=form.cleaned_data["time_limit_minutes"],
                is_published=bool(form.cleaned_data.get("is_published")),
            )
            ExamTemplateChapter.objects.bulk_create([
                ExamTemplateChapter(template=template, bank=bank, percentage=percentage)
                for bank, percentage in form.chapter_weights()
            ])
        except IntegrityError:
            if template:
                template.delete()
            form.add_error("name", "You already have an exam setup with this name for this subject.")
        else:
            try:
                weights = form.chapter_weights()
                attempt = _start_exam(
                    request.user, weights[0][0], template.name, template.question_count,
                    template.mix, template.time_limit_minutes, template=template,
                    chapter_weights=weights, subject=subject,
                )
            except ValueError as error:
                template.delete()
                form.add_error(None, str(error))
            else:
                return redirect("bank:exam_attempt", attempt_id=attempt.pk)
    chapter_rows = []
    for row in form.chapter_rows:
        row["question_counts"] = counts_by_bank[row["bank"].pk]
        chapter_rows.append(row)
    templates = ExamTemplate.objects.filter(subject=subject).select_related("user").prefetch_related(
        "chapters__bank",
    )
    admin_templates = templates.filter(user__is_staff=True, is_published=True)
    my_templates = templates.filter(user=request.user).exclude(
        user__is_staff=True, is_published=True,
    )
    recent_attempts = ExamAttempt.objects.filter(user=request.user).filter(
        Q(subject=subject) | Q(bank__subject=subject)
    )[:10]
    return render(request, "bank/subject_exams.html", {
        "subject": subject,
        "form": form,
        "chapter_rows": chapter_rows,
        "semesters": sorted({bank.semester for bank in banks}),
        "question_counts": {
            question_format: sum(
                row["question_counts"][question_format] for row in chapter_rows
            )
            for question_format in EXAM_FORMATS
        },
        "admin_templates": admin_templates,
        "my_templates": my_templates,
        "recent_attempts": recent_attempts,
    })


@login_required
@require_POST
def exam_template_start(request, template_id):
    template = get_object_or_404(
        ExamTemplate.objects.select_related("bank", "subject").filter(
            Q(user=request.user) | Q(is_published=True, user__is_staff=True),
        ),
        pk=template_id,
    )
    if template.subject_id:
        chapter_weights = [
            (chapter.bank, chapter.percentage)
            for chapter in template.chapters.select_related("bank")
        ]
        if any(chapter.subject_id != template.subject_id for chapter, _ in chapter_weights):
            messages.error(request, "A saved chapter no longer belongs to this subject.")
            return redirect("bank:subject_exams", slug=template.subject.slug)
        bank = chapter_weights[0][0] if chapter_weights else None
        subject = template.subject
    else:
        chapter_weights = None
        bank = template.bank
        subject = None
    try:
        attempt = _start_exam(
            request.user, bank, template.name, template.question_count,
            template.mix, template.time_limit_minutes, template=template,
            chapter_weights=chapter_weights, subject=subject,
        )
    except ValueError as error:
        messages.error(request, str(error))
        destination = "bank:subject_exams" if template.subject_id else "bank:deck_exams"
        slug = template.subject.slug if template.subject_id else template.bank.slug
        return redirect(destination, slug=slug)
    return redirect("bank:exam_attempt", attempt_id=attempt.pk)


def _saved_attempt_template_name(attempt):
    base = f"{attempt.title} (saved)"[:100]
    name = base
    suffix = 2
    while ExamTemplate.objects.filter(
        user=attempt.user,
        bank=attempt.bank if attempt.subject_id is None else None,
        subject=attempt.subject,
        name=name,
    ).exists():
        extra = f" ({suffix})"
        name = f"{base[:100 - len(extra)]}{extra}"
        suffix += 1
    return name


@login_required
@require_POST
def api_exam_save_template(request, attempt_id):
    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("bank", "subject"),
        pk=attempt_id,
        user=request.user,
    )
    if attempt.submitted_at is None:
        return JsonResponse({"error": "Finish the exam before saving it as a template."}, status=409)
    review = _exam_review_data(attempt)
    if not review["report_ready"]:
        return JsonResponse(
            {"error": "Complete every self-assessment before saving this exam."},
            status=409,
        )
    if review["question_count"] != len(attempt.question_ids):
        return JsonResponse(
            {"error": "Some exam questions are no longer available, so this exam cannot be saved."},
            status=409,
        )
    chapter_weights = attempt.configuration.get("chapter_weights", [])
    if attempt.subject_id:
        chapter_ids = [
            row.get("bank_id") for row in chapter_weights if isinstance(row, dict)
        ] if isinstance(chapter_weights, list) else []
        chapters = list(
            QuestionBank.objects.filter(subject=attempt.subject, pk__in=chapter_ids)
        )
        if (
            not chapter_ids
            or len(chapter_ids) != len(set(chapter_ids))
            or len(chapters) != len(chapter_ids)
        ):
            return JsonResponse(
                {"error": "The chapters in this exam are no longer available."},
                status=409,
            )
    with transaction.atomic():
        template, created = ExamTemplate.objects.get_or_create(
            source_attempt=attempt,
            defaults={
                "user": request.user,
                "bank": attempt.bank if attempt.subject_id is None else None,
                "subject": attempt.subject,
                "name": _saved_attempt_template_name(attempt),
                "question_count": len(attempt.question_ids),
                "mix": attempt.configuration.get("mix", {}),
                "time_limit_minutes": attempt.configuration.get("time_limit_minutes"),
                "snapshot": {
                    "question_ids": list(attempt.question_ids),
                    "configuration": attempt.configuration,
                },
            },
        )
        if created and attempt.subject_id:
            chapter_by_id = {chapter.pk: chapter for chapter in chapters}
            ExamTemplateChapter.objects.bulk_create([
                ExamTemplateChapter(
                    template=template,
                    bank=chapter_by_id[row["bank_id"]],
                    percentage=row["percentage"],
                )
                for row in chapter_weights
            ])
    return JsonResponse({"saved": True, "created": created, "template_name": template.name})


@login_required
def exam_template_edit(request, template_id):
    template = get_object_or_404(
        ExamTemplate.objects.select_related("bank", "subject"), pk=template_id, user=request.user,
    )
    if template.snapshot:
        messages.info(request, "Saved question sets keep their original questions and cannot be edited.")
        destination = "bank:subject_exams" if template.subject_id else "bank:deck_exams"
        slug = template.subject.slug if template.subject_id else template.bank.slug
        return redirect(destination, slug=slug)
    subject = template.subject
    banks = (
        list(subject.decks.select_related("section").order_by("semester", "title"))
        if subject else None
    )
    chapter_initials = {
        chapter.bank_id: chapter.percentage
        for chapter in template.chapters.all()
    }
    initial = {
        "name": template.name,
        "question_count": template.question_count,
        "mcq_percent": template.mix.get("mcq", 40),
        "tf_percent": template.mix.get("tf", 20),
        "descriptive_percent": template.mix.get("descriptive", 20),
        "identify_percent": template.mix.get("identify", 20),
        "time_limit_minutes": template.time_limit_minutes,
    }
    form = ExamTemplateForm(
        request.POST or None,
        initial=initial,
        banks=banks,
        show_publish=request.user.is_staff,
        chapter_initials=chapter_initials,
        publish_initial=template.is_published,
    )
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                template.name = form.cleaned_data["name"]
                template.question_count = form.cleaned_data["question_count"]
                template.mix = form.mix()
                template.time_limit_minutes = form.cleaned_data["time_limit_minutes"]
                if request.user.is_staff:
                    template.is_published = bool(form.cleaned_data.get("is_published"))
                template.save()
                if subject:
                    template.chapters.all().delete()
                    ExamTemplateChapter.objects.bulk_create([
                        ExamTemplateChapter(template=template, bank=bank, percentage=percentage)
                        for bank, percentage in form.chapter_weights()
                    ])
        except IntegrityError:
            form.add_error("name", "You already have an exam setup with this name for this scope.")
        else:
            messages.success(request, "Exam template updated.")
            destination = "bank:subject_exams" if subject else "bank:deck_exams"
            slug = subject.slug if subject else template.bank.slug
            return redirect(destination, slug=slug)
    chapter_rows = []
    counts_by_bank = _exam_question_counts_by_bank(banks or [])
    for row in form.chapter_rows:
        row["question_counts"] = counts_by_bank[row["bank"].pk]
        chapter_rows.append(row)
    return render(request, "bank/exam_template_edit.html", {
        "template": template,
        "subject": subject,
        "form": form,
        "chapter_rows": chapter_rows,
        "semesters": sorted({bank.semester for bank in banks or []}),
        "cancel_url": (
            reverse("bank:subject_exams", args=[subject.slug])
            if subject else reverse("bank:deck_exams", args=[template.bank.slug])
        ),
    })


@login_required
@require_POST
def exam_template_delete(request, template_id):
    template = get_object_or_404(
        ExamTemplate.objects.select_related("bank", "subject"), pk=template_id, user=request.user,
    )
    subject = template.subject
    slug = subject.slug if subject else template.bank.slug
    template.delete()
    messages.success(request, "Exam setup deleted.")
    destination = "bank:subject_exams" if subject else "bank:deck_exams"
    return redirect(destination, slug=slug)


@login_required
@require_POST
def exam_attempt_delete(request, attempt_id):
    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("bank", "subject"),
        pk=attempt_id,
        user=request.user,
    )
    destination = "bank:subject_exams" if attempt.subject_id else "bank:deck_exams"
    slug = attempt.subject.slug if attempt.subject_id else attempt.bank.slug
    attempt.delete()
    messages.success(request, "Exam attempt deleted.")
    return redirect(destination, slug=slug)


@login_required
@ensure_csrf_cookie
def exam_attempt(request, attempt_id):
    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("bank", "subject", "shared_from"),
        pk=attempt_id,
        user=request.user,
    )
    _expire_exam_if_needed(attempt)
    back_url = (
        reverse("bank:subject_exams", args=[attempt.subject.slug])
        if attempt.subject_id else reverse("bank:deck_exams", args=[attempt.bank.slug])
    )
    return render(request, "bank/exam_attempt.html", {
        "attempt": attempt,
        "api_url": reverse("bank:api_exam_attempt", args=[attempt.pk]),
        "answer_url": reverse("bank:api_exam_answer", args=[attempt.pk]),
        "submit_url": reverse("bank:api_exam_submit", args=[attempt.pk]),
        "ai_assess_url": reverse("bank:api_exam_ai_assess", args=[attempt.pk]),
        "share_url": reverse("bank:api_exam_share", args=[attempt.pk]),
        "save_template_url": reverse("bank:api_exam_save_template", args=[attempt.pk]),
        "delete_attempt_url": reverse("bank:exam_attempt_delete", args=[attempt.pk]),
        "practice_again_url": (
            reverse("bank:exam_share_retry", args=[attempt.pk])
            if attempt.shared_from_id and attempt.shared_from.share_token else ""
        ),
        "back_url": back_url,
    })


def _create_shared_practice(source_attempt, user):
    time_limit = source_attempt.configuration.get("time_limit_minutes")
    expires_at = (
        timezone.now() + timedelta(minutes=time_limit)
        if isinstance(time_limit, int) and not isinstance(time_limit, bool) and time_limit > 0
        else None
    )
    return ExamAttempt.objects.create(
        user=user,
        bank_id=source_attempt.bank_id,
        subject_id=source_attempt.subject_id,
        shared_from=source_attempt,
        title=source_attempt.title,
        configuration=source_attempt.configuration.copy(),
        question_ids=list(source_attempt.question_ids),
        expires_at=expires_at,
    )


@login_required
def exam_share(request, token):
    source_attempt = get_object_or_404(
        ExamAttempt.objects.select_related("bank", "subject"),
        share_token=token,
        submitted_at__isnull=False,
    )
    report = _exam_review_data(source_attempt)
    if not report["report_ready"] or report["question_count"] != len(source_attempt.question_ids):
        return HttpResponse("Some questions in this shared exam are no longer available.", status=410)
    with transaction.atomic():
        source_attempt = get_object_or_404(
            ExamAttempt.objects.select_for_update(),
            pk=source_attempt.pk,
            share_token=token,
            submitted_at__isnull=False,
        )
        prior_practice = ExamAttempt.objects.filter(
            user=request.user,
            shared_from=source_attempt,
        ).order_by("-started_at", "-pk").first()
        if prior_practice:
            return redirect("bank:exam_attempt", attempt_id=prior_practice.pk)
        attempt = _create_shared_practice(source_attempt, request.user)
    return redirect("bank:exam_attempt", attempt_id=attempt.pk)


@login_required
@require_POST
def exam_share_retry(request, attempt_id):
    prior_practice = get_object_or_404(
        ExamAttempt.objects.select_related("shared_from"),
        pk=attempt_id,
        user=request.user,
        submitted_at__isnull=False,
        shared_from__share_token__isnull=False,
    )
    source_attempt = prior_practice.shared_from
    report = _exam_review_data(source_attempt)
    if not report["report_ready"] or report["question_count"] != len(source_attempt.question_ids):
        return HttpResponse("Some questions in this shared exam are no longer available.", status=410)
    with transaction.atomic():
        source_attempt = get_object_or_404(
            ExamAttempt.objects.select_for_update(),
            pk=source_attempt.pk,
            share_token__isnull=False,
            submitted_at__isnull=False,
        )
        attempt = _create_shared_practice(source_attempt, request.user)
    return redirect("bank:exam_attempt", attempt_id=attempt.pk)


@login_required
@require_POST
def api_exam_share(request, attempt_id):
    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("bank", "subject"),
        pk=attempt_id,
        user=request.user,
    )
    data = json_body(request)
    if not isinstance(data, dict) or data.get("action") not in {"create", "revoke"}:
        return JsonResponse({"error": "Choose whether to create or revoke the share link."}, status=400)
    if data["action"] == "revoke":
        attempt.share_token = None
        attempt.save(update_fields=["share_token"])
        return JsonResponse({"share_url": None})
    report = _exam_review_data(attempt)
    if attempt.submitted_at is None or not report["report_ready"]:
        return JsonResponse(
            {"error": "Finish the exam and all self-assessments before sharing it."},
            status=409,
        )
    if report["question_count"] != len(attempt.question_ids):
        return JsonResponse(
            {"error": "Some exam questions are no longer available, so this exam cannot be shared."},
            status=409,
        )
    if attempt.share_token is None:
        attempt.share_token = uuid.uuid4()
        attempt.save(update_fields=["share_token"])
    url = reverse("bank:exam_share", args=[attempt.share_token])
    return JsonResponse({"share_url": request.build_absolute_uri(url)})


def _expire_exam_if_needed(attempt):
    if attempt.submitted_at is None and attempt.expires_at and timezone.now() >= attempt.expires_at:
        attempt.submitted_at = attempt.expires_at
        attempt.save(update_fields=["submitted_at"])
        return True
    return False


def _exam_question_payload(question, index, reveal, response=None):
    kind = (
        question.image_answer_mode
        if question.kind == Question.IMAGE else question.kind
    )
    payload = {
        "id": question.pk,
        "number": question.number or str(index + 1),
        "kind": kind,
        "stem": question.stem,
        "options": [
            {
                "id": option.pk,
                "letter": chr(65 + option_index),
                "text": option.text,
            }
            for option_index, option in enumerate(question.options.all())
        ],
        "response": response or {},
    }
    if question.image_asset_id:
        payload.update({
            "image_url": question.image_asset.secure_url,
            "image_width": question.image_asset.width,
            "image_height": question.image_asset.height,
        })
    if kind == Question.IMAGE_IDENTIFY:
        payload["targets"] = [
            {
                "id": target.pk, "label": target.label,
                "x_percent": float(target.x_percent) if target.x_percent is not None else None,
                "y_percent": float(target.y_percent) if target.y_percent is not None else None,
                **({"answer": target.answer} if reveal else {}),
            }
            for target in question.image_targets.all()
        ]
    if reveal:
        payload.update({
            "answer": question.answer,
            "explanation": question.explanation,
            "note": question.note,
        })
        if question.answer_image_asset_id:
            payload["answer_image_url"] = question.answer_image_asset.secure_url
        if kind == Question.MCQ:
            payload["correct_ids"] = [
                option.pk for option in question.options.all() if option.is_correct
            ]
        elif kind == Question.TF:
            payload["answer_key"] = {
                str(option.pk): option.is_correct for option in question.options.all()
            }
    return payload


def _exam_review_data(attempt):
    questions = list(
        Question.objects.filter(pk__in=attempt.question_ids)
        .select_related("image_asset", "answer_image_asset")
        .prefetch_related("options", "image_targets")
    )
    by_id = {question.pk: question for question in questions}
    ordered = [by_id[question_id] for question_id in attempt.question_ids if question_id in by_id]
    items = [
        _exam_question_payload(
            question, index, attempt.submitted_at is not None,
            attempt.responses.get(str(question.pk), {}),
        )
        for index, question in enumerate(ordered)
    ]
    if attempt.submitted_at is not None:
        notes = _user_notes(attempt.user, [question.pk for question in ordered])
        for item in items:
            item["my_note"] = notes.get(item["id"], "")
    points = 0.0
    missed = []
    assessment_complete = True
    report_rows = []
    for question, item in zip(ordered, items):
        response = item["response"]
        kind = item["kind"]
        earned = 0.0
        possible = 1.0
        reason = ""
        if kind == Question.MCQ:
            correct = response.get("selected") in item.get("correct_ids", [])
            earned = float(correct)
            reason = "Correct" if correct else "Incorrect or unanswered"
        elif kind == Question.TF:
            answers = response.get("answers", {})
            correct = bool(item["options"]) and all(
                answers.get(str(option["id"])) is item.get("answer_key", {}).get(str(option["id"]))
                for option in item["options"]
            )
            earned = float(correct)
            reason = "Correct" if correct else "Incorrect or unanswered"
        elif attempt.submitted_at and kind == Question.DESC:
            rating = response.get("ratings", {}).get("answer")
            if rating not in {"known", "missed"}:
                assessment_complete = False
                reason = "Awaiting self-assessment"
            else:
                earned = float(rating == "known")
                if response.get("assessment_source") == "ai":
                    reason = "AI marked correct" if earned else "AI marked incorrect"
                else:
                    reason = "Self-rated as known" if earned else "Self-rated as missed"
        elif attempt.submitted_at and kind == Question.IMAGE_IDENTIFY:
            ratings = response.get("ratings", {})
            target_ids = [str(target["id"]) for target in item.get("targets", [])]
            rated = [ratings.get(target_id) for target_id in target_ids]
            if not target_ids or any(value not in {"known", "missed"} for value in rated):
                assessment_complete = False
                reason = "Awaiting self-assessment"
            else:
                earned = sum(value == "known" for value in rated) / len(target_ids)
                reason = (
                    "All targets self-rated as known" if earned == 1
                    else "Some targets self-rated as missed" if earned
                    else "All targets self-rated as missed"
                )
        points += earned
        report_row = {
            "id": question.pk,
            "number": item["number"],
            "stem": item["stem"],
            "earned": earned,
            "possible": possible,
            "reason": reason,
        }
        report_rows.append(report_row)
        if attempt.submitted_at and reason != "Awaiting self-assessment" and earned < possible:
            missed.append(report_row)

    report_ready = attempt.submitted_at is not None and assessment_complete
    score_percent = round(points / len(items) * 100) if report_ready and items else 0
    result = {
        "title": attempt.title,
        "submitted": attempt.submitted_at is not None,
        "questions": items,
        "question_count": len(items),
        "report_ready": report_ready,
        "expires_at": attempt.expires_at.isoformat() if attempt.expires_at else None,
        "started_at": attempt.started_at.isoformat(),
    }
    if report_ready:
        result.update({
            "score_percent": score_percent,
            "points_earned": round(points, 2),
            "points_possible": len(items),
            "missed": missed,
            "report": report_rows,
        })
    return result


@login_required
def api_exam_attempt(request, attempt_id):
    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("bank"), pk=attempt_id, user=request.user,
    )
    _expire_exam_if_needed(attempt)
    result = _exam_review_data(attempt)
    if attempt.submitted_at is not None:
        task = AIExamAssessment.objects.filter(attempt=attempt).first()
        result["ai_assessment_status"] = task.status if task else "not_requested"
    if result["report_ready"]:
        result["template_saved"] = ExamTemplate.objects.filter(source_attempt=attempt).exists()
    if result["report_ready"]:
        result["share_url"] = (
            request.build_absolute_uri(reverse("bank:exam_share", args=[attempt.share_token]))
            if attempt.share_token else None
        )
    return JsonResponse(result)


def _exam_question_for_attempt(attempt, question_id):
    if question_id not in attempt.question_ids:
        return None
    return get_object_or_404(
        Question.objects.prefetch_related("options", "image_targets"),
        pk=question_id,
    )


def _validated_exam_response(question, response):
    if not isinstance(response, dict):
        raise ValueError("Answer must be an object.")
    kind = question.image_answer_mode if question.kind == Question.IMAGE else question.kind
    if kind == Question.MCQ:
        selected = response.get("selected")
        valid_ids = {option.pk for option in question.options.all()}
        if selected is not None and (
            isinstance(selected, bool) or not isinstance(selected, int) or selected not in valid_ids
        ):
            raise ValueError("Choose a valid option.")
        return {"selected": selected}
    if kind == Question.TF:
        answers = response.get("answers", {})
        valid_ids = {str(option.pk) for option in question.options.all()}
        if not isinstance(answers, dict) or any(
            key not in valid_ids or not isinstance(value, bool) for key, value in answers.items()
        ):
            raise ValueError("Choose True or False for valid statements.")
        return {"answers": answers}
    if kind == Question.DESC:
        text = response.get("text", "")
        if not isinstance(text, str) or len(text) > 10000:
            raise ValueError("Descriptive responses must be 10,000 characters or fewer.")
        return {"text": text}
    targets = response.get("targets", {})
    valid_ids = {str(target.pk) for target in question.image_targets.all()}
    if not isinstance(targets, dict) or any(
        key not in valid_ids or not isinstance(value, str) or len(value) > 1000
        for key, value in targets.items()
    ):
        raise ValueError("Enter valid answers for the labelled targets.")
    return {"targets": targets}


@login_required
@require_POST
def api_exam_answer(request, attempt_id):
    attempt = get_object_or_404(ExamAttempt, pk=attempt_id, user=request.user)
    expired_submission = (
        attempt.submitted_at is not None
        and attempt.expires_at is not None
        and attempt.submitted_at >= attempt.expires_at
    )
    if expired_submission or _expire_exam_if_needed(attempt):
        return JsonResponse({"error": "The exam has ended.", "submitted": True}, status=409)
    data = json_body(request)
    if not isinstance(data, dict) or isinstance(data.get("question_id"), bool) or not isinstance(data.get("question_id"), int):
        return JsonResponse({"error": "Invalid exam answer."}, status=400)
    question = _exam_question_for_attempt(attempt, data["question_id"])
    if question is None:
        return JsonResponse({"error": "This question is not in this exam."}, status=400)
    response = data.get("response")
    kind = question.image_answer_mode if question.kind == Question.IMAGE else question.kind
    current = attempt.responses.get(str(question.pk), {})
    if attempt.submitted_at:
        ratings = data.get("ratings")
        if kind not in {Question.DESC, Question.IMAGE_IDENTIFY} or not isinstance(ratings, dict):
            return JsonResponse({"error": "This answer can no longer be changed."}, status=400)
        allowed = {"known", "missed"}
        valid_keys = (
            {str(target.pk) for target in question.image_targets.all()}
            if kind == Question.IMAGE_IDENTIFY else {"answer"}
        )
        if not ratings or any(
            key not in valid_keys or value not in allowed for key, value in ratings.items()
        ):
            return JsonResponse({"error": "Invalid self-assessment."}, status=400)
        if kind == Question.DESC and ratings.get("answer") == "known" and not current.get("text", "").strip():
            return JsonResponse({"error": "Enter your answer before rating it as known."}, status=400)
        if kind == Question.IMAGE_IDENTIFY and any(
            value == "known" and not current.get("targets", {}).get(key, "").strip()
            for key, value in ratings.items()
        ):
            return JsonResponse({"error": "Enter a response before rating a target as known."}, status=400)
        current["ratings"] = ratings
        if kind == Question.DESC:
            current["assessment_source"] = "manual"
    else:
        try:
            current.update(_validated_exam_response(question, response))
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
    attempt.responses[str(question.pk)] = current
    attempt.save(update_fields=["responses"])
    return JsonResponse({"saved": True})


@login_required
@require_POST
def api_exam_submit(request, attempt_id):
    attempt = get_object_or_404(ExamAttempt, pk=attempt_id, user=request.user)
    data = json_body(request)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Invalid exam submission."}, status=400)
    submitted_responses = data.get("responses")
    if submitted_responses is not None:
        if not isinstance(submitted_responses, dict):
            return JsonResponse({"error": "Exam answers must be an object."}, status=400)
        allowed_ids = {str(question_id) for question_id in attempt.question_ids}
        if any(question_id not in allowed_ids for question_id in submitted_responses):
            return JsonResponse({"error": "This exam contains an unknown question."}, status=400)
        questions = {
            str(question.pk): question
            for question in Question.objects.filter(pk__in=attempt.question_ids)
            .prefetch_related("options", "image_targets")
        }
        validated_responses = {}
        try:
            for question_id, response in submitted_responses.items():
                question = questions.get(question_id)
                if question is None:
                    return JsonResponse({"error": "This question is no longer available."}, status=400)
                validated_responses[question_id] = _validated_exam_response(question, response)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        if attempt.submitted_at is None:
            attempt.responses = {
                **attempt.responses,
                **{
                    question_id: {
                        **attempt.responses.get(question_id, {}),
                        **response,
                    }
                    for question_id, response in validated_responses.items()
                },
            }
    if attempt.submitted_at is None:
        attempt.submitted_at = min(timezone.now(), attempt.expires_at) if attempt.expires_at else timezone.now()
        update_fields = ["submitted_at"]
        if submitted_responses is not None:
            update_fields.append("responses")
        attempt.save(update_fields=update_fields)
    result = _exam_review_data(attempt)
    if result["report_ready"]:
        result["template_saved"] = ExamTemplate.objects.filter(source_attempt=attempt).exists()
        result["share_url"] = (
            request.build_absolute_uri(reverse("bank:exam_share", args=[attempt.share_token]))
            if attempt.share_token else None
        )
    return JsonResponse(result)


@login_required
@require_http_methods(["GET", "POST"])
def api_exam_ai_assess(request, attempt_id):
    attempt = get_object_or_404(ExamAttempt, pk=attempt_id, user=request.user)
    if attempt.submitted_at is None:
        return JsonResponse({"error": "Submit the exam before requesting AI assessment."}, status=409)
    if request.method == "GET":
        task = AIExamAssessment.objects.filter(attempt=attempt).first()
        if task and task.status in {AIExamAssessment.QUEUED, AIExamAssessment.PROCESSING}:
            ai_marking.process_next_queued_assessment()
            task.refresh_from_db()
        payload = {"assessment_status": task.status if task else "not_requested"}
        if task and task.status == AIExamAssessment.QUEUED:
            payload["queue_position"] = AIExamAssessment.objects.filter(
                status=AIExamAssessment.QUEUED,
            ).filter(
                Q(created_at__lt=task.created_at)
                | Q(created_at=task.created_at, pk__lte=task.pk)
            ).count()
        elif task and task.status == AIExamAssessment.FAILED:
            payload["error"] = task.error_message
        elif task and task.status == AIExamAssessment.COMPLETED:
            attempt.refresh_from_db()
            result = _exam_review_data(attempt)
            payload.update(result)
            if result["report_ready"]:
                payload["template_saved"] = ExamTemplate.objects.filter(source_attempt=attempt).exists()
                payload["share_url"] = (
                    request.build_absolute_uri(reverse("bank:exam_share", args=[attempt.share_token]))
                    if attempt.share_token else None
                )
        return JsonResponse(payload)

    data = json_body(request)
    if isinstance(data, dict) and data.get("action") == "cancel":
        AIExamAssessment.objects.filter(
            attempt=attempt,
            status__in=[AIExamAssessment.QUEUED, AIExamAssessment.PROCESSING],
        ).update(status=AIExamAssessment.CANCELLED)
        return JsonResponse({"cancelled": True})

    task, created = AIExamAssessment.objects.get_or_create(attempt=attempt)
    if not created and task.status in {AIExamAssessment.FAILED, AIExamAssessment.CANCELLED}:
        task.status = AIExamAssessment.QUEUED
        task.error_message = ""
        task.save(update_fields=["status", "error_message", "updated_at"])
    return JsonResponse({"assessment_status": task.status}, status=202)


@login_required
@ensure_csrf_cookie
def quiz(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    quiz_type = request.GET.get("type", "mcq_tf")
    if quiz_type not in {"mcq_tf", "descriptive", "image", "labs"}:
        quiz_type = "mcq_tf"
    label_filter = sources.normalize(request.GET.get("label", sources.ALL), request.user)
    query = f"type={quiz_type}"
    if label_filter != "all":
        query += f"&label={label_filter}"
    api = reverse("bank:api_questions", args=[bank.slug]) + "?" + query
    key = f"deck:{bank.slug}:{quiz_type}"
    if label_filter != "all":
        key += f":label:{label_filter}"
    return render(request, "bank/quiz.html", _quiz_context(
        request, title=bank.title, key=key, question_type=quiz_type,
        api=api, back=bank.get_absolute_url(), review=False, edit_api="",
        start_question=request.GET.get("resume_question", ""),
        can_sort=request.user.is_staff,
        sort_api=reverse("bank:api_question_sorting_bin", args=[0]),
        review_bin_api=reverse("bank:api_question_review_bin", args=[0]),
    ))


@staff_required
@ensure_csrf_cookie
def review_deck(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    review_type = request.GET.get("type", "mcq_tf")
    if review_type not in {"mcq_tf", "descriptive", "image", "labs"}:
        review_type = "mcq_tf"
    review_label = {
        "descriptive": "Descriptive",
        "image": "Identify",
        "mcq_tf": "MCQs & True / False",
        "labs": "Labs",
    }[review_type]
    query = f"review=1&type={review_type}"
    return render(request, "bank/quiz.html", _quiz_context(
        request, title=f"Review {review_label} · {bank.title}",
        key=f"review:{bank.slug}:{review_type}",
        question_type=review_type,
        api=reverse("bank:api_questions", args=[bank.slug]) + "?" + query,
        back=bank.get_absolute_url(), review=True,
        edit_api=reverse("bank:api_question_edit", args=[0]), can_sort=True, sort_api="",
        review_bin_api=reverse("bank:api_question_review_bin", args=[0]),
    ))


@login_required
@ensure_csrf_cookie
def saved_quiz(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    notes = "?notes=1" if _notes_only(request) else ""
    return render(request, "bank/quiz.html", _quiz_context(
        request, title=f"Saved{' with notes' if notes else ''} · {bank.title}",
        key=f"saved{'-notes' if notes else ''}:{request.user.pk}:{bank.slug}",
        api=reverse("bank:api_saved_questions", args=[bank.slug]) + notes,
        back=reverse("bank:saved_deck", args=[bank.slug]) + notes, review=False, edit_api="",
        question_type="saved", can_sort=False, sort_api=""))


@login_required
def export_bank(request, slug):
    bank = get_object_or_404(QuestionBank.objects.select_related("subject", "section"), slug=slug)
    rows = services.bank_as_dicts(bank)
    if not request.user.is_staff:  # keep other universities' names private to admins
        rows = ({**row, "label": sources.visible_label(row["label"], request.user)} for row in rows)
    text = parser.to_text(bank.title, rows,
                          subject=bank.subject.name if bank.subject else None, semester=bank.semester,
                          section=bank.section.name if bank.section else None)
    if bank.questions.filter(is_in_sorting_bin=False, is_in_review_bin=False, book_chapter__isnull=True,
                             image_asset__isnull=False).exists():
        text = "# NOTE: Attached image files are not included in this text export.\n\n" + text
    resp = HttpResponse(text, content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{bank.slug}.txt"'
    return resp


REPLACE_CONFIRMATION = "REPLACE"


@staff_member_required
def subject_backup(request):
    return render(request, "bank/subject_backup.html", {
        "max_backup_mb": full_backup.MAX_BACKUP_BYTES // (1024 * 1024),
        "restore_points": BackupSnapshot.objects.defer("data"),
        "confirmation": REPLACE_CONFIRMATION,
        "keep": full_backup.KEEP_SNAPSHOTS,
    })


def _backup_download(data, name):
    response = HttpResponse(data, content_type="application/gzip")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


@staff_member_required
def export_subject_backup(request):
    try:
        data = full_backup.encode(full_backup.build_payload(include_private=False))
    except full_backup.BackupError as error:
        return HttpResponse(str(error), status=413, content_type="text/plain; charset=utf-8")
    return _backup_download(data, f"deca-shine-backup-{timezone.localtime():%Y-%m-%d-%H%M}.json.gz")


def _count_label(count, noun):
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def _replace_and_report(request, payload, reason):
    """Replace the database, keep the admin signed in if their account exists in the backup."""
    username = request.user.get_username()
    try:
        snapshot, counts = full_backup.replace_database(payload, reason)
    except full_backup.BackupError as error:
        messages.error(request, str(error))
        return redirect("bank:subject_backup")
    user = get_user_model().objects.filter(username=username, is_active=True, is_staff=True).first()
    # Every session row was wiped with the old database, so this request needs a fresh one.
    request.session.cycle_key()
    if user:
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    else:
        logout(request)
    messages.success(
        request,
        "The site now holds the backup: " + ", ".join((
            _count_label(counts.get("bank.Question", 0), "question"),
            _count_label(counts.get("bank.QuestionBank", 0), "chapter"),
            _count_label(counts.get("bank.Book", 0), "library book"),
            _count_label(counts.get("auth.User", 0), "account"),
        )) + f". Before replacing anything, a copy of the previous database was saved as a restore point "
        f"({timezone.localtime(snapshot.created_at):%d %b %Y, %H:%M}). You can download it or restore it "
        "from the Backup page.",
    )
    if not user:
        messages.warning(request, "Your account is not in the backup, so you have been signed out. "
                                  "Sign in with an admin account from the backup.")
        return redirect("bank:login")
    return redirect("bank:subject_backup")


@staff_member_required
@require_POST
def import_subject_backup(request):
    backup_file = request.FILES.get("backup")
    if backup_file is None:
        messages.error(request, "Choose a backup file to import.")
        return redirect("bank:subject_backup")
    if backup_file.size > full_backup.MAX_BACKUP_BYTES:
        messages.error(request, "The backup file is larger than the 100 MB limit.")
        return redirect("bank:subject_backup")
    raw = backup_file.read()
    try:
        payload = full_backup.decode(raw)
    except full_backup.BackupError as error:
        messages.error(request, str(error))
        return redirect("bank:subject_backup")

    if full_backup.is_full_backup(payload):
        if request.POST.get("confirm", "").strip() != REPLACE_CONFIRMATION:
            messages.error(request, f"Type {REPLACE_CONFIRMATION} in the box to confirm replacing the whole site.")
            return redirect("bank:subject_backup")
        return _replace_and_report(request, payload, f"Before importing {backup_file.name}")

    # An older questions-only backup: it has no accounts, so it is only ever added, never replaced.
    try:
        result = restore_backup(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except BackupError as error:
        messages.error(request, str(error))
        return redirect("bank:subject_backup")
    added, skipped = result["added"], result["skipped"]
    messages.success(
        request,
        "This was an older questions-only backup, so nothing was deleted: missing content was added. "
        "Added " + ", ".join((
            _count_label(added["subjects"], "subject"),
            _count_label(added["sections"], "section"),
            _count_label(added["decks"], "deck"),
            _count_label(added["questions"], "question"),
            _count_label(added["options"], "option"),
            _count_label(added["images"], "image asset"),
            _count_label(added["image_targets"], "image target"),
        )) + f"; skipped {sum(skipped.values()):,} matching existing records.",
    )
    return redirect("bank:subject_backup")


@staff_member_required
def download_restore_point(request, pk):
    snapshot = get_object_or_404(BackupSnapshot, pk=pk)
    return _backup_download(
        bytes(snapshot.data),
        f"deca-shine-restore-point-{timezone.localtime(snapshot.created_at):%Y-%m-%d-%H%M}.json.gz",
    )


@staff_member_required
@require_POST
def restore_restore_point(request, pk):
    snapshot = get_object_or_404(BackupSnapshot, pk=pk)
    if request.POST.get("confirm", "").strip() != REPLACE_CONFIRMATION:
        messages.error(request, f"Type {REPLACE_CONFIRMATION} in the box to confirm restoring this version.")
        return redirect("bank:subject_backup")
    label = f"{timezone.localtime(snapshot.created_at):%d %b %Y, %H:%M}"
    try:
        payload = full_backup.decode(bytes(snapshot.data))
    except full_backup.BackupError as error:
        messages.error(request, str(error))
        return redirect("bank:subject_backup")
    return _replace_and_report(request, payload, f"Before restoring the restore point from {label}")


# ---------------------------------------------------------------- JSON API
@login_required
def api_questions(request, slug):
    """Return quiz questions and, outside staff review, their practice answer keys."""
    bank = get_object_or_404(QuestionBank, slug=slug)
    review = request.GET.get("review") == "1"
    if review and not request.user.is_staff:
        return HttpResponseForbidden("Staff access is required.")
    qs = bank.questions.filter(is_in_sorting_bin=False, is_in_review_bin=False, book_chapter__isnull=True)
    qs = sources.apply(qs, sources.normalize(request.GET.get("label", sources.ALL), request.user))
    quiz_type = request.GET.get("type")
    if quiz_type == "descriptive":
        qs = qs.filter(kind=Question.DESC, is_lab=False)
    elif quiz_type == "mcq_tf":
        qs = qs.filter(
            kind__in=[Question.MCQ, Question.TF],
            is_lab=False,
        )
    elif quiz_type == "image":
        qs = qs.filter(kind=Question.IMAGE, is_lab=False)
    elif quiz_type == "labs":
        qs = qs.filter(is_lab=True)
    qs = qs.select_related("image_asset", "answer_image_asset").prefetch_related("options", "image_targets")
    return JsonResponse({
        "title": bank.title,
        "questions": serialize(qs, request.user, review=review, include_answers=not review),
    })


@staff_required
def sorting_bin(request):
    questions = Question.objects.filter(is_in_sorting_bin=True).select_related(
        "bank__subject"
    ).order_by("bank__subject__name", "bank__semester", "bank__title", "id")
    groups = []
    for question in questions:
        path = question.bank.path
        if not groups or groups[-1]["path"] != path:
            groups.append({"path": path, "questions": []})
        groups[-1]["questions"].append(question)
    return render(request, "bank/sorting_bin.html", {
        "groups": groups,
        "count": len(questions),
        "prompt": _sorting_bin_prompt(),
    })


@staff_required
def review_bin(request):
    questions = Question.objects.filter(is_in_review_bin=True).select_related(
        "bank__subject"
    ).order_by("bank__subject__name", "bank__semester", "bank__title", "id")
    groups = []
    for question in questions:
        path = question.bank.path
        if not groups or groups[-1]["path"] != path:
            groups.append({"path": path, "questions": []})
        groups[-1]["questions"].append(question)
    return render(request, "bank/review_bin.html", {
        "groups": groups,
        "count": len(questions),
    })


@staff_required
def sorting_bin_export(request):
    questions = Question.objects.filter(is_in_sorting_bin=True).select_related(
        "bank__subject").prefetch_related("options")
    out = [
        "# Sorting-bin export. Preserve ID and SOURCE fields; fill in TARGET fields.",
        "# Use the companion AI prompt to classify and optionally correct these questions.",
        "# Image-dependent questions are not included; review them in the image question editor.",
        "",
    ]
    for question in questions.exclude(kind=Question.IMAGE):
        out += [
            f"Q: {question.stem}",
            f"ID: {question.pk}",
            f"SOURCE_SUBJECT: {question.bank.subject.name if question.bank.subject else ''}",
            f"SOURCE_SEMESTER: {question.bank.semester}",
            f"SOURCE_DECK: {question.bank.title}",
            "TARGET_SUBJECT:",
            "TARGET_SEMESTER:",
            "TARGET_DECK:",
        ]
        if question.number:
            out.append(f"NO: {question.number}")
        if question.label:
            out.append(f"LABEL: {question.label}")
        if question.kind == Question.DESC:
            out += ["TYPE: DESC", f"ANSWER: {question.answer}"]
        else:
            if question.kind == Question.TF:
                out.append("TYPE: TF")
            for index, option in enumerate(question.options.all()):
                out.append(f"{chr(65 + index)}. {option.text}")
            if question.kind == Question.TF:
                out.append("ANSWER: " + "".join("T" if option.is_correct else "F" for option in question.options.all()))
            else:
                out.append("ANSWER: " + ", ".join(
                    chr(65 + index) for index, option in enumerate(question.options.all()) if option.is_correct
                ))
        if question.explanation:
            out.append(f"EXPLANATION: {question.explanation}")
        if question.note:
            out.append(f"NOTE: {question.note}")
        out.append("")
    response = HttpResponse("\n".join(out), content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="sorting-bin.txt"'
    return response


@staff_required
def review_bin_export(request):
    questions = Question.objects.filter(is_in_review_bin=True).select_related("bank__subject").prefetch_related("options")
    out = [
        "# Review-bin export. Preserve ID and source information for manual review or re-file.",
        "# Image-dependent questions are not included; review them in the image question editor.",
        "",
    ]
    for question in questions.exclude(kind=Question.IMAGE):
        out += [
            f"Q: {question.stem}",
            f"ID: {question.pk}",
            f"SOURCE_SUBJECT: {question.bank.subject.name if question.bank.subject else ''}",
            f"SOURCE_SEMESTER: {question.bank.semester}",
            f"SOURCE_DECK: {question.bank.title}",
        ]
        if question.number:
            out.append(f"NO: {question.number}")
        if question.label:
            out.append(f"LABEL: {question.label}")
        if question.kind == Question.DESC:
            out += ["TYPE: DESC", f"ANSWER: {question.answer}"]
        else:
            if question.kind == Question.TF:
                out.append("TYPE: TF")
            for index, option in enumerate(question.options.all()):
                out.append(f"{chr(65 + index)}. {option.text}")
            if question.kind == Question.TF:
                out.append("ANSWER: " + "".join("T" if option.is_correct else "F" for option in question.options.all()))
            else:
                out.append("ANSWER: " + ", ".join(
                    chr(65 + index) for index, option in enumerate(question.options.all()) if option.is_correct
                ))
        if question.explanation:
            out.append(f"EXPLANATION: {question.explanation}")
        if question.note:
            out.append(f"NOTE: {question.note}")
        out.append("")
    response = HttpResponse("\n".join(out), content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="review-bin.txt"'
    return response


def _sorting_bin_prompt():
    destinations = QuestionBank.objects.select_related("subject").order_by(
        "subject__name", "semester", "title"
    )
    by_subject = {}
    for bank in destinations:
        subject_name = bank.subject.name if bank.subject else "Unfiled"
        by_subject.setdefault(subject_name, []).append(
            f"  - TARGET_SEMESTER: {bank.semester} | TARGET_DECK: {bank.title}"
        )
    destination_lines = []
    for subject_name, decks in by_subject.items():
        destination_lines.append(f"SOURCE_SUBJECT: {subject_name}")
        destination_lines.extend(decks)
    available = "\n".join(destination_lines) or "(none)"
    return (
        parser.SORTING_BIN_AI_PROMPT
        + "\nAvailable destination decks, grouped by SOURCE_SUBJECT. Each question may "
        "only use destinations under its own source subject:\n"
        + available
        + "\nChoose only a destination listed under that question's SOURCE_SUBJECT. "
        "Do not transfer questions between subjects or invent subject, semester, or deck names.\n"
    )


def _sorting_bin_import_rows(text):
    result = parser.parse(text)
    rows = []
    seen_ids = set()
    for parsed in result.questions:
        errors = list(parsed.errors)
        try:
            question_id = int(parsed.question_id)
        except (TypeError, ValueError):
            question_id = None
            errors.append("Missing or invalid stable ID.")
        if question_id in seen_ids:
            errors.append("Duplicate ID in this import.")
        if question_id is not None:
            seen_ids.add(question_id)
        question = Question.objects.filter(pk=question_id, is_in_sorting_bin=True).select_related(
            "bank__subject"
        ).first() if question_id is not None else None
        if question is None:
            errors.append("ID does not match a question currently in the sorting bin.")
        subject = Subject.objects.filter(name__iexact=parsed.target_subject.strip()).first()
        if not subject:
            errors.append("Choose an existing target subject.")
        elif question and question.bank.subject_id and subject.pk != question.bank.subject_id:
            errors.append(
                f"Question belongs to {question.bank.subject.name}; it cannot be moved to another subject."
            )
        try:
            semester = int(parsed.target_semester)
            if not 1 <= semester <= 12:
                raise ValueError
        except (TypeError, ValueError):
            semester = None
            errors.append("Target semester must be a number from 1 to 12.")
        deck_title = parsed.target_deck.strip()
        if not deck_title:
            errors.append("Target chapter/deck is required.")
        deck = services.find_deck(subject, semester, deck_title) if subject and semester and deck_title else None
        row = {
            "parsed": parsed, "question": question, "subject": subject, "semester": semester,
            "deck_title": deck_title, "deck": deck, "errors": errors,
            "source_path": question.bank.path if question else "Unknown source",
            "target_path": (
                f"{subject.name if subject else parsed.target_subject or 'Unknown subject'}"
                f" › Semester {semester if semester else parsed.target_semester or '?'}"
                f" › {deck_title or 'Unspecified chapter'}"
            ),
            "new_chapter": bool(subject and semester and deck_title and not deck),
            "answer": parsed.answer_raw if parsed.kind == Question.DESC else (
                "".join("T" if correct else "F" for correct in parsed.correct)
                if parsed.kind == Question.TF else ", ".join(
                    chr(65 + index) for index, correct in enumerate(parsed.correct) if correct
                )
            ),
        }
        rows.append(row)

    groups_by_target = {}
    for row in rows:
        target_path = row["target_path"]
        groups_by_target.setdefault(target_path, []).append(row)
    groups = [{"path": path, "rows": grouped_rows}
              for path, grouped_rows in groups_by_target.items()]
    return rows, groups


@staff_required
def sorting_bin_import(request):
    pending_key = "sorting_bin_pending_import"
    if request.method == "GET":
        text = request.session.get(pending_key)
        if text is not None and request.GET.get("edit") != "1":
            rows, groups = _sorting_bin_import_rows(text)
            return render(request, "bank/sorting_bin_import_preview.html", {
                "groups": groups,
                "row_count": len(rows),
                "valid_count": sum(not row["errors"] for row in rows),
                "error_count": sum(bool(row["errors"]) for row in rows),
            })
        return render(request, "bank/sorting_bin_import.html", {
            "text": text or "", "prompt": _sorting_bin_prompt(),
            "subjects": Subject.objects.order_by("name"),
        })

    action = request.POST.get("action")
    if action == "edit":
        return redirect(f"{reverse('bank:sorting_bin_import')}?edit=1")
    if action == "cancel":
        request.session.pop(pending_key, None)
        return redirect("bank:sorting_bin_import")

    if action == "preview":
        text = request.POST.get("text", "")
        upload = request.FILES.get("file")
        if upload:
            text = upload.read().decode("utf-8-sig", errors="replace")
        request.session[pending_key] = text
        request.session.modified = True
        return redirect("bank:sorting_bin_import")

    if action != "commit":
        messages.error(request, "Start by reviewing the reorganized text before applying it.")
        return redirect("bank:sorting_bin_import")

    text = request.session.get(pending_key)
    if text is None:
        messages.error(request, "There is no pending import to apply. Preview the reorganized text first.")
        return redirect("bank:sorting_bin_import")

    rows, groups = _sorting_bin_import_rows(text)
    if not rows:
        messages.error(request, "No questions were found to import.")
    elif any(row["errors"] for row in rows):
        messages.error(request, "Fix all preview errors before applying any changes.")
    else:
        with transaction.atomic():
            for row in rows:
                parsed = row["parsed"]
                question = row["question"]
                bank = row["deck"] or services.get_or_create_deck(
                    row["subject"], row["semester"], row["deck_title"])
                question.bank = bank
                question.stem = parsed.stem
                question.kind = parsed.kind
                question.answer = parsed.answer_raw if parsed.kind == Question.DESC else ""
                question.label = parsed.label
                question.explanation = parsed.explanation
                question.note = parsed.note
                question.is_in_sorting_bin = False
                question.sorting_bin_at = None
                question.save()
                question.options.all().delete()
                Option.objects.bulk_create([
                    Option(question=question, text=option, is_correct=correct, order=index)
                    for index, (option, correct) in enumerate(zip(parsed.options, parsed.correct))
                ])
        request.session.pop(pending_key, None)
        messages.success(request, f"Updated and filed {len(rows)} sorting-bin question(s).")
        return redirect("bank:sorting_bin")

    return render(request, "bank/sorting_bin_import_preview.html", {
        "groups": groups,
        "row_count": len(rows),
        "valid_count": sum(not row["errors"] for row in rows),
        "error_count": sum(bool(row["errors"]) for row in rows),
    })


@staff_required
@ensure_csrf_cookie
def sorting_bin_review(request):
    return render(request, "bank/quiz.html", _quiz_context(
        request, title="Sorting bin review", key="sorting-bin-review",
        api=reverse("bank:api_sorting_bin_questions"),
        back=reverse("bank:sorting_bin"), review=True,
        start_question=request.GET.get("question", ""),
        edit_api=reverse("bank:api_question_edit", args=[0]), can_sort=False, sort_api="",
        review_bin_api=reverse("bank:api_question_review_bin", args=[0]),
    ))


@staff_required
@ensure_csrf_cookie
def review_bin_review(request):
    return render(request, "bank/quiz.html", _quiz_context(
        request, title="Review bin", key="review-bin-review",
        api=reverse("bank:api_review_bin_questions"),
        back=reverse("bank:review_bin"), review=True,
        start_question=request.GET.get("question", ""),
        edit_api=reverse("bank:api_question_edit", args=[0]), can_sort=False, sort_api="",
        review_bin_api=reverse("bank:api_question_review_bin", args=[0]),
    ))


@staff_required
@require_POST
def api_question_sorting_bin(request, pk):
    question = get_object_or_404(Question, pk=pk, is_in_sorting_bin=False, is_in_review_bin=False)
    question.is_in_sorting_bin = True
    question.is_in_review_bin = False
    question.sorting_bin_at = timezone.now()
    question.review_bin_at = None
    question.save(update_fields=["is_in_sorting_bin", "is_in_review_bin", "sorting_bin_at", "review_bin_at", "updated_at"])
    return JsonResponse({"moved": True, "id": question.pk, "bin": "sorting"})


@staff_required
@require_POST
def api_question_review_bin(request, pk):
    question = get_object_or_404(Question, pk=pk, is_in_review_bin=False, is_in_sorting_bin=False)
    question.is_in_review_bin = True
    question.is_in_sorting_bin = False
    question.review_bin_at = timezone.now()
    question.sorting_bin_at = None
    question.save(update_fields=["is_in_review_bin", "is_in_sorting_bin", "review_bin_at", "sorting_bin_at", "updated_at"])
    return JsonResponse({"moved": True, "id": question.pk, "bin": "review"})


@staff_required
def api_sorting_bin_questions(request):
    questions = Question.objects.filter(is_in_sorting_bin=True).select_related(
        "bank__subject", "image_asset", "answer_image_asset").prefetch_related("options", "image_targets")
    return JsonResponse({"title": "Sorting bin", "questions": serialize(questions, request.user, review=True)})


@staff_required
def api_review_bin_questions(request):
    questions = Question.objects.filter(is_in_review_bin=True).select_related(
        "bank__subject", "image_asset", "answer_image_asset").prefetch_related("options", "image_targets")
    return JsonResponse({"title": "Review bin", "questions": serialize(questions, request.user, review=True)})


@staff_required
@require_POST
def api_question_edit(request, pk):
    question = get_object_or_404(
        Question.objects.select_related("image_asset", "answer_image_asset").prefetch_related("options", "image_targets"),
        pk=pk,
    )
    data = json_body(request)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    delete_question = data.get("delete_question", False)
    if not isinstance(delete_question, bool):
        return JsonResponse({"error": "Invalid delete action."}, status=400)
    if delete_question:
        image_ids = {question.image_asset_id, question.answer_image_asset_id}
        question.delete()
        cleanup_warning = "; ".join(
            warning for image_id in image_ids
            if (warning := _delete_unused_question_image(image_id))
        )
        return JsonResponse({"deleted": True, "warning": cleanup_warning})

    kind = data.get("kind")
    stem = data.get("stem")
    answer = data.get("answer", "")
    label = data.get("label", "")
    explanation = data.get("explanation", "")
    note = data.get("note", "")
    is_lab = data.get("is_lab", question.is_lab)
    options = data.get("options", [])
    if not isinstance(is_lab, bool):
        return JsonResponse({"error": "Invalid Labs state."}, status=400)
    if kind not in {Question.MCQ, Question.TF, Question.DESC, Question.IMAGE}:
        return JsonResponse({"error": "Choose a valid question type."}, status=400)
    targets = data.get("targets", [])
    image_answer_mode = data.get("image_answer_mode", question.image_answer_mode)
    image_format_review = (
        question.kind == Question.IMAGE
        and question.image_answer_mode != Question.IMAGE_IDENTIFY
        and kind in {Question.MCQ, Question.TF, Question.DESC}
    )
    if question.kind == Question.IMAGE and kind != Question.IMAGE and not image_format_review:
        return JsonResponse({"error": "Edit image questions using the image question editor."}, status=400)
    if kind == Question.IMAGE:
        if not question.image_asset_id:
            return JsonResponse({"error": "This image question has no associated image."}, status=400)
        if image_answer_mode not in {choice[0] for choice in Question.IMAGE_ANSWER_CHOICES}:
            return JsonResponse({"error": "Choose an image answer style."}, status=400)
        if image_answer_mode == Question.IMAGE_IDENTIFY:
            if not isinstance(targets, list) or not 1 <= len(targets) <= 50:
                return JsonResponse({"error": "Add between 1 and 50 identification targets."}, status=400)
            labels = set()
            for target in targets:
                if not isinstance(target, dict) or not isinstance(target.get("label"), str) or not target["label"].strip():
                    return JsonResponse({"error": "Every image target needs a label."}, status=400)
                if not isinstance(target.get("answer"), str) or not target["answer"].strip():
                    return JsonResponse({"error": "Every image target needs an answer."}, status=400)
                label = target["label"].strip().casefold()
                if label in labels:
                    return JsonResponse({"error": "Image target labels must be unique."}, status=400)
                labels.add(label)
                x, y = target.get("x_percent"), target.get("y_percent")
                if (x in (None, "")) != (y in (None, "")):
                    return JsonResponse({"error": "Each marker needs both coordinates or neither."}, status=400)
                if x not in (None, ""):
                    try:
                        coordinates = float(x), float(y)
                    except (TypeError, ValueError):
                        return JsonResponse({"error": "Image marker coordinates must be numbers."}, status=400)
                    if not all(0 <= coordinate <= 100 for coordinate in coordinates):
                        return JsonResponse({"error": "Image marker coordinates must be within the image."}, status=400)
            options = []
            targets = [
                {
                    "label": target["label"].strip(),
                    "answer": target["answer"].strip(),
                    "x_percent": target.get("x_percent") or None,
                    "y_percent": target.get("y_percent") or None,
                }
                for target in targets
            ]
        else:
            if not isinstance(options, list) or not 2 <= len(options) <= 8:
                return JsonResponse({"error": "Add between 2 and 8 image MCQ options."}, status=400)
            if any(
                not isinstance(option, dict)
                or not isinstance(option.get("text"), str)
                or not option["text"].strip()
                or not isinstance(option.get("is_correct"), bool)
                for option in options
            ):
                return JsonResponse({"error": "Every image MCQ option needs text and a correct/incorrect selection."}, status=400)
            if not any(option["is_correct"] for option in options):
                return JsonResponse({"error": "Select at least one correct image MCQ option."}, status=400)
            targets = []
    if not isinstance(stem, str) or not stem.strip():
        return JsonResponse({"error": "Question text is required."}, status=400)
    if not all(isinstance(value, str) for value in (answer, label, explanation, note)):
        return JsonResponse({"error": "Answer, label, explanation, and note must be text."}, status=400)
    if len(label.strip()) > Question._meta.get_field("label").max_length:
        return JsonResponse({"error": "Label must be 100 characters or fewer."}, status=400)
    if kind == Question.DESC:
        if not answer.strip():
            if not question.answer_image_asset_id:
                return JsonResponse({"error": "A descriptive question needs an answer or answer image."}, status=400)
        options = []
    elif kind == Question.IMAGE:
        pass
    else:
        minimum = 1 if kind == Question.TF else 2
        if not isinstance(options, list) or not minimum <= len(options) <= 8:
            return JsonResponse({"error": f"Add between {minimum} and 8 options."}, status=400)
        if any(not isinstance(option, dict) or not isinstance(option.get("text"), str)
               or not option["text"].strip() or not isinstance(option.get("is_correct"), bool)
               for option in options):
            return JsonResponse({"error": "Every option needs text and a correct/incorrect selection."}, status=400)
        if kind == Question.MCQ and not any(option["is_correct"] for option in options):
            return JsonResponse({"error": "Select at least one correct option."}, status=400)

    with transaction.atomic():
        question.kind = Question.IMAGE if image_format_review else kind
        if image_format_review:
            question.image_answer_mode = kind
        question.stem = stem.strip()
        question.answer = answer.strip() if kind == Question.DESC else ""
        question.label = label.strip()
        question.is_lab = is_lab
        if kind == Question.IMAGE and not image_format_review:
            question.image_answer_mode = image_answer_mode
        question.explanation = explanation.strip()
        question.note = note.strip()
        move_to_bin = data.get("is_in_sorting_bin", question.is_in_sorting_bin)
        move_to_review_bin = data.get("is_in_review_bin", question.is_in_review_bin)
        if not isinstance(move_to_bin, bool):
            return JsonResponse({"error": "Invalid sorting-bin state."}, status=400)
        if not isinstance(move_to_review_bin, bool):
            return JsonResponse({"error": "Invalid review-bin state."}, status=400)
        question.is_in_sorting_bin = move_to_bin
        question.sorting_bin_at = timezone.now() if move_to_bin else None
        question.is_in_review_bin = move_to_review_bin
        question.review_bin_at = timezone.now() if move_to_review_bin else None
        if move_to_bin:
            question.is_in_review_bin = False
            question.review_bin_at = None
        if move_to_review_bin:
            question.is_in_sorting_bin = False
            question.sorting_bin_at = None
        question.save()

        existing_options = list(question.options.all())
        for index, option_data in enumerate(options):
            if index < len(existing_options):
                option = existing_options[index]
                option.text = option_data["text"].strip()
                option.is_correct = option_data["is_correct"]
                option.order = index
                option.save()
            else:
                Option.objects.create(
                    question=question, text=option_data["text"].strip(),
                    is_correct=option_data["is_correct"], order=index,
                )
        for option in existing_options[len(options):]:
            option.delete()
        if kind == Question.IMAGE and not image_format_review:
            question.image_targets.all().delete()
            if image_answer_mode == Question.IMAGE_IDENTIFY:
                ImageTarget.objects.bulk_create([
                    ImageTarget(question=question, order=index, **target)
                    for index, target in enumerate(targets)
                ])

    updated = Question.objects.select_related("image_asset").prefetch_related(
        "options", "image_targets",
    ).get(pk=question.pk)
    return JsonResponse({"question": serialize([updated], request.user, review=True)[0]})


@login_required
@require_POST
def api_image_target(request, pk, target_pk):
    question = get_object_or_404(
        Question, pk=pk, kind=Question.IMAGE,
        image_answer_mode=Question.IMAGE_IDENTIFY,
        is_in_sorting_bin=False, is_in_review_bin=False,
    )
    target = get_object_or_404(ImageTarget, pk=target_pk, question=question)
    data = json_body(request)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    action = data.get("action")
    if action == "reveal":
        return JsonResponse({"answer": target.answer, "target_id": target.pk})
    if action == "grade" and data.get("grade") in {"known", "missed"}:
        return JsonResponse({"saved": True, "target_id": target.pk})
    return JsonResponse({"error": "Choose reveal or a valid self-assessment."}, status=400)


@login_required
def api_saved_questions(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    qs = (bank.questions.filter(_saved_question_filter(request.user, _notes_only(request)), is_in_review_bin=False)
          .select_related("image_asset", "answer_image_asset").prefetch_related("options", "image_targets").order_by("id"))
    return JsonResponse({
        "title": bank.title,
        "questions": serialize(qs, request.user, include_answers=True),
    })


@login_required
@require_POST
def api_check(request, pk):
    q = get_object_or_404(
        Question.objects.prefetch_related("options"), pk=pk, is_in_sorting_bin=False, is_in_review_bin=False)
    data = json_body(request)
    if data is None:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    kind = (
        q.image_answer_mode
        if q.kind == Question.IMAGE and q.image_answer_mode != Question.IMAGE_IDENTIFY
        else q.kind
    )
    if q.kind == Question.IMAGE and kind == Question.IMAGE:
        return JsonResponse({"error": "Identify targets individually to record self-assessment."}, status=400)
    opts = list(q.options.all())
    out = {"explanation": q.explanation, "note": q.note, "correct": None}
    reveal = bool(data.get("reveal"))
    if kind == Question.DESC:
        if not reveal:
            return JsonResponse({"error": "Descriptive answers can only be requested with reveal."}, status=400)
        out["answer"] = q.answer
    elif kind == Question.TF:
        key = {str(o.id): o.is_correct for o in opts}
        out["key"] = key
        if not reveal:
            answers = data.get("answers") or {}
            per = {k: answers.get(k) is v for k, v in key.items()}
            out["per"] = per
            out["correct"] = all(per.values())
    else:
        correct_ids = [o.id for o in opts if o.is_correct]
        out["correct_ids"] = correct_ids
        if not reveal:
            out["correct"] = data.get("selected") in correct_ids
    return JsonResponse(out)


@require_POST
def api_save(request, pk):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login"}, status=401)
    q = get_object_or_404(Question, pk=pk, is_in_sorting_bin=False, is_in_review_bin=False)
    data = json_body(request) or {}
    existing = SavedQuestion.objects.filter(user=request.user, question=q)
    want = data.get("saved")
    if want is None:
        want = not existing.exists()
    if want:
        SavedQuestion.objects.get_or_create(user=request.user, question=q)
    else:
        existing.delete()
        QuestionNote.objects.filter(user=request.user, question=q).delete()  # a note always lives in Saved
    return JsonResponse({"saved": bool(want)})


@require_POST
def api_note(request, pk):
    """Set the user's private note on a question; empty text deletes it.

    Writing a note also saves the question, and un-saving deletes the note,
    so every noted question shows up under Saved.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login"}, status=401)
    q = get_object_or_404(Question, pk=pk)
    data = json_body(request) or {}
    text = str(data.get("text", "")).strip()
    if len(text) > 5000:
        return JsonResponse({"error": "Note is too long (max 5000 characters)."}, status=400)
    if not text:
        QuestionNote.objects.filter(user=request.user, question=q).delete()
        return JsonResponse({"note": "", "saved": SavedQuestion.objects.filter(user=request.user, question=q).exists()})
    QuestionNote.objects.update_or_create(user=request.user, question=q, defaults={"text": text})
    SavedQuestion.objects.get_or_create(user=request.user, question=q)
    return JsonResponse({"note": text, "saved": True})


@require_POST
def api_comment(request, pk):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login"}, status=401)
    q = get_object_or_404(Question, pk=pk)
    data = json_body(request) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return JsonResponse({"error": "Please write a comment first."}, status=400)
    if len(text) > 2000:
        return JsonResponse({"error": "Comment is too long (max 2000 characters)."}, status=400)
    Comment.objects.create(question=q, user=request.user, text=text)
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------- library
LIVE_QUESTIONS = Q(questions__is_in_sorting_bin=False, questions__is_in_review_bin=False)


@login_required
def library_home(request):
    error = ""
    if request.method == "POST":
        if not request.user.has_perm("bank.add_question"):
            return HttpResponseForbidden("You cannot add books.")
        title = request.POST.get("title", "").strip()
        if not title:
            error = "Write the book’s name."
        elif len(title) > 100:
            error = "Keep the book’s name under 100 characters."
        elif Book.objects.filter(title__iexact=title).exists():
            error = f"“{title}” is already in the Library."
        else:
            book = Book.objects.create(title=title)
            messages.success(request, f"Added “{book.title}”. Now add its chapters.")
            return redirect(book)
    books = Book.objects.annotate(
        n_chapters=Count("chapters", distinct=True),
        n_questions=Count("chapters__questions", filter=Q(
            chapters__questions__is_in_sorting_bin=False, chapters__questions__is_in_review_bin=False,
        ), distinct=True),
    )
    return render(request, "bank/library_home.html", {"books": books, "error": error},
                  status=400 if error else 200)


@login_required
def book_detail(request, slug):
    book = get_object_or_404(Book, slug=slug)
    error = ""
    if request.method == "POST":
        if not request.user.has_perm("bank.add_question"):
            return HttpResponseForbidden("You cannot add chapters.")
        title = request.POST.get("title", "").strip()
        number = request.POST.get("number", "").strip()
        next_number = (book.chapters.aggregate(n=Max("number"))["n"] or 0) + 1
        if not title:
            error = "Write the chapter’s name."
        elif number and not (number.isdigit() and 1 <= int(number) <= 999):
            error = "The chapter number must be between 1 and 999."
        elif book.chapters.filter(number=int(number or next_number)).exists():
            error = f"Chapter {number} already exists in this book."
        else:
            chapter = BookChapter.objects.create(book=book, number=int(number or next_number), title=title[:200])
            messages.success(request, f"Added chapter {chapter.number}. {chapter.title}.")
            return redirect(book)
    chapters = book.chapters.annotate(n_questions=Count("questions", filter=LIVE_QUESTIONS))
    return render(request, "bank/book_detail.html", {"book": book, "chapters": chapters, "error": error},
                  status=400 if error else 200)


@login_required
@ensure_csrf_cookie
def book_chapter_quiz(request, slug, chapter_id):
    chapter = get_object_or_404(BookChapter.objects.select_related("book"), pk=chapter_id, book__slug=slug)
    return render(request, "bank/quiz.html", _quiz_context(
        request, title=f"{chapter.book.title} · {chapter.number}. {chapter.title}", key=f"book:{chapter.pk}",
        api=reverse("bank:api_book_chapter_questions", args=[chapter.pk]),
        back=chapter.book.get_absolute_url(), review=False, edit_api="",
        question_type="book", can_sort=False, sort_api=""))


@login_required
def api_book_chapter_questions(request, chapter_id):
    chapter = get_object_or_404(BookChapter, pk=chapter_id)
    qs = (chapter.questions.filter(is_in_sorting_bin=False, is_in_review_bin=False)
          .select_related("image_asset", "answer_image_asset").prefetch_related("options", "image_targets")
          .order_by("id"))
    return JsonResponse({"title": chapter.title, "questions": serialize(qs, request.user, include_answers=True)})


# ---------------------------------------------------------------- saved section
def _notes_only(request):
    """Saved pages take ?notes=1 to show only questions the user wrote a note on."""
    return request.GET.get("notes") == "1"


def _saved_question_filter(user, notes_only, prefix=""):
    lookup = Q(**{f"{prefix}saved_by__user": user, f"{prefix}is_in_sorting_bin": False})
    if notes_only:
        lookup &= Q(**{f"{prefix}id__in": QuestionNote.objects.filter(user=user).values("question_id")})
    return lookup


def _saved_decks(user, notes_only=False):
    return QuestionBank.objects.annotate(
        saved_n=Count("questions__saved_by", filter=_saved_question_filter(user, notes_only, "questions__"),
                      distinct=True)
    ).filter(saved_n__gt=0)


@login_required
def saved_home(request):
    notes_only = _notes_only(request)
    subjects = Subject.objects.annotate(
        n_saved=Count("decks__questions__saved_by",
                      filter=_saved_question_filter(request.user, notes_only, "decks__questions__"), distinct=True))
    subjects = list(subjects)
    return render(request, "bank/saved_home.html", {
        "subjects": subjects, "notes_only": notes_only, "total_saved": sum(s.n_saved for s in subjects),
    })


@login_required
def saved_subject(request, slug):
    notes_only = _notes_only(request)
    subject = get_object_or_404(Subject, slug=slug)
    decks = _saved_decks(request.user, notes_only).filter(subject=subject).order_by("semester", "title")
    decks = list(decks)
    return render(request, "bank/saved_subject.html", {
        "subject": subject, "decks": decks, "notes_only": notes_only, "total_saved": sum(d.saved_n for d in decks),
    })


@login_required
def saved_deck(request, slug):
    notes_only = _notes_only(request)
    bank = get_object_or_404(QuestionBank.objects.select_related("subject"), slug=slug)
    qs = list(bank.questions.filter(_saved_question_filter(request.user, notes_only))
              .prefetch_related("options").order_by("id"))
    notes = _user_notes(request.user, [q.id for q in qs])
    for q in qs:
        q.my_note = notes.get(q.id, "")
    return render(request, "bank/saved_deck.html", {"bank": bank, "questions": qs, "notes_only": notes_only})


@login_required
@require_POST
def saved_remove(request, pk):
    SavedQuestion.objects.filter(user=request.user, question_id=pk).delete()
    QuestionNote.objects.filter(user=request.user, question_id=pk).delete()
    return redirect(safe_next(request, reverse("bank:saved_home")))


@login_required
def user_settings(request):
    password_form = PasswordChangeForm(request.user)
    if request.method == "POST" and request.POST.get("action") == "password":
        password_form = PasswordChangeForm(request.user, request.POST)
        if password_form.is_valid():
            user = password_form.save()
            # Keeps this device signed in; every other device signed in with the old password is signed out.
            update_session_auth_hash(request, user)
            messages.success(request, "Password changed. Other devices signed in with your old password are now signed out.")
            return redirect("bank:settings")
    user = request.user
    role = "Admin" if user.is_staff else "Editor" if user.has_perm("bank.change_question") else "Student"
    return render(request, "bank/settings.html", {
        "password_form": password_form,
        "role": role,
        "password_open": password_form.is_bound,
    })


# ---------------------------------------------------------------- editor pages
@perm_required("bank.change_question")
def manage_deck(request, slug):
    bank = get_object_or_404(QuestionBank.objects.select_related("subject"), slug=slug)
    qs = bank.questions.filter(is_in_sorting_bin=False).annotate(
        open_comments=Count("comments", filter=Q(comments__resolved=False), distinct=True))
    return render(request, "bank/manage_deck.html", {"bank": bank, "questions": qs})


def _edit_question(request, question, bank=None, is_new=False):
    Formset = option_formset(extra=4 if is_new else 2)
    form = QuestionForm(request.POST or None, request.FILES or None, instance=question)
    formset = Formset(request.POST or None, instance=question)
    if request.method == "POST":
        v1 = form.is_valid()
        if v1:
            formset.instance.kind = form.cleaned_data["kind"]
        v2 = formset.is_valid()
        if v1 and v2:
            uploaded_answer_image = (
                form.cleaned_data["answer_image"]
                if form.cleaned_data["kind"] == Question.DESC else None
            )
            answer_image = None
            if uploaded_answer_image:
                try:
                    answer_image = _create_question_image(question.bank, uploaded_answer_image)
                except (ImageStorageError, ValueError, OSError) as error:
                    form.add_error("answer_image", str(error))
                    v1 = False
            old_answer_image_id = question.answer_image_asset_id
        if v1 and v2:
            with transaction.atomic():
                q = form.save(commit=False)
                if answer_image:
                    q.answer_image_asset = answer_image
                elif q.kind == Question.DESC and form.cleaned_data["clear_answer_image"]:
                    q.answer_image_asset = None
                q.save()
                formset.instance = q
                if q.kind == Question.DESC:
                    q.options.all().delete()
                else:
                    formset.save()
                    ordered = []
                    for form in formset.forms:
                        if not form.cleaned_data:
                            continue
                        if form.cleaned_data.get("DELETE"):
                            continue
                        instance = form.instance
                        if instance.pk:
                            ordered.append(instance)
                    for index, option in enumerate(ordered):
                        if option.order != index:
                            option.order = index
                            option.save(update_fields=["order"])
                ids = [int(x) for x in request.POST.getlist("resolve") if x.isdigit()]
                if ids and not is_new:
                    Comment.objects.filter(question=q, pk__in=ids, resolved=False).update(
                        resolved=True, resolved_by=request.user, resolved_at=timezone.now())
            cleanup_warning = (
                _delete_unused_question_image(old_answer_image_id)
                if old_answer_image_id and old_answer_image_id != q.answer_image_asset_id else ""
            )
            if cleanup_warning:
                messages.warning(request, cleanup_warning)
            else:
                messages.success(request, "Question saved.")
            return redirect(safe_next(request, reverse("bank:manage_deck", args=[q.bank.slug])))
    comments = question.comments.select_related("user") if question.pk else []
    return render(request, "bank/question_edit.html", {
        "form": form, "formset": formset, "question": question, "is_new": is_new,
        "comments": comments, "next": request.GET.get("next", ""),
        "answer_image": question.answer_image_asset if question.answer_image_asset_id else None,
        "image_uploads_enabled": image_uploads_enabled(),
        "local_image_uploads": settings.DEBUG,
    })


@perm_required("bank.change_question")
def question_edit(request, pk):
    q = get_object_or_404(Question.objects.select_related("bank__subject"), pk=pk)
    if q.image_asset_id:
        return _image_question_form(request, q.bank, q.image_asset, question=q)
    return _edit_question(request, q)


@perm_required("bank.add_question")
def question_add(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    return _edit_question(request, Question(bank=bank), is_new=True)


def _image_question_form(request, bank, image, question=None):
    if image is None or image.bank_id != bank.pk:
        return HttpResponseForbidden("This image is not available in the selected chapter.")
    if question is not None and question.image_asset_id != image.pk:
        return HttpResponseForbidden("This question does not use the selected image.")
    initial = {}
    if question is not None:
        initial = {
            "is_lab": question.is_lab,
            "answer_mode": (
                question.image_answer_mode
                if question.kind == Question.IMAGE
                else question.kind
            ),
            "stem": question.stem,
            "answer": question.answer,
            "answer_image": "",
            "explanation": question.explanation,
            "note": question.note,
            "label": question.label,
            "book_chapter": question.book_chapter_id,
            "targets_json": json.dumps([
                {"label": target.label, "answer": target.answer,
                 "x_percent": float(target.x_percent) if target.x_percent is not None else None,
                 "y_percent": float(target.y_percent) if target.y_percent is not None else None}
                for target in question.image_targets.all()
            ]),
            "options_json": json.dumps([
                {"text": option.text, "is_correct": option.is_correct}
                for option in question.options.all()
            ]),
        }
    requested_mode = request.GET.get(
        "kind",
        initial.get("answer_mode", Question.IMAGE_IDENTIFY),
    )
    if requested_mode not in {
        Question.IMAGE_IDENTIFY, Question.MCQ, Question.TF, Question.DESC,
    }:
        requested_mode = Question.IMAGE_IDENTIFY
    form = ImageQuestionForm(request.POST or None, request.FILES or None, initial=initial or {
        "is_lab": request.GET.get("is_lab") == "1",
        "label": request.GET.get("label", ""),
        "book_chapter": request.GET.get("book_chapter") or None,
        "answer_mode": requested_mode,
        "answer": "",
        "targets_json": "[]",
        "options_json": "[]",
    }, has_answer_image=bool(question and question.answer_image_asset_id))
    destination = request.POST.get(
        "destination",
        request.GET.get(
            "destination",
            "identify" if (question and question.kind == Question.IMAGE)
            or requested_mode == Question.IMAGE_IDENTIFY else "regular",
        ),
    )
    if destination not in {"identify", "regular"}:
        destination = "regular"
    if request.method == "POST" and form.is_valid():
        answer_mode = form.cleaned_data["answer_mode"]
        store_in_identify = destination == "identify" or answer_mode == Question.IMAGE_IDENTIFY
        stored_kind = Question.IMAGE if store_in_identify else answer_mode
        stored_image_mode = answer_mode if store_in_identify else Question.IMAGE_IDENTIFY
        uploaded_answer_image = (
            form.cleaned_data["answer_image"]
            if answer_mode == Question.DESC else None
        )
        answer_image = None
        if uploaded_answer_image:
            try:
                answer_image = _create_question_image(bank, uploaded_answer_image)
            except (ImageStorageError, ValueError, OSError) as error:
                form.add_error("answer_image", str(error))
        if form.errors:
            return render(request, "bank/image_question_form.html", {
                "bank": bank, "image": image, "question": question,
                "form": form, "is_new": question is None,
                "initial_targets": form["targets_json"].value() or "[]",
                "initial_options": form["options_json"].value() or "[]",
                "next_url": request.POST.get("next") or request.GET.get("next", ""),
                "destination": destination,
                "cancel_url": safe_next(request, bank.get_absolute_url()),
                "answer_image": question.answer_image_asset if question and question.answer_image_asset_id else None,
                "image_uploads_enabled": image_uploads_enabled(),
                "local_image_uploads": settings.DEBUG,
            })
        old_answer_image_id = question.answer_image_asset_id if question else None
        with transaction.atomic():
            if question is None:
                question = Question.objects.create(
                    bank=bank,
                    kind=stored_kind,
                    number=(
                        str(bank.questions.filter(kind=Question.IMAGE).count() + 1)
                        if stored_kind == Question.IMAGE else ""
                    ),
                    image_asset=image,
                )
            question.kind = stored_kind
            question.stem = form.cleaned_data["stem"]
            question.is_lab = form.cleaned_data["is_lab"]
            question.image_answer_mode = stored_image_mode
            question.answer = (
                form.cleaned_data["answer"].strip()
                if answer_mode == Question.DESC else ""
            )
            if answer_image:
                question.answer_image_asset = answer_image
            elif answer_mode == Question.DESC and form.cleaned_data["clear_answer_image"]:
                question.answer_image_asset = None
            question.explanation = form.cleaned_data["explanation"].strip()
            question.note = form.cleaned_data["note"].strip()
            question.book_chapter = form.cleaned_data["book_chapter"]
            question.label = label_for_book(form.cleaned_data["label"], question.book_chapter)
            question.save()
            question.options.all().delete()
            question.image_targets.all().delete()
            if answer_mode in {Question.MCQ, Question.TF}:
                Option.objects.bulk_create([
                    Option(
                        question=question, text=option["text"].strip(),
                        is_correct=option["is_correct"], order=index,
                    )
                    for index, option in enumerate(form.cleaned_data["options"])
                ])
            elif answer_mode == Question.IMAGE_IDENTIFY:
                ImageTarget.objects.bulk_create([
                    ImageTarget(
                        question=question, label=target["label"].strip(),
                        answer=target["answer"].strip(),
                        x_percent=target.get("x_percent") or None,
                        y_percent=target.get("y_percent") or None,
                        order=index,
                    )
                    for index, target in enumerate(form.cleaned_data["targets"])
                ])
        cleanup_warning = (
            _delete_unused_question_image(old_answer_image_id)
            if old_answer_image_id and old_answer_image_id != question.answer_image_asset_id else ""
        )
        if cleanup_warning:
            messages.warning(request, cleanup_warning)
        if request.POST.get("add_another") == "1":
            messages.success(request, "Question saved. Add another separate question using the same image.")
            next_url = request.POST.get("next") or request.GET.get("next", "")
            add_url = reverse("bank:image_question_add", args=[bank.slug, image.pk])
            if form.cleaned_data["answer_mode"] != Question.IMAGE_IDENTIFY:
                add_url += f"?kind={form.cleaned_data['answer_mode']}"
            if destination == "identify":
                add_url += ("&" if "?" in add_url else "?") + "destination=identify"
            if form.cleaned_data["is_lab"]:
                add_url += ("&" if "?" in add_url else "?") + "is_lab=1"
            if question.book_chapter_id:
                add_url += ("&" if "?" in add_url else "?") + f"book_chapter={question.book_chapter_id}"
            if form.cleaned_data["label"]:
                add_url += ("&" if "?" in add_url else "?") + urlencode({"label": form.cleaned_data["label"]})
            if next_url:
                add_url += ("&" if "?" in add_url else "?") + urlencode({"next": next_url})
            return redirect(add_url)
        if question.is_lab:
            success_message = "Image question saved to Labs."
        elif question.kind == Question.IMAGE and question.image_answer_mode == Question.MCQ:
            success_message = "Image MCQ saved to Identify."
        elif question.kind == Question.IMAGE and question.image_answer_mode == Question.TF:
            success_message = "Image True / False question saved to Identify."
        elif question.kind == Question.IMAGE and question.image_answer_mode == Question.DESC:
            success_message = "Image descriptive question saved to Identify."
        elif question.kind == Question.IMAGE:
            success_message = "Image identification question saved to Identify."
        elif question.kind == Question.MCQ:
            success_message = "Image MCQ saved to MCQs & True / False."
        elif question.kind == Question.TF:
            success_message = "Image True / False question saved to MCQs & True / False."
        else:
            success_message = "Image descriptive question saved to Descriptive."
        messages.success(request, success_message)
        return redirect(safe_next(request, bank.get_absolute_url()))
    return render(request, "bank/image_question_form.html", {
        "bank": bank, "image": image, "question": question,
        "form": form, "is_new": question is None,
        "initial_targets": form["targets_json"].value() or "[]",
        "initial_options": form["options_json"].value() or "[]",
        "next_url": request.POST.get("next") or request.GET.get("next", ""),
        "destination": destination,
        "cancel_url": safe_next(request, bank.get_absolute_url()),
        "answer_image": question.answer_image_asset if question and question.answer_image_asset_id else None,
        "image_uploads_enabled": image_uploads_enabled(),
        "local_image_uploads": settings.DEBUG,
    })


def _create_question_image(bank, uploaded):
    result = upload_question_image(uploaded)
    return QuestionImage.objects.create(
        bank=bank,
        public_id=result["public_id"],
        secure_url=result["secure_url"],
        original_name=uploaded.name[:255],
        width=result["width"],
        height=result["height"],
    )


@perm_required("bank.add_question")
def image_question_upload(request, slug):
    bank = get_object_or_404(QuestionBank, slug=slug)
    form = ImageUploadForm(request.POST or None, request.FILES or None)
    requested_mode = request.POST.get("kind", request.GET.get("kind", Question.IMAGE_IDENTIFY))
    destination = request.POST.get(
        "destination",
        request.GET.get(
            "destination",
            "identify" if requested_mode == Question.IMAGE_IDENTIFY else "regular",
        ),
    )
    if destination not in {"identify", "regular"}:
        destination = "regular"
    requested_lab = request.POST.get("is_lab", request.GET.get("is_lab", "")) == "1"
    if requested_mode not in {Question.IMAGE_IDENTIFY, Question.MCQ, Question.TF, Question.DESC}:
        requested_mode = Question.IMAGE_IDENTIFY
    if request.method == "POST" and form.is_valid():
        uploaded = form.cleaned_data["image"]
        try:
            image = _create_question_image(bank, uploaded)
        except (ImageStorageError, ValueError, OSError) as error:
            form.add_error("image", str(error))
        else:
            add_url = reverse("bank:image_question_add", args=[bank.slug, image.pk])
            answer_mode = requested_mode
            if answer_mode in {Question.MCQ, Question.TF, Question.DESC}:
                add_url += f"?kind={answer_mode}"
            if destination == "identify":
                add_url += ("&" if "?" in add_url else "?") + "destination=identify"
            if requested_lab:
                add_url += ("&" if "?" in add_url else "?") + "is_lab=1"
            next_url = request.POST.get("next") or request.GET.get("next", "")
            if next_url:
                add_url += ("&" if "?" in add_url else "?") + urlencode({"next": next_url})
            return redirect(add_url)
    return render(request, "bank/image_upload.html", {
        "bank": bank,
        "form": form,
        "image_uploads_enabled": image_uploads_enabled(),
        "local_image_uploads": settings.DEBUG,
        "answer_mode": requested_mode,
        "is_lab": requested_lab,
        "destination": destination,
        "next_url": request.GET.get("next", ""),
    })


@perm_required("bank.add_question")
def image_question_add(request, slug, image_id):
    bank = get_object_or_404(QuestionBank, slug=slug)
    image = get_object_or_404(QuestionImage, pk=image_id, bank=bank)
    return _image_question_form(request, bank, image)


def _delete_unused_question_image(image_id):
    if image_id is None:
        return ""
    image = QuestionImage.objects.filter(pk=image_id).first()
    if image is None or image.questions.exists() or image.answer_questions.exists():
        return ""
    try:
        delete_question_image(image.public_id)
    except (ImageStorageError, ValueError, OSError) as error:
        return f"The unused image could not be removed from storage: {error}"
    image.delete()
    return ""


@perm_required("bank.delete_question")
@require_POST
def question_delete(request, pk):
    q = get_object_or_404(Question, pk=pk)
    slug = q.bank.slug
    image_ids = {q.image_asset_id, q.answer_image_asset_id}
    q.delete()
    cleanup_warning = "; ".join(
        warning for image_id in image_ids
        if (warning := _delete_unused_question_image(image_id))
    )
    if cleanup_warning:
        messages.warning(request, cleanup_warning)
    else:
        messages.success(request, "Question deleted.")
    return redirect("bank:manage_deck", slug=slug)


@perm_required("bank.change_question")
def comments_inbox(request):
    show = request.GET.get("show", "open")
    qs = Comment.objects.select_related("user", "question__bank__subject")
    qs = qs.filter(resolved=(show == "resolved"))
    return render(request, "bank/comments.html", {
        "comments": qs, "show": show,
        "n_open": Comment.objects.filter(resolved=False).count(),
        "n_resolved": Comment.objects.filter(resolved=True).count(),
    })


@perm_required("bank.change_question")
@require_POST
def comment_toggle(request, pk):
    c = get_object_or_404(Comment, pk=pk)
    c.resolved = not c.resolved
    c.resolved_by = request.user if c.resolved else None
    c.resolved_at = timezone.now() if c.resolved else None
    c.save()
    return redirect(safe_next(request, reverse("bank:comments")))


# ---------------------------------------------------------------- image processing
MAX_PDF_BYTES = 80 * 1024 * 1024


@perm_required("bank.add_question")
def image_processing(request):
    """Turn a chapter PDF into a zip of its figures, arrows and labels included."""
    form = {"pages": "", "margin": "normal", "mode": "figures"}
    error = ""
    if request.method == "POST":
        form = {key: request.POST.get(key, default).strip() for key, default in form.items()}
        upload = request.FILES.get("pdf")
        if not upload:
            error = "Choose a PDF file first."
        elif upload.size > MAX_PDF_BYTES:
            error = "This PDF is larger than 80 MB. Split it into chapters first (iLovePDF → Split PDF)."
        elif upload.read(5) != b"%PDF-":
            error = "This file is not a PDF."
        else:
            upload.seek(0)
            source = upload.temporary_file_path() if hasattr(upload, "temporary_file_path") else upload.read()
            try:
                data, name, _count, _stopped = figures.extract(
                    source, pages=form["pages"], margin=form["margin"], whole_pages=form["mode"] == "pages")
            except figures.FigureError as exc:
                error = str(exc)
            else:
                response = HttpResponse(data, content_type="application/zip")
                response["Content-Disposition"] = f'attachment; filename="{name}"'
                return response
    return render(request, "bank/image_processing.html", {
        "form": form, "error": error, "margins": figures.MARGINS,
    }, status=400 if error else 200)


# ---------------------------------------------------------------- bulk import
@perm_required("bank.add_question")
def import_questions(request):
    subjects = Subject.objects.all()
    ctx = {
        "subjects": subjects, "semesters": [s for s, _ in SEMESTER_CHOICES], "prompt": parser.AI_PROMPT,
        "text": "", "subject_id": request.GET.get("subject", ""), "semester": request.GET.get("semester", ""),
        "section_id": request.GET.get("section", ""),
        "sections": SubjectSection.objects.select_related("subject").all(),
        "deck_title": request.GET.get("deck_title", ""), "skip_dupes": True,
        "import_to_labs": request.GET.get("import_to_labs") == "1",
        "decks_json": [{"s": d.subject_id, "m": d.semester, "c": d.section_id, "t": d.title}
                       for d in QuestionBank.objects.filter(subject__isnull=False)],
        "book_chapters": BookChapter.objects.select_related("book"),
        "book_chapter_id": request.GET.get("book_chapter", ""),
    }
    if request.method != "POST":
        return render(request, "bank/import.html", ctx)

    text = request.POST.get("text", "")
    upload = request.FILES.get("file")
    if upload:
        text = upload.read().decode("utf-8-sig", errors="replace")
    result = parser.parse(text)
    skip_dupes = bool(request.POST.get("skip_dupes"))
    import_to_labs = request.POST.get("import_to_labs") == "1"

    # Form fields win; header lines (SUBJECT/SEMESTER/DECK) in the text fill any blanks.
    subject = None
    if request.POST.get("subject"):
        subject = Subject.objects.filter(pk=request.POST["subject"]).first()
    elif result.subject:
        subject = Subject.objects.filter(name__iexact=result.subject).first()
    section_error = ""
    section = None
    section_value = request.POST.get("section", "").strip()
    if section_value:
        if subject:
            section = SubjectSection.objects.filter(pk=section_value, subject=subject).first()
        if not section:
            section_error = "Choose a sub-subject belonging to the selected subject."
    elif result.section and subject:
        section = SubjectSection.objects.filter(subject=subject, name__iexact=result.section).first()
        if not section:
            section_error = f"No sub-subject named “{result.section}” exists under {subject.name}."
    try:
        semester = int(request.POST.get("semester") or result.semester or 0) or None
    except ValueError:
        semester = None
    title = request.POST.get("deck_title", "").strip() or result.bank_title

    deck = services.find_deck(subject, semester, title) if (subject and semester and title) else None
    if deck and section and deck.section_id not in (None, section.pk):
        section_error = "This deck already belongs to a different sub-subject."
    book_chapter_id = request.POST.get("book_chapter", "").strip()
    book_chapter = (
        BookChapter.objects.select_related("book").filter(pk=book_chapter_id).first()
        if book_chapter_id.isdigit() else None
    )
    if book_chapter:
        # Library questions are separate from the subject chapters, so only the book chapter counts.
        parser.mark_duplicates(result.questions, services.existing_keys(book_chapter=book_chapter, is_lab=import_to_labs))
    elif deck:
        parser.mark_duplicates(result.questions, services.existing_keys(deck, is_lab=import_to_labs))
    if book_chapter_id and not book_chapter:
        section_error = section_error or "That Library chapter no longer exists. Choose it again."

    if request.POST.get("action") == "commit":
        missing = [n for n, v in (("subject", subject), ("semester", semester), ("deck title", title)) if not v]
        if section_error:
            messages.error(request, section_error)
        elif missing:
            messages.error(request, "Please choose the " + ", ".join(missing) + ".")
        else:
            picked = {
                int(name[6:]): upload for name, upload in request.FILES.items()
                if name.startswith("image_") and name[6:].isdigit()
            }
            assigned, problems = services.match_import_images(
                result.questions, request.FILES.getlist("images"), picked,
            )
            if assigned and not image_uploads_enabled():
                problems.insert(0, "Image uploads are not configured on this server.")
            for index, upload in assigned.items():
                try:
                    validate_image_file(upload)
                except forms.ValidationError as error:
                    problems.append(f"{upload.name}: {' '.join(error.messages)}")
            if problems:
                for problem in problems:
                    messages.error(request, problem)
                if request.FILES:
                    messages.error(request, "Nothing was imported. Choose the images again after fixing this.")
            else:
                uploaded = []
                try:
                    # Questions are only saved once every image is stored; on failure the
                    # database rolls back and the already-stored files are removed.
                    with transaction.atomic():
                        deck = services.get_or_create_deck(subject, semester, title, section)
                        images = {}
                        for index, upload in sorted(assigned.items()):
                            images[index] = _create_question_image(deck, upload)
                            uploaded.append(images[index].public_id)
                        created, bad, dup = services.save_questions(
                            deck, result.questions, skip_dupes, is_lab=import_to_labs, images=images,
                            book_chapter=book_chapter,
                        )
                except Exception as error:
                    for public_id in uploaded:
                        try:
                            delete_question_image(public_id)
                        except (ImageStorageError, ValueError, OSError):
                            pass
                    if not isinstance(error, (ImageStorageError, ValueError, OSError)):
                        raise
                    messages.error(request, f"Nothing was imported: {error}")
                else:
                    destination = "Labs in " if import_to_labs else ""
                    with_images = f" with {len(images)} image(s)" if images else ""
                    in_book = f" and the Library chapter “{book_chapter}”" if book_chapter else ""
                    messages.success(
                        request, f"Imported {created} question(s){with_images} into {destination}“{deck.path}”"
                                 f"{in_book}. Skipped {bad} with errors and {dup} duplicate(s).")
                    # Back to an empty import page, so the next chapter can be pasted straight away.
                    return redirect("bank:import")

    rows = []
    for q in result.questions:
        if q.kind == parser.DESC:
            ans = q.answer_raw
        elif q.kind == parser.TF:
            ans = "".join("T" if c else "F" for c in q.correct)
        else:
            ans = ", ".join(chr(65 + i) for i, c in enumerate(q.correct) if c)
        rows.append({
            "q": q, "answer": ans, "skipped": bool(q.duplicate_of) and skip_dupes,
            "image_key": services.image_key(q.image) if q.image else services.image_key(q.number) if q.number else "",
        })
    ctx.update({
        "text": text, "subject_id": str(subject.pk) if subject else "",
        "section_id": str(section.pk) if section else section_value,
        "selected_section": section,
        "section_error": section_error,
        "book_chapter_id": str(book_chapter.pk) if book_chapter else book_chapter_id,
        "semester": str(semester or ""), "deck_title": title, "skip_dupes": skip_dupes,
        "deck_exists": deck, "rows": rows, "previewed": True,
        "import_to_labs": import_to_labs,
        "importable": sum(1 for r in rows if r["q"].ok and not r["skipped"]),
        "n_errors": sum(1 for r in rows if not r["q"].ok),
        "n_dupes": sum(1 for r in rows if r["q"].duplicate_of),
        "stray": result.stray,
        "image_uploads_enabled": image_uploads_enabled(),
    })
    return render(request, "bank/import.html", ctx)


# ---------------------------------------------------------------- accounts
class SingleSessionLoginView(LoginView):
    template_name = "bank/login.html"

    def form_valid(self, form):
        user = form.get_user()
        cache_key = f"bank:active-session:{user.pk}"
        old_session_key = cache.get(cache_key)

        # Remove the previous browser session before creating the new one.
        # The old browser will therefore be logged out as soon as it makes
        # another request, while the new login becomes the only active session.
        if old_session_key and old_session_key != self.request.session.session_key:
            Session.objects.filter(session_key=old_session_key).delete()

        response = super().form_valid(form)
        if self.request.session.session_key:
            cache.set(cache_key, self.request.session.session_key, None)
        return response


def logout_view(request):
    if request.user.is_authenticated:
        cache.delete(f"bank:active-session:{request.user.pk}")
    logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)
