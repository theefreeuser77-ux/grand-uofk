"""Profiles: handles, friend codes and profile pictures."""
import io
import logging
import re
import secrets
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError

from . import image_storage
from .models import Profile

log = logging.getLogger(__name__)

HANDLE_RE = re.compile(r"^[a-z][a-z0-9_]{2,19}$")
HANDLE_COOLDOWN = timedelta(days=30)
RESERVED_HANDLES = {
    "admin", "administrator", "me", "code", "settings", "staff", "support", "help",
    "rawasi", "team", "official", "moderator", "mod", "root", "system", "null", "none",
}
# No 0/O or 1/I/L, so a code read out loud or copied by hand can't be mistaken.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6

MAX_AVATAR_BYTES = 5 * 1024 * 1024
MAX_AVATAR_PIXELS = 40_000_000
AVATAR_SIZES = {"large": 256, "small": 64}


# ---------------------------------------------------------------- handles and codes
def normalize_handle(value):
    return (value or "").strip().lstrip("@").lower()


def handle_problem(handle, profile=None):
    """Why this handle can't be used, or None if it's fine."""
    if not HANDLE_RE.match(handle):
        return "Use 3–20 letters, numbers or _, starting with a letter."
    if handle in RESERVED_HANDLES:
        return "That handle is reserved."
    taken = Profile.objects.filter(handle=handle)
    if profile is not None:
        taken = taken.exclude(pk=profile.pk)
    if taken.exists():
        return "That handle is taken."
    return None


def next_handle_change(profile):
    """When this person may change their handle again, or None if they can now."""
    if profile.handle_changed_at is None:
        return None
    allowed = profile.handle_changed_at + HANDLE_COOLDOWN
    return allowed if allowed > timezone.now() else None


def _handle_from_username(username, taken):
    base = re.sub(r"[^a-z0-9_]", "", (username or "").lower().replace(" ", "_").replace(".", "_"))
    base = base.lstrip("0123456789_")[:16]
    if len(base) < 3:
        base = f"student{base}"[:16]
    if base in RESERVED_HANDLES:
        base = f"{base}_1"
    handle, n = base, 2
    while handle in taken:
        handle, n = f"{base}{n}"[:20], n + 1
    return handle


def new_friend_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_friend_code(value):
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def profile_for(user):
    """The user's profile, made on first use."""
    try:
        return user.profile
    except Profile.DoesNotExist:
        pass
    for _ in range(10):
        taken = set(Profile.objects.values_list("handle", flat=True))
        try:
            with transaction.atomic():
                return Profile.objects.create(
                    user=user,
                    handle=_handle_from_username(user.get_username(), taken),
                    friend_code=new_friend_code(),
                )
        except IntegrityError:
            existing = Profile.objects.filter(user=user).first()
            if existing:
                return existing
    raise RuntimeError("Could not create a profile.")


def find_by_friend_code(value):
    code = normalize_friend_code(value)
    if len(code) != CODE_LENGTH:
        return None
    return Profile.objects.select_related("user").filter(friend_code=code, user__is_active=True).first()


# ---------------------------------------------------------------- pictures
def _square(image, size):
    return ImageOps.fit(image, (size, size), method=Image.Resampling.LANCZOS, centering=(0.5, 0.4))


def process_avatar(uploaded_file):
    """Check an upload and return {"large": webp_bytes, "small": webp_bytes}.

    The picture is turned upright, cropped square and re-encoded, which also drops
    EXIF data such as the GPS position a phone camera writes into a photo.
    """
    if uploaded_file.size > MAX_AVATAR_BYTES:
        raise ValidationError("That picture is over 5 MB. Choose a smaller one.")
    try:
        with Image.open(uploaded_file) as probe:
            width, height = probe.size
            if width * height > MAX_AVATAR_PIXELS:
                raise ValidationError("That picture is too large. Choose a smaller one.")
            probe.verify()
        uploaded_file.seek(0)
        with Image.open(uploaded_file) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGBA") if image.mode in ("P", "LA", "RGBA") else image.convert("RGB")
            if image.mode == "RGBA":
                backing = Image.new("RGB", image.size, (255, 255, 255))
                backing.paste(image, mask=image.getchannel("A"))
                image = backing
            out = {}
            for name, size in AVATAR_SIZES.items():
                buffer = io.BytesIO()
                _square(image, size).save(buffer, "WEBP", quality=82, method=6)
                out[name] = buffer.getvalue()
            return out
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise ValidationError("That file isn't a picture we can read. Use a JPG, PNG or WebP.") from error


def set_avatar(profile, uploaded_file):
    files = process_avatar(uploaded_file)
    stored = image_storage.upload_avatar(profile.user_id, files)
    old_key = profile.avatar_key
    profile.avatar_key = stored["key"]
    profile.avatar_url = stored["large_url"]
    profile.avatar_small_url = stored["small_url"]
    profile.save(update_fields=["avatar_key", "avatar_url", "avatar_small_url"])
    _delete_quietly(old_key)


def remove_avatar(profile):
    old_key = profile.avatar_key
    profile.avatar_key = profile.avatar_url = profile.avatar_small_url = ""
    profile.save(update_fields=["avatar_key", "avatar_url", "avatar_small_url"])
    _delete_quietly(old_key)


def _delete_quietly(key):
    if not key:
        return
    try:
        image_storage.delete_avatar(key)
    except (image_storage.ImageStorageError, ValueError):
        log.warning("Could not delete old profile picture %s", key, exc_info=True)


# ---------------------------------------------------------------- what a viewer may see
def can_see_stats(profile, viewer):
    if viewer.pk == profile.user_id:
        return True
    if profile.stats_visibility == Profile.VISIBLE_EVERYONE:
        return True
    if profile.stats_visibility == Profile.VISIBLE_FRIENDS:
        from .friends import are_friends
        return are_friends(profile.user, viewer)
    return False


def profile_stats(profile):
    user = profile.user
    return {
        "exams_finished": user.exam_attempts.filter(submitted_at__isnull=False).count(),
        "saved": user.saved_questions.count(),
    }
