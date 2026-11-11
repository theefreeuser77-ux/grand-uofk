import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.text import slugify

SEMESTER_CHOICES = [(i, f"Semester {i}") for i in range(1, 13)]


def _unique_slug(model, base, instance):
    base = slugify(base)[:200] or "item"
    slug, i = base, 2
    while model.objects.filter(slug=slug).exclude(pk=instance.pk).exists():
        slug, i = f"{base}-{i}", i + 1
    return slug


class Subject(models.Model):
    """Top-level classification: one big block on the home page."""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    description = models.CharField(max_length=200, blank=True)
    order = models.PositiveSmallIntegerField(default=0, help_text="Lower numbers appear first.")

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(Subject, self.name, self)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("bank:subject_detail", args=[self.slug])


class SubjectSection(models.Model):
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=120, blank=True)
    order = models.PositiveSmallIntegerField(default=0, help_text="Lower numbers appear first.")

    class Meta:
        ordering = ["order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["subject", "name"], name="uniq_section_name_per_subject"),
            models.UniqueConstraint(fields=["subject", "slug"], name="uniq_section_slug_per_subject"),
        ]

    def __str__(self):
        return f"{self.subject.name} › {self.name}"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:120] or "section"
            self.slug = base
            suffix = 2
            while SubjectSection.objects.filter(subject=self.subject, slug=self.slug).exclude(pk=self.pk).exists():
                self.slug = f"{base}-{suffix}"
                suffix += 1
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("bank:section_detail", args=[self.subject.slug, self.slug])


class QuestionBank(models.Model):
    """A deck = one lesson (e.g. 'Haematology') inside a subject and semester."""
    subject = models.ForeignKey(Subject, null=True, on_delete=models.PROTECT, related_name="decks")
    section = models.ForeignKey(
        SubjectSection, null=True, blank=True, on_delete=models.SET_NULL, related_name="decks",
        verbose_name="sub-subject",
    )
    semester = models.PositiveSmallIntegerField(choices=SEMESTER_CHOICES, default=1)
    title = models.CharField("deck title", max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "deck"
        ordering = ["subject__order", "semester", "title"]
        constraints = [
            models.UniqueConstraint(fields=["subject", "semester", "title"], name="uniq_deck_per_semester"),
        ]

    def __str__(self):
        return f"{self.title} (S{self.semester})"

    def clean(self):
        super().clean()
        if self.section_id and self.subject_id and self.section.subject_id != self.subject_id:
            raise ValidationError({"section": "Choose a sub-subject belonging to the selected subject."})

    @property
    def path(self):
        s = self.subject.name if self.subject_id else "Unfiled"
        if self.section_id:
            s = f"{s} › {self.section.name}"
        return f"{s} › Semester {self.semester} › {self.title}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(QuestionBank, self.title, self)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("bank:bank_detail", args=[self.slug])


class Book(models.Model):
    """A textbook in the Library (e.g. "Gray's Review"), split into its own chapters.

    Its questions still live in subject decks; the book is a second way in, and its
    title is the label those questions carry.
    """
    title = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(Book, self.title, self)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("bank:book_detail", args=[self.slug])

    @property
    def cover_tone(self):
        """Which of the Library's cover colours this book wears; fixed per book."""
        return (self.pk or 0) % 6


class BookChapter(models.Model):
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="chapters")
    number = models.PositiveSmallIntegerField(default=1)
    title = models.CharField(max_length=200)

    class Meta:
        ordering = ["book__title", "number", "id"]
        constraints = [models.UniqueConstraint(fields=["book", "number"], name="uniq_book_chapter_number")]

    def __str__(self):
        return f"{self.book.title} › {self.number}. {self.title}"


