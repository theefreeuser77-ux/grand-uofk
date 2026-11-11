from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import Comment, ExamAttempt, Question

SITE_NAME = "Rawasi 101"


def site(request):
    ctx = {"SITE_NAME": SITE_NAME}
    u = getattr(request, "user", None)
    if u is not None and u.is_authenticated:
        from .profiles import profile_for
        ctx["my_profile"] = profile_for(u)
        from .friends import incoming_count
        ctx["friend_requests_count"] = incoming_count(u)
        active_exam = ExamAttempt.objects.filter(
            user=u,
            submitted_at__isnull=True,
        ).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
        ).order_by("-started_at", "-pk").first()
        if active_exam:
            ctx["active_exam_resume"] = {
                "id": active_exam.pk,
                "title": active_exam.title,
                "url": reverse("bank:exam_attempt", args=[active_exam.pk]),
                "started_at": active_exam.started_at.isoformat(),
            }
    if u is not None and u.is_authenticated and u.has_perm("bank.change_question"):
        ctx["open_comments"] = Comment.objects.filter(resolved=False).count()
    if u is not None and u.is_authenticated and u.is_staff:
        ctx["sorting_bin_count"] = Question.objects.filter(is_in_sorting_bin=True).count()
        ctx["review_bin_count"] = Question.objects.filter(is_in_review_bin=True).count()
    return ctx
