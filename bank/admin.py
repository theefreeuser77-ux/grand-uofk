from django.contrib import admin

from .forms import OptionFormSet, QuestionForm
from .models import Book, BookChapter, Comment, Option, Question, QuestionBank, Subject, SubjectSection


class OptionInline(admin.TabularInline):
    model = Option
    formset = OptionFormSet
    extra = 4
    fields = ("order", "text", "is_correct")


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("name", "order", "slug")
    list_editable = ("order",)
    prepopulated_fields = {"slug": ("name",)}


@admin.register(SubjectSection)
class SubjectSectionAdmin(admin.ModelAdmin):
    list_display = ("name", "subject", "order", "slug")
    list_editable = ("order",)
    list_filter = ("subject",)
    prepopulated_fields = {"slug": ("name",)}


@admin.register(QuestionBank)
class QuestionBankAdmin(admin.ModelAdmin):
    list_display = ("title", "subject", "section", "semester", "slug")
    list_editable = ("subject", "section", "semester")   # quick way to file decks
    list_filter = ("subject", "section", "semester")
    search_fields = ("title",)
    prepopulated_fields = {"slug": ("title",)}


class BookChapterInline(admin.TabularInline):
    model = BookChapter
    extra = 1
    fields = ("number", "title")


@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = ("title", "slug")
    search_fields = ("title",)
    prepopulated_fields = {"slug": ("title",)}
    inlines = [BookChapterInline]


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    form = QuestionForm
    list_display = ("__str__", "bank", "kind", "is_lab", "label")
    list_filter = ("bank__subject", "bank", "kind", "is_lab")
    search_fields = ("stem", "number")
    inlines = [OptionInline]
    fieldsets = (
        (None, {"fields": ("bank", "number", "label", "kind", "stem", "is_lab")}),
        ("Descriptive answer", {"fields": ("answer",)}),
        ("Feedback shown after answering", {"fields": ("explanation", "note")}),
    )

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if form.instance.kind == Question.DESC:
            form.instance.options.all().delete()


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ("__str__", "user", "created_at", "resolved")
    list_filter = ("resolved",)
    readonly_fields = ("question", "user", "text", "created_at", "resolved_at", "resolved_by")