class Question(models.Model):
    MCQ, TF, DESC, IMAGE = "mcq", "tf", "desc", "image"
    IMAGE_IDENTIFY, IMAGE_MCQ = "identify", "mcq"
    KIND_CHOICES = [
        (MCQ, "Multiple choice (pick one; any option marked correct is accepted)"),
        (TF, "True / False (each option is a statement)"),
        (DESC, "Descriptive question (flashcard)"),
        (IMAGE, "Image-dependent question"),
    ]
    IMAGE_ANSWER_CHOICES = [
        (IMAGE_IDENTIFY, "Identify image targets"),
        (IMAGE_MCQ, "Multiple-choice question"),
        (TF, "True / False question"),
        (DESC, "Descriptive question"),
    ]

    bank = models.ForeignKey(QuestionBank, on_delete=models.CASCADE, related_name="questions",
                             verbose_name="deck")
    number = models.CharField("source number", max_length=20, blank=True,
                              help_text="Label from the original paper, e.g. 17. Optional.")
    label = models.CharField(max_length=100, blank=True)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=MCQ)
    stem = models.TextField("question")
    answer = models.TextField(blank=True)
    explanation = models.TextField(blank=True)
    note = models.TextField(blank=True, help_text="Warning shown to students (ambiguous / flawed source).")
    is_lab = models.BooleanField(
        "include in Labs",
        default=False,
        help_text="Keep this question in the mixed-format Labs tab instead of the standard question-type tabs.",
    )
    image_asset = models.ForeignKey(
        "QuestionImage", null=True, blank=True, on_delete=models.PROTECT, related_name="questions",
    )
    answer_image_asset = models.ForeignKey(
        "QuestionImage", null=True, blank=True, on_delete=models.PROTECT,
        related_name="answer_questions",
    )
    image_answer_mode = models.CharField(
        max_length=8, choices=IMAGE_ANSWER_CHOICES, default=IMAGE_IDENTIFY,
    )
    book_chapter = models.ForeignKey(
        BookChapter, null=True, blank=True, on_delete=models.SET_NULL, related_name="questions",
        help_text="Optional: also list this question in a Library book chapter.",
    )
    is_in_sorting_bin = models.BooleanField(default=False)
    sorting_bin_at = models.DateTimeField(null=True, blank=True)
    is_in_review_bin = models.BooleanField(default=False)
    review_bin_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["bank_id", "id"]

    def __str__(self):
        return f"{self.number or self.pk}. {self.stem[:70]}"


class QuestionImage(models.Model):
    bank = models.ForeignKey(QuestionBank, on_delete=models.PROTECT, related_name="images")
    public_id = models.CharField(max_length=255, unique=True)
    secure_url = models.URLField(max_length=2048)
    original_name = models.CharField(max_length=255)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return self.original_name


class Option(models.Model):
    """MCQ: a possible answer.  TF: a statement, is_correct = 'the statement is true'."""
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="options")
    text = models.TextField()
    is_correct = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.text[:60]


class ImageTarget(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="image_targets")
    label = models.CharField(max_length=40)
    answer = models.TextField()
    x_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    y_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.label}: {self.answer[:50]}"


class SavedQuestion(models.Model):
    """A question a user bookmarked; shown under 'Saved' in the same subject/semester/deck layout."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_questions")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="saved_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "question"], name="uniq_saved_question")]
        ordering = ["-created_at"]


class QuestionNote(models.Model):
    """A user's private study note on a question, shown behind a toggle whenever they meet it again."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="question_notes")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="user_notes")
    text = models.TextField(max_length=5000)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "question"], name="uniq_question_note")]


class ExamTemplate(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="exam_templates")
    bank = models.ForeignKey(
        QuestionBank, null=True, blank=True, on_delete=models.CASCADE, related_name="exam_templates",
    )
    subject = models.ForeignKey(
        Subject, null=True, blank=True, on_delete=models.CASCADE, related_name="exam_templates",
    )
    name = models.CharField(max_length=100)
    question_count = models.PositiveSmallIntegerField()
    mix = models.JSONField(default=dict)
    time_limit_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    # Which questions it uses, by label: a filter key from bank/sources.py ("all", "uofk", "other"…).
    source = models.CharField(max_length=20, default="all")
    is_published = models.BooleanField(default=False)
    snapshot = models.JSONField(null=True, blank=True)
    source_attempt = models.OneToOneField(
        "ExamAttempt", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="saved_template",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "bank", "name"], name="uniq_exam_template_user_bank_name"),
            models.UniqueConstraint(
                fields=["user", "subject", "name"],
                condition=models.Q(subject__isnull=False),
                name="uniq_exam_template_user_subject_name",
            ),
        ]

    def __str__(self):
        scope = self.bank.title if self.bank_id else self.subject.name
        return f"{self.name} · {scope}"


