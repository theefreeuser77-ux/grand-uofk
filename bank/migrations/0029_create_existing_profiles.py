import re
import secrets

from django.conf import settings
from django.db import migrations

CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
RESERVED = {
    "admin", "administrator", "me", "code", "settings", "staff", "support", "help",
    "rawasi", "team", "official", "moderator", "mod", "root", "system", "null", "none",
}


def _handle(username, taken):
    base = re.sub(r"[^a-z0-9_]", "", (username or "").lower().replace(" ", "_").replace(".", "_"))
    base = base.lstrip("0123456789_")[:16]
    if len(base) < 3:
        base = f"student{base}"[:16]
    if base in RESERVED:
        base = f"{base}_1"
    handle, n = base, 2
    while handle in taken:
        handle, n = f"{base}{n}"[:20], n + 1
    return handle


def create_profiles(apps, schema_editor):
    """Give every existing account a handle (from its username) and a friend code."""
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Profile = apps.get_model("bank", "Profile")
    handles = set(Profile.objects.values_list("handle", flat=True))
    codes = set(Profile.objects.values_list("friend_code", flat=True))
    new = []
    for user in User.objects.filter(profile__isnull=True).order_by("pk"):
        handle = _handle(user.username, handles)
        handles.add(handle)
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        while code in codes:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        codes.add(code)
        new.append(Profile(user=user, handle=handle, friend_code=code))
    Profile.objects.bulk_create(new, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("bank", "0028_profile"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(create_profiles, migrations.RunPython.noop),
    ]
