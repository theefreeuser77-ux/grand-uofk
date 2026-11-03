import json

from django import forms
from django.forms.models import BaseInlineFormSet, inlineformset_factory

from .models import BookChapter, ImageTarget, Option, Question, QuestionBank, QuestionImage


class ExamTemplateForm(forms.Form):
    name = forms.CharField(max_length=100, initial="My practice exam")
    question_count = forms.IntegerField(min_value=1, max_value=200, initial=20)
    mcq_percent = forms.IntegerField(min_value=0, max_value=100, initial=40, label="Multiple choice (%)")
    tf_percent = forms.IntegerField(min_value=0, max_value=100, initial=20, label="True / False (%)")
    descriptive_percent = forms.IntegerField(min_value=0, max_value=100, initial=20, label="Descriptive (%)")
    identify_percent = forms.IntegerField(min_value=0, max_value=100, initial=20, label="Identify (%)")
    time_limit_minutes = forms.IntegerField(
        min_value=1, max_value=300, required=False, label="Time limit (minutes)",
        help_text="Optional. Leave blank for an untimed exam.",
    )

    def __init__(
        self, *args, banks=None, chapter_initials=None, show_publish=False,
        publish_initial=False, **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.requires_chapters = banks is not None
        self.show_publish = show_publish
        chapter_initials = chapter_initials or {}
        self.chapter_rows = []
        static_names = (
            "name", "question_count", "mcq_percent", "tf_percent",
            "descriptive_percent", "identify_percent", "time_limit_minutes",
        )
        self.core_fields = [self[name] for name in static_names]
        if show_publish:
            self.fields["is_published"] = forms.BooleanField(
                required=False,
                initial=publish_initial,
                label="Publish this template for all users",
                help_text="Published templates appear in the shared templates section.",
            )
            self.publish_field = self["is_published"]
        else:
            self.publish_field = None
        for bank in banks or []:
            selected_name = f"chapter_{bank.pk}_selected"
            percentage_name = f"chapter_{bank.pk}_percentage"
            self.fields[selected_name] = forms.BooleanField(
                required=False, initial=bank.pk in chapter_initials,
            )
            self.fields[percentage_name] = forms.IntegerField(
                min_value=1, max_value=100, required=False,
                widget=forms.NumberInput(attrs={"min": 1, "max": 100}),
                initial=chapter_initials.get(bank.pk),
            )
            self.chapter_rows.append({
                "bank": bank,
                "selected": self[selected_name],
                "percentage": self[percentage_name],
            })

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Enter a name for this exam setup.")
        return name

    def clean(self):
        cleaned = super().clean()
        fields = ("mcq_percent", "tf_percent", "descriptive_percent", "identify_percent")
        if all(field in cleaned for field in fields) and sum(cleaned[field] for field in fields) != 100:
            raise forms.ValidationError("Question format percentages must add up to 100%.")
        if self.requires_chapters:
            selected = []
            selected_count = 0
            for row in self.chapter_rows:
                bank = row["bank"]
                if cleaned.get(f"chapter_{bank.pk}_selected"):
                    selected_count += 1
                    percentage = cleaned.get(f"chapter_{bank.pk}_percentage")
                    if percentage is None:
                        self.add_error(
                            f"chapter_{bank.pk}_percentage",
                            "Enter this chapter's percentage.",
                        )
                    else:
                        selected.append((bank, percentage))
            if not selected_count:
                raise forms.ValidationError("Select at least one chapter for this exam.")
            if len(selected) == selected_count and sum(percentage for _, percentage in selected) != 100:
                raise forms.ValidationError("Selected chapter percentages must add up to 100%.")
        return cleaned

    def mix(self):
        return {
            "mcq": self.cleaned_data["mcq_percent"],
            "tf": self.cleaned_data["tf_percent"],
            "descriptive": self.cleaned_data["descriptive_percent"],
            "identify": self.cleaned_data["identify_percent"],
        }

    def chapter_weights(self):
        weights = []
        for row in self.chapter_rows:
            bank = row["bank"]
            if self.cleaned_data.get(f"chapter_{bank.pk}_selected"):
                weights.append((bank, self.cleaned_data[f"chapter_{bank.pk}_percentage"]))
        return weights


def validate_image_file(uploaded):
    if uploaded.size > 20 * 1024 * 1024:
        raise forms.ValidationError("Image must be 20 MB or smaller.")
    from PIL import Image, UnidentifiedImageError

    try:
        uploaded.seek(0)
        image = Image.open(uploaded)
        if image.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
            raise forms.ValidationError("Use a JPEG, PNG, WebP, or GIF image.")
        if image.width * image.height > 80_000_000:
            raise forms.ValidationError("Image dimensions are too large to process.")
        image.verify()
    except (UnidentifiedImageError, OSError) as error:
        raise forms.ValidationError("The uploaded file is not a valid supported image.") from error
    finally:
        uploaded.seek(0)
    return uploaded


def book_chapter_field():
    field = forms.ModelChoiceField(
        queryset=BookChapter.objects.select_related("book"), required=False,
        label="Library book chapter", empty_label="Not from a Library book",
        help_text="Optional: also list this question in a chapter of a Library book.",
    )
    field.label_from_instance = str
    return field


def label_for_book(label, chapter):
    """Questions from a Library book carry its name as their label, unless they already have one."""
    return label or (chapter.book.title if chapter else "")


class QuestionForm(forms.ModelForm):
    answer_image = forms.FileField(
        required=False,
        help_text="Optional image shown with the answer to descriptive questions (up to 20 MB).",
        widget=forms.ClearableFileInput(attrs={
            "accept": "image/jpeg,image/png,image/webp,image/gif",
        }),
    )
    clear_answer_image = forms.BooleanField(required=False, label="Remove current answer image")

    class Meta:
        model = Question
        fields = ["bank", "number", "label", "book_chapter", "kind", "stem", "answer", "explanation", "note"]
        widgets = {
            "stem": forms.Textarea(attrs={"rows": 3}),
            "answer": forms.Textarea(attrs={"rows": 3}),
            "explanation": forms.Textarea(attrs={"rows": 3}),
            "note": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if self.instance.kind != Question.IMAGE:
            self.fields["kind"].choices = [
                choice for choice in self.fields["kind"].choices
                if choice[0] != Question.IMAGE
            ]
        f = self.fields["bank"]
        f.queryset = QuestionBank.objects.select_related("subject", "section").order_by(
            "subject__order", "semester", "title")
        f.label_from_instance = lambda d: d.path
        f.label = "Deck (subject › semester › title)"
        self.fields["book_chapter"] = book_chapter_field()

    def clean(self):
        cleaned = super().clean()
        cleaned["label"] = label_for_book(cleaned.get("label", ""), cleaned.get("book_chapter"))
        if (
            cleaned.get("kind") == Question.DESC
            and not (cleaned.get("answer") or "").strip()
            and not cleaned.get("answer_image")
            and (
                not self.instance.answer_image_asset_id
                or cleaned.get("clear_answer_image")
            )
        ):
            self.add_error("answer", "Enter the answer shown on the descriptive flashcard.")
        return cleaned

    def clean_answer_image(self):
        uploaded = self.cleaned_data.get("answer_image")
        return validate_image_file(uploaded) if uploaded else uploaded


class ImageUploadForm(forms.Form):
    image = forms.FileField(
        help_text="Upload a clear image, scan, diagram, or table screenshot (up to 20 MB).",
        widget=forms.ClearableFileInput(attrs={
            "accept": "image/jpeg,image/png,image/webp,image/gif",
        }),
    )

    def clean_image(self):
        uploaded = self.cleaned_data["image"]
        return validate_image_file(uploaded)


class ImageQuestionForm(forms.Form):
    def __init__(self, *args, has_answer_image=False, **kwargs):
        self.has_answer_image = has_answer_image
        super().__init__(*args, **kwargs)
        self.fields["book_chapter"] = book_chapter_field()

    is_lab = forms.BooleanField(
        label="Import this question to Labs",
        required=False,
        help_text="Labs can contain MCQs, True / False, descriptive, and image questions.",
    )
    answer_mode = forms.ChoiceField(choices=(
        (Question.IMAGE_IDENTIFY, "Identify labelled structures"),
        (Question.MCQ, "MCQ"),
        (Question.TF, "True / False"),
        (Question.DESC, "Descriptive"),
    ))
    stem = forms.CharField(label="Question / instructions", widget=forms.Textarea(attrs={"rows": 3}))
    answer = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    answer_image = forms.FileField(
        required=False,
        help_text="Optional image shown with this descriptive answer (up to 20 MB).",
        widget=forms.ClearableFileInput(attrs={
            "accept": "image/jpeg,image/png,image/webp,image/gif",
        }),
    )
    clear_answer_image = forms.BooleanField(required=False, label="Remove current answer image")
    explanation = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    targets_json = forms.CharField(widget=forms.HiddenInput(), initial="[]")
    options_json = forms.CharField(widget=forms.HiddenInput(), initial="[]")

    def clean_stem(self):
        stem = self.cleaned_data["stem"].strip()
        if not stem:
            raise forms.ValidationError("Enter the question or identification instructions.")
        return stem

    def _json_list(self, name):
        try:
            value = json.loads(self.cleaned_data[name])
        except (TypeError, ValueError) as error:
            raise forms.ValidationError("Question entries could not be read; please check them and try again.") from error
        if not isinstance(value, list):
            raise forms.ValidationError("Question entries must be a list.")
        return value

    def clean(self):
        cleaned = super().clean()
        if "answer_mode" not in cleaned:
            return cleaned
        mode = cleaned["answer_mode"]
        if mode == Question.IMAGE_IDENTIFY:
            targets = self._json_list("targets_json") if "targets_json" in cleaned else []
            if not 1 <= len(targets) <= 50:
                self.add_error("targets_json", "Add between 1 and 50 identification targets.")
                return cleaned
            seen_labels = set()
            for target in targets:
                if not isinstance(target, dict):
                    self.add_error("targets_json", "Each target must have a label and answer.")
                    break
                label = target.get("label")
                answer = target.get("answer")
                if not isinstance(label, str) or not label.strip():
                    self.add_error("targets_json", "Every target needs a label, such as A or 1.")
                    break
                if not isinstance(answer, str) or not answer.strip():
                    self.add_error("targets_json", f"Target {label.strip()} needs an answer.")
                    break
                if label.strip().casefold() in seen_labels:
                    self.add_error("targets_json", f"Target label {label.strip()} is repeated.")
                    break
                seen_labels.add(label.strip().casefold())
                x, y = target.get("x_percent"), target.get("y_percent")
                if (x in (None, "")) != (y in (None, "")):
                    self.add_error("targets_json", f"Target {label.strip()} needs both marker coordinates or neither.")
                    break
                if x not in (None, "") and not self._valid_coordinates(x, y):
                    self.add_error("targets_json", f"Target {label.strip()} marker must be within the image.")
                    break
            cleaned["targets"] = targets
        elif mode in {Question.MCQ, Question.TF}:
            options = self._json_list("options_json") if "options_json" in cleaned else []
            minimum = 1 if mode == Question.TF else 2
            if not minimum <= len(options) <= 8:
                self.add_error("options_json", f"Add between {minimum} and 8 answer choices.")
                return cleaned
            if any(
                not isinstance(option, dict)
                or not isinstance(option.get("text"), str)
                or not option["text"].strip()
                or not isinstance(option.get("is_correct"), bool)
                for option in options
            ):
                self.add_error("options_json", "Every answer choice needs text and a correct/incorrect selection.")
                return cleaned
            if mode == Question.MCQ and not any(option["is_correct"] for option in options):
                self.add_error("options_json", "Mark at least one correct answer choice.")
            cleaned["options"] = options
        elif (
            not cleaned.get("answer", "").strip()
            and not cleaned.get("answer_image")
            and (
                not self.has_answer_image
                or cleaned.get("clear_answer_image")
            )
        ):
            self.add_error("answer", "Enter the answer shown on the descriptive flashcard.")
        return cleaned

    def clean_answer_image(self):
        uploaded = self.cleaned_data.get("answer_image")
        return validate_image_file(uploaded) if uploaded else uploaded

    @staticmethod
    def _valid_coordinates(x, y):
        try:
            x_value, y_value = float(x), float(y)
        except (TypeError, ValueError):
            return False
        return 0 <= x_value <= 100 and 0 <= y_value <= 100


class OptionFormSet(BaseInlineFormSet):
    """Shared by the editor page and the Django admin."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        kind = self.data.get("kind", self.instance.kind)
        if kind == Question.DESC:
            return
        opts = [f.cleaned_data for f in self.forms
                if f.cleaned_data and not f.cleaned_data.get("DELETE")]
        is_tf = kind == Question.TF
        need = 1 if is_tf else 2
        if len(opts) < need:
            raise forms.ValidationError(f"Add at least {need} option(s).")
        if len(opts) > 8:
            raise forms.ValidationError("At most 8 options.")
        if not is_tf and not any(o.get("is_correct") for o in opts):
            raise forms.ValidationError("Tick at least one correct option.")


def option_formset(extra=2):
    return inlineformset_factory(
        Question, Option, formset=OptionFormSet, fields=("text", "is_correct"),
        extra=extra, can_delete=True,
        widgets={"text": forms.Textarea(attrs={"rows": 2})},
    )
