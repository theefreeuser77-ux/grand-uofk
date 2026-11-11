import io
import tempfile
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from . import profiles
from .models import Profile


def _photo(size=(800, 600), fmt="JPEG", exif=True, name="me.jpg"):
    image = Image.new("RGB", size, (200, 40, 40))
    buffer = io.BytesIO()
    kwargs = {}
    if exif:
        data = Image.Exif()
        data[0x010F] = "PhoneMaker"  # Make
        data[0x8825] = {1: "N", 2: (15.0, 30.0, 0.0)}  # GPS
        kwargs["exif"] = data.tobytes()
    image.save(buffer, fmt, **kwargs)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=f"image/{fmt.lower()}")


class HandleAndCodeTests(TestCase):
    def test_profile_made_from_username(self):
        user = User.objects.create_user("Sara.Ahmed", password="pw-12345678")
        profile = profiles.profile_for(user)
        self.assertEqual(profile.handle, "sara_ahmed")
        self.assertEqual(len(profile.friend_code), 6)
        self.assertTrue(set(profile.friend_code) <= set(profiles.CODE_ALPHABET))
        self.assertEqual(profiles.profile_for(user).pk, profile.pk)

    def test_clashing_and_odd_usernames_get_unique_valid_handles(self):
        a = profiles.profile_for(User.objects.create_user("sara_ahmed"))
        b = profiles.profile_for(User.objects.create_user("Sara Ahmed"))
        c = profiles.profile_for(User.objects.create_user("42"))
        d = profiles.profile_for(User.objects.create_user("admin"))
        self.assertEqual(a.handle, "sara_ahmed")
        self.assertEqual(b.handle, "sara_ahmed2")
        for p in (a, b, c, d):
            self.assertRegex(p.handle, profiles.HANDLE_RE)
            self.assertNotIn(p.handle, profiles.RESERVED_HANDLES)

    def test_handle_rules(self):
        self.assertIsNotNone(profiles.handle_problem("ab"))
        self.assertIsNotNone(profiles.handle_problem("9lives"))
        self.assertIsNotNone(profiles.handle_problem("has-dash"))
        self.assertIsNotNone(profiles.handle_problem("admin"))
        self.assertIsNone(profiles.handle_problem("good_name1"))

    def test_find_by_friend_code_ignores_case_and_spaces(self):
        profile = profiles.profile_for(User.objects.create_user("omar"))
        messy = " " + "-".join(profile.friend_code.lower()) + " "
        self.assertEqual(profiles.find_by_friend_code(messy), profile)
        self.assertIsNone(profiles.find_by_friend_code("ZZ"))


class AvatarProcessingTests(TestCase):
    def test_square_webp_without_exif(self):
        out = profiles.process_avatar(_photo())
        for name, size in profiles.AVATAR_SIZES.items():
            with Image.open(io.BytesIO(out[name])) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.size, (size, size))
                self.assertEqual(len(image.getexif()), 0)
        self.assertLess(len(out["large"]), 40_000)

    def test_png_with_transparency(self):
        image = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        out = profiles.process_avatar(SimpleUploadedFile("a.png", buffer.getvalue(), content_type="image/png"))
        self.assertIn("small", out)

    def test_rejects_non_images_and_big_files(self):
        with self.assertRaises(ValidationError):
            profiles.process_avatar(SimpleUploadedFile("x.jpg", b"not a picture", content_type="image/jpeg"))
        big = _photo()
        big.size = profiles.MAX_AVATAR_BYTES + 1
        with self.assertRaises(ValidationError):
            profiles.process_avatar(big)