class ExamTemplateChapter(models.Model):
    template = models.ForeignKey(ExamTemplate, on_delete=models.CASCADE, related_name="chapters")
    bank = models.ForeignKey(QuestionBank, on_delete=models.CASCADE, related_name="exam_template_chapters")
    percentage = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["bank__semester", "bank__title", "bank_id"]
        constraints = [
            models.UniqueConstraint(fields=["template", "bank"], name="uniq_exam_template_chapter"),
            models.CheckConstraint(
                condition=models.Q(percentage__gte=1, percentage__lte=100),
                name="exam_template_chapter_percentage_range",
            ),
        ]


class ExamAttempt(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="exam_attempts")
    bank = models.ForeignKey(QuestionBank, on_delete=models.CASCADE, related_name="exam_attempts")
    subject = models.ForeignKey(
        Subject, null=True, blank=True, on_delete=models.CASCADE, related_name="exam_attempts",
    )
    shared_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="shared_attempts",
    )
    template = models.ForeignKey(
        ExamTemplate, null=True, blank=True, on_delete=models.SET_NULL, related_name="attempts",
    )
    title = models.CharField(max_length=120)
    configuration = models.JSONField(default=dict)
    question_ids = models.JSONField(default=list)
    responses = models.JSONField(default=dict)
    share_token = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self):
        return f"{self.title} · {self.user}"


class AIRequestQuota(models.Model):
    name = models.CharField(max_length=40, primary_key=True)
    request_timestamps = models.JSONField(default=list)


class TopicSearch(models.Model):
    """An AI's answer to "which questions of this chapter are about <topic>?", kept so the
    next student asking the same thing gets it instantly without another AI request."""
    bank = models.ForeignKey(QuestionBank, on_delete=models.CASCADE, related_name="topic_searches")
    label = models.CharField(max_length=20)          # question source filter used (e.g. "all", "uofk")
    topic = models.CharField(max_length=80)          # normalised: lower case, single spaces
    signature = models.CharField(max_length=40)      # fingerprint of the questions searched
    question_ids = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["bank", "label", "topic"])]


class AIExamAssessment(models.Model):
    QUEUED, PROCESSING, COMPLETED, FAILED, CANCELLED = (
        "queued", "processing", "completed", "failed", "cancelled",
    )
    STATUS_CHOICES = [
        (QUEUED, "Queued"),
        (PROCESSING, "Processing"),
        (COMPLETED, "Completed"),
        (FAILED, "Failed"),
        (CANCELLED, "Cancelled"),
    ]

    attempt = models.OneToOneField(
        ExamAttempt, on_delete=models.CASCADE, related_name="ai_assessment",
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=QUEUED)
    error_message = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "pk"]


class BackupSnapshot(models.Model):
    """A restore point: the whole database (gzip JSON) saved automatically before a backup replaced it.

    These rows are never wiped by a restore and are not part of backups themselves.
    """
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=200)
    data = models.BinaryField()
    size = models.PositiveIntegerField(default=0)
    counts = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"Restore point {self.created_at:%Y-%m-%d %H:%M} ({self.reason})"

    @property
    def question_count(self):
        return self.counts.get("bank.Question", 0)

    @property
    def user_count(self):
        return self.counts.get("auth.User", 0)


class Comment(models.Model):
    """Private message from a user to the admins about a question."""
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                             related_name="question_comments")
    text = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["resolved", "-created_at"]

    def __str__(self):
        return f"Comment on Q{self.question_id}: {self.text[:40]}"


class PushPhrase(models.Model):
    """A short message admins write; students who allow notifications get one at random."""
    text = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.text


class PushSettings(models.Model):
    """Site-wide notification settings (a single row)."""
    enabled = models.BooleanField(default=True)
    interval_hours = models.DecimalField(max_digits=5, decimal_places=1, default=24)
    title = models.CharField(max_length=60, default="Rawasi")

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]


