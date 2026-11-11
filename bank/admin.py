from django.contrib import admin

from .forms import OptionFormSet, QuestionForm
from .models import Block, Book, BookChapter, Comment, FriendRequest, Option, Profile, Question, QuestionBank, Subject, SubjectSection


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


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("handle", "display_name", "user", "friend_code", "university", "year", "has_picture")
    search_fields = ("handle", "display_name", "user__username", "friend_code")
    list_filter = ("university", "year")
    readonly_fields = ("friend_code", "avatar_key", "avatar_url", "avatar_small_url", "created_at", "handle_changed_at")
    actions = ["remove_pictures"]

    @admin.display(boolean=True, description="Picture")
    def has_picture(self, obj):
        return bool(obj.avatar_key)

    @admin.action(description="Remove profile picture")
    def remove_pictures(self, request, queryset):
        from .profiles import remove_avatar
        count = 0
        for profile in queryset.exclude(avatar_key=""):
            remove_avatar(profile)
            count += 1
        self.message_user(request, f"Removed {count} profile picture{'s' if count != 1 else ''}.")


@admin.register(FriendRequest)
class FriendRequestAdmin(admin.ModelAdmin):
    list_display = ("from_user", "to_user", "status", "withdrawn", "created_at")
    list_filter = ("status",)
    search_fields = ("from_user__username", "to_user__username")
    raw_id_fields = ("from_user", "to_user")


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = ("blocker", "blocked", "created_at")
    search_fields = ("blocker__username", "blocked__username")
    raw_id_fields = ("blocker", "blocked")
