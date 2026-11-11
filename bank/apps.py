from django.apps import AppConfig


class BankConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bank"
    verbose_name = "Question bank"

    def ready(self):
        from django.conf import settings
        from django.db.models.signals import post_save

        def make_profile(sender, instance, created, raw=False, **kwargs):
            if created and not raw:
                from .profiles import profile_for
                profile_for(instance)

        post_save.connect(make_profile, sender=settings.AUTH_USER_MODEL, dispatch_uid="bank_make_profile")