class PushPreference(models.Model):
    """One student's notifications: on or off, and when their next one is due."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_preference")
    enabled = models.BooleanField(default=False)
    next_send_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_phrase = models.ForeignKey(PushPhrase, null=True, blank=True, on_delete=models.SET_NULL)


class PushSubscription(models.Model):
    """A browser or phone a student allowed notifications on."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.URLField(max_length=1000, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)


class Profile(models.Model):
    """A student's public face: handle, friend code, picture and privacy choices."""
    VISIBLE_PRIVATE, VISIBLE_FRIENDS, VISIBLE_EVERYONE = "private", "friends", "everyone"
    STATS_CHOICES = [
        (VISIBLE_PRIVATE, "Only me"),
        (VISIBLE_FRIENDS, "My friends"),
        (VISIBLE_EVERYONE, "Everyone"),
    ]
    REQUESTS_EVERYONE, REQUESTS_UNIVERSITY, REQUESTS_NOBODY = "everyone", "university", "nobody"
    REQUESTS_CHOICES = [
        (REQUESTS_EVERYONE, "Everyone"),
        (REQUESTS_UNIVERSITY, "People at my university"),
        (REQUESTS_NOBODY, "Nobody"),
    ]
    YEAR_CHOICES = [(i, f"Year {i}") for i in range(1, 7)]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    handle = models.CharField(max_length=20, unique=True)
    handle_changed_at = models.DateTimeField(null=True, blank=True)
    friend_code = models.CharField(max_length=6, unique=True)
    display_name = models.CharField(max_length=60, blank=True)
    university = models.CharField(max_length=20, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True, choices=YEAR_CHOICES)
    # "supabase:avatars/<user>/<id>" or "local:<user>/<id>"; the 256px and 64px files sit next to it.
    avatar_key = models.CharField(max_length=200, blank=True)
    avatar_url = models.URLField(max_length=500, blank=True)
    avatar_small_url = models.URLField(max_length=500, blank=True)
    stats_visibility = models.CharField(max_length=10, choices=STATS_CHOICES, default=VISIBLE_PRIVATE)
    who_can_request = models.CharField(max_length=10, choices=REQUESTS_CHOICES, default=REQUESTS_EVERYONE)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"@{self.handle}"

    @property
    def name(self):
        return self.display_name or self.user.get_username()

    @property
    def initials(self):
        parts = self.name.split()
        letters = (parts[0][:1] + (parts[1][:1] if len(parts) > 1 else "")) if parts else "?"
        return letters.upper()

    @property
    def university_name(self):
        from .sources import UNIVERSITIES
        if self.university == "other":
            return "Other university"
        return next((name for key, name, _ in UNIVERSITIES if key == self.university), "")

    @property
    def avatar_color(self):
        """A steady colour per person for the initials circle."""
        return f"avatar-c{self.user_id % 6}"

    def get_absolute_url(self):
        return reverse("bank:profile", args=[self.handle])


class FriendRequest(models.Model):
    """One person asking another to be friends. A decline is never shown to the sender."""
    PENDING, DECLINED = "pending", "declined"
    STATUS_CHOICES = [(PENDING, "Pending"), (DECLINED, "Declined")]

    from_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="friend_requests_sent")
    to_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="friend_requests_received")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    # The sender took back a request that had (unknown to them) been declined.
    withdrawn = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["from_user", "to_user"], name="unique_friend_request"),
            models.CheckConstraint(condition=~models.Q(from_user=models.F("to_user")), name="no_self_friend_request"),
        ]
        indexes = [models.Index(fields=["to_user", "status"])]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.from_user} → {self.to_user} ({self.status})"


class Friendship(models.Model):
    """Stored both ways (A→B and B→A), so 'my friends' is one simple query."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="friendships")
    friend = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "friend"], name="unique_friendship"),
            models.CheckConstraint(condition=~models.Q(user=models.F("friend")), name="no_self_friendship"),
        ]

    def __str__(self):
        return f"{self.user} ↔ {self.friend}"


class Block(models.Model):
    blocker = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="blocks_made")
    blocked = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["blocker", "blocked"], name="unique_block"),
            models.CheckConstraint(condition=~models.Q(blocker=models.F("blocked")), name="no_self_block"),
        ]
        ordering = ["-created_at"]