@override_settings(DEBUG=True)
class ProfileViewTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        override = override_settings(MEDIA_ROOT=self.media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user("layla", password="pw-12345678")
        self.other = User.objects.create_user("hassan", password="pw-12345678")
        self.client.force_login(self.user)

    def _save(self, **changes):
        profile = profiles.profile_for(self.user)
        data = {"action": "profile", "display_name": profile.display_name, "handle": profile.handle,
                "university": profile.university, "year": profile.year or "",
                "stats_visibility": profile.stats_visibility, "who_can_request": profile.who_can_request}
        data.update(changes)
        return self.client.post(reverse("bank:settings"), data)

    def test_edit_profile_and_handle_cooldown(self):
        response = self._save(display_name="  Layla   M ", handle="@Layla_M", university="gezira", year=3)
        self.assertRedirects(response, reverse("bank:settings"))
        profile = Profile.objects.get(user=self.user)
        self.assertEqual((profile.display_name, profile.handle, profile.university, profile.year),
                         ("Layla M", "layla_m", "gezira", 3))
        self.assertIsNotNone(profile.handle_changed_at)

        response = self._save(handle="layla_new")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You changed your handle recently")
        self.assertEqual(Profile.objects.get(user=self.user).handle, "layla_m")

        Profile.objects.filter(user=self.user).update(handle_changed_at=timezone.now() - timedelta(days=31))
        self.assertRedirects(self._save(handle="layla_new"), reverse("bank:settings"))

    def test_taken_handle_and_bad_university(self):
        taken = profiles.profile_for(self.other).handle
        response = self._save(handle=taken, university="mars")
        self.assertContains(response, "That handle is taken.")
        self.assertContains(response, "Choose a university from the list.")

    def test_upload_replace_and_remove_picture(self):
        url = reverse("bank:settings")
        self.client.post(url, {"action": "avatar", "picture": _photo()})
        profile = Profile.objects.get(user=self.user)
        self.assertTrue(profile.avatar_key.startswith("local:"))
        first_key = profile.avatar_key
        first_file = profile.avatar_url.rsplit("/", 1)[1]

        self.client.post(url, {"action": "avatar", "picture": _photo(fmt="PNG", exif=False, name="b.png")})
        profile.refresh_from_db()
        self.assertNotEqual(profile.avatar_key, first_key)
        user_dir = f"{self.media.name}/avatars/{self.user.pk}"
        import os
        self.assertNotIn(first_file, os.listdir(user_dir))
        self.assertEqual(len(os.listdir(user_dir)), 2)

        self.client.post(url, {"action": "avatar_remove"})
        profile.refresh_from_db()
        self.assertEqual(profile.avatar_key, "")
        self.assertEqual(os.listdir(user_dir), [])

    def test_bad_upload_shows_error(self):
        response = self.client.post(reverse("bank:settings"), {
            "action": "avatar", "picture": SimpleUploadedFile("x.jpg", b"nope", content_type="image/jpeg")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Profile.objects.get(user=self.user).avatar_key, "")

    def test_profile_page_privacy(self):
        mine = profiles.profile_for(self.user)
        theirs = profiles.profile_for(self.other)

        response = self.client.get(reverse("bank:profile", args=[mine.handle]))
        self.assertContains(response, mine.friend_code)
        self.assertContains(response, "questions saved")

        response = self.client.get(reverse("bank:profile", args=[theirs.handle.upper()]))
        self.assertNotContains(response, theirs.friend_code)
        self.assertContains(response, "keeps their study stats private")

        theirs.stats_visibility = Profile.VISIBLE_FRIENDS
        theirs.save()
        self.assertContains(self.client.get(theirs.get_absolute_url()), "keeps their study stats private")

        theirs.stats_visibility = Profile.VISIBLE_EVERYONE
        theirs.save()
        self.assertContains(self.client.get(theirs.get_absolute_url()), "mock exams finished")

    def test_inactive_and_missing_profiles_404(self):
        theirs = profiles.profile_for(self.other)
        self.other.is_active = False
        self.other.save()
        self.assertEqual(self.client.get(theirs.get_absolute_url()).status_code, 404)
        self.assertEqual(self.client.get(reverse("bank:profile", args=["nobody_here"])).status_code, 404)

    def test_me_redirect_and_login_required(self):
        self.assertRedirects(self.client.get(reverse("bank:my_profile")),
                             profiles.profile_for(self.user).get_absolute_url())
        self.client.logout()
        response = self.client.get(reverse("bank:profile", args=["layla"]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)
