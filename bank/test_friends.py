from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import friends, profiles
from .models import Block, FriendRequest, Friendship, Profile


class FriendRulesTests(TestCase):
    def setUp(self):
        self.a = User.objects.create_user("amna")
        self.b = User.objects.create_user("bakri")
        self.c = User.objects.create_user("caroline")

    def test_new_users_get_a_profile(self):
        self.assertTrue(Profile.objects.filter(user=self.a).exists())

    def test_send_and_accept(self):
        self.assertEqual(friends.send_request(self.a, self.b), friends.SENT)
        self.assertEqual(friends.relationship(self.a, self.b), friends.SENT)
        self.assertEqual(friends.relationship(self.b, self.a), friends.RECEIVED)
        friends.accept(self.b, FriendRequest.objects.get())
        self.assertTrue(friends.are_friends(self.a, self.b))
        self.assertTrue(friends.are_friends(self.b, self.a))
        self.assertFalse(FriendRequest.objects.exists())
        self.assertEqual(friends.send_request(self.a, self.b), friends.FRIENDS)

    def test_asking_each_other_makes_friends(self):
        friends.send_request(self.a, self.b)
        self.assertEqual(friends.send_request(self.b, self.a), friends.FRIENDS)
        self.assertEqual(Friendship.objects.count(), 2)

    def test_decline_is_silent_and_doesnt_renotify(self):
        friends.send_request(self.a, self.b)
        friends.decline(self.b, FriendRequest.objects.get())
        self.assertEqual(friends.relationship(self.a, self.b), friends.SENT)
        self.assertEqual(friends.relationship(self.b, self.a), friends.NONE)
        self.assertEqual(friends.incoming_count(self.b), 0)

        friends.cancel(self.a, self.b)
        self.assertEqual(friends.relationship(self.a, self.b), friends.NONE)
        with patch.object(friends, "notify") as notify:
            self.assertEqual(friends.send_request(self.a, self.b), friends.SENT)
            notify.assert_not_called()
        self.assertEqual(friends.incoming_count(self.b), 0)

        FriendRequest.objects.update(responded_at=timezone.now() - timedelta(days=31), withdrawn=True)
        with patch.object(friends, "notify") as notify:
            friends.send_request(self.a, self.b)
            notify.assert_called_once()
        self.assertEqual(friends.incoming_count(self.b), 1)

    def test_cancel_pending(self):
        friends.send_request(self.a, self.b)
        friends.cancel(self.a, self.b)
        self.assertFalse(FriendRequest.objects.exists())

    def test_block_clears_everything_and_stops_requests(self):
        friends.send_request(self.a, self.b)
        friends.accept(self.b, FriendRequest.objects.get())
        friends.send_request(self.c, self.a)
        friends.block(self.a, self.b)
        self.assertFalse(friends.are_friends(self.a, self.b))
        with self.assertRaises(friends.FriendError):
            friends.send_request(self.b, self.a)
        with self.assertRaises(friends.FriendError):
            friends.send_request(self.a, self.b)
        self.assertEqual(friends.relationship(self.a, self.b), friends.BLOCKED_BY_ME)
        friends.unblock(self.a, self.b)
        self.assertEqual(friends.send_request(self.b, self.a), friends.SENT)

    def test_who_can_request(self):
        Profile.objects.filter(user=self.b).update(who_can_request=Profile.REQUESTS_NOBODY)
        self.b.refresh_from_db()
        with self.assertRaisesMessage(friends.FriendError, "isn't accepting"):
            friends.send_request(self.a, self.b)

        Profile.objects.filter(user=self.b).update(who_can_request=Profile.REQUESTS_UNIVERSITY, university="gezira")
        self.b = User.objects.get(pk=self.b.pk)
        with self.assertRaisesMessage(friends.FriendError, "their university"):
            friends.send_request(self.a, self.b)
        Profile.objects.filter(user=self.a).update(university="gezira")
        self.a = User.objects.get(pk=self.a.pk)
        self.assertEqual(friends.send_request(self.a, self.b), friends.SENT)

    def test_daily_limit(self):
        others = [User.objects.create_user(f"student{i}") for i in range(friends.DAILY_REQUEST_LIMIT)]
        for other in others:
            friends.send_request(self.a, other)
        with self.assertRaisesMessage(friends.FriendError, "Try again tomorrow"):
            friends.send_request(self.a, self.b)

    def test_cannot_accept_someone_elses_request(self):
        friends.send_request(self.a, self.b)
        with self.assertRaises(friends.FriendError):
            friends.accept(self.c, FriendRequest.objects.get())


class FriendViewTests(TestCase):
    def setUp(self):
        self.a = User.objects.create_user("amna", password="pw-12345678")
        self.b = User.objects.create_user("bakri", password="pw-12345678")
        self.pa, self.pb = self.a.profile, self.b.profile
        self.client.force_login(self.a)

    def act(self, profile, action, **data):
        return self.client.post(reverse("bank:friend_action", args=[profile.handle, action]), data)

    def test_full_flow_through_pages(self):
        response = self.client.post(reverse("bank:friends"), {"code": self.pb.friend_code.lower()})
        self.assertRedirects(response, self.pb.get_absolute_url())
        self.assertContains(self.client.get(self.pb.get_absolute_url()), "Add friend")

        self.assertRedirects(self.act(self.pb, "add"), self.pb.get_absolute_url())
        self.assertContains(self.client.get(self.pb.get_absolute_url()), "Request sent")

        self.client.force_login(self.b)
        page = self.client.get(reverse("bank:friends"))
        self.assertContains(page, "Waiting for you")
        self.assertContains(page, '<span class="badge"')
        self.act(self.pa, "accept", next=reverse("bank:friends"))
        page = self.client.get(reverse("bank:friends"))
        self.assertContains(page, "@amna")
        self.assertNotContains(page, '<span class="badge"')
        self.assertContains(self.client.get(self.pa.get_absolute_url()), "Friends")

        self.act(self.pa, "remove")
        self.assertFalse(friends.are_friends(self.a, self.b))

    def test_unknown_code_and_own_code(self):
        response = self.client.post(reverse("bank:friends"), {"code": "ZZZZZZ"}, follow=True)
        self.assertContains(response, "No one has that friend code")
        response = self.client.post(reverse("bank:friends"), {"code": self.pa.friend_code}, follow=True)
        self.assertContains(response, "your own code")

    def test_blocked_person_cannot_see_or_find_blocker(self):
        self.act(self.pb, "block")
        self.assertTrue(Block.objects.filter(blocker=self.a, blocked=self.b).exists())
        self.assertContains(self.client.get(reverse("bank:friends")), "Unblock")

        self.client.force_login(self.b)
        self.assertEqual(self.client.get(self.pa.get_absolute_url()).status_code, 404)
        response = self.client.post(reverse("bank:friends"), {"code": self.pa.friend_code}, follow=True)
        self.assertContains(response, "No one has that friend code")
        response = self.act(self.pa, "add", next="/")
        self.assertFalse(FriendRequest.objects.exists())

    def test_own_profile_has_qr_and_actions_need_post(self):
        page = self.client.get(self.pa.get_absolute_url())
        self.assertContains(page, "<svg")
        self.assertEqual(self.client.get(
            reverse("bank:friend_action", args=[self.pb.handle, "add"])).status_code, 405)
        self.assertEqual(self.act(self.pb, "explode").status_code, 404)
        self.assertEqual(self.act(self.pa, "add").status_code, 404)
