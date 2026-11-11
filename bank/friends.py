"""Friends: requests, friendships and blocks.

Rules worth knowing:
- A decline is silent. The sender keeps seeing "Request sent", and asking again within
  RESEND_AFTER of a decline doesn't notify the other person again.
- If two people ask each other, the second request makes them friends straight away.
- Blocking removes any friendship and requests between the two, and the blocked person
  can no longer open the blocker's profile or send them requests.
"""
import logging
import threading
from datetime import timedelta

from django.db import IntegrityError, close_old_connections, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from . import push
from .models import Block, FriendRequest, Friendship, Profile

log = logging.getLogger(__name__)

DAILY_REQUEST_LIMIT = 20
RESEND_AFTER = timedelta(days=30)

# Relationship states, from the viewer's side.
SELF, FRIENDS, SENT, RECEIVED, NONE, BLOCKED_BY_ME = "self", "friends", "sent", "received", "none", "blocked"


class FriendError(Exception):
    """Something the person should be told, e.g. 'This person isn't accepting requests.'"""


# ---------------------------------------------------------------- reading
def is_blocked_either_way(a, b):
    return Block.objects.filter(Q(blocker=a, blocked=b) | Q(blocker=b, blocked=a)).exists()


def has_blocked(blocker, blocked):
    return Block.objects.filter(blocker=blocker, blocked=blocked).exists()


def are_friends(a, b):
    return Friendship.objects.filter(user=a, friend=b).exists()


def relationship(viewer, other):
    if viewer.pk == other.pk:
        return SELF
    if has_blocked(viewer, other):
        return BLOCKED_BY_ME
    if are_friends(viewer, other):
        return FRIENDS
    if FriendRequest.objects.filter(from_user=other, to_user=viewer, status=FriendRequest.PENDING).exists():
        return RECEIVED
    if FriendRequest.objects.filter(from_user=viewer, to_user=other, withdrawn=False).exists():
        return SENT
    return NONE


def friends_of(user):
    return (Profile.objects.select_related("user")
            .filter(user__in=Friendship.objects.filter(user=user).values("friend"), user__is_active=True)
            .order_by("display_name", "handle"))


def incoming(user):
    return (FriendRequest.objects.select_related("from_user__profile")
            .filter(to_user=user, status=FriendRequest.PENDING, from_user__is_active=True))


def outgoing(user):
    return (FriendRequest.objects.select_related("to_user__profile")
            .filter(from_user=user, withdrawn=False, to_user__is_active=True))


def incoming_count(user):
    return FriendRequest.objects.filter(to_user=user, status=FriendRequest.PENDING).count()


def blocked_by(user):
    return Block.objects.select_related("blocked__profile").filter(blocker=user)


def request_problem(sender, receiver):
    """Why `sender` can't send `receiver` a request right now, or None."""
    if sender.pk == receiver.pk:
        return "That's you."
    if not receiver.is_active or is_blocked_either_way(sender, receiver):
        return "You can't send this person a friend request."
    their = getattr(receiver, "profile", None)
    rule = their.who_can_request if their else Profile.REQUESTS_EVERYONE
    if rule == Profile.REQUESTS_NOBODY:
        return "This person isn't accepting friend requests."
    if rule == Profile.REQUESTS_UNIVERSITY:
        mine = getattr(sender, "profile", None)
        same = mine and their.university and their.university != "other" and mine.university == their.university
        if not same:
            return "This person only accepts friend requests from their university."
    return None


# ---------------------------------------------------------------- changing
def send_request(sender, receiver):
    """Ask `receiver` to be friends. Returns the new relationship state."""
    if are_friends(sender, receiver):
        return FRIENDS
    problem = request_problem(sender, receiver)
    if problem:
        raise FriendError(problem)

    # They already asked us: accept instead.
    theirs = FriendRequest.objects.filter(from_user=receiver, to_user=sender, status=FriendRequest.PENDING).first()
    if theirs:
        accept(sender, theirs)
        return FRIENDS

    now = timezone.now()
    existing = FriendRequest.objects.filter(from_user=sender, to_user=receiver).first()
    if existing and not existing.withdrawn:
        return SENT
    if existing and existing.status == FriendRequest.DECLINED and existing.responded_at and now - existing.responded_at < RESEND_AFTER:
        existing.withdrawn = False                      # still declined; they aren't bothered again
        existing.save(update_fields=["withdrawn"])
        return SENT

    sent_today = FriendRequest.objects.filter(from_user=sender, created_at__gte=now - timedelta(days=1)).count()
    if sent_today >= DAILY_REQUEST_LIMIT:
        raise FriendError("You've sent a lot of friend requests today. Try again tomorrow.")

    try:
        with transaction.atomic():
            if existing:
                existing.delete()
            FriendRequest.objects.create(from_user=sender, to_user=receiver)
    except IntegrityError:          # a double tap: the first one already went through
        return SENT
    name = _name(sender)
    notify(receiver, f"{name} sent you a friend request.", reverse("bank:friends"))
    return SENT


@transaction.atomic
def accept(user, friend_request):
    if friend_request.to_user_id != user.pk:
        raise FriendError("That request isn't yours.")
    sender = friend_request.from_user
    if is_blocked_either_way(user, sender):
        friend_request.delete()
        raise FriendError("You can't add this person.")
    _make_friends(user, sender)
    FriendRequest.objects.filter(
        Q(from_user=user, to_user=sender) | Q(from_user=sender, to_user=user)).delete()
    notify(sender, f"{_name(user)} accepted your friend request.", Profile.objects.get(user=user).get_absolute_url())


def decline(user, friend_request):
    if friend_request.to_user_id != user.pk:
        raise FriendError("That request isn't yours.")
    friend_request.status = FriendRequest.DECLINED
    friend_request.responded_at = timezone.now()
    friend_request.save(update_fields=["status", "responded_at"])


def cancel(sender, receiver):
    request = FriendRequest.objects.filter(from_user=sender, to_user=receiver).first()
    if not request:
        return
    if request.status == FriendRequest.PENDING:
        request.delete()
    else:
        request.withdrawn = True
        request.save(update_fields=["withdrawn"])


def remove_friend(user, other):
    Friendship.objects.filter(Q(user=user, friend=other) | Q(user=other, friend=user)).delete()


@transaction.atomic
def block(blocker, blocked):
    if blocker.pk == blocked.pk:
        raise FriendError("You can't block yourself.")
    Block.objects.get_or_create(blocker=blocker, blocked=blocked)
    remove_friend(blocker, blocked)
    FriendRequest.objects.filter(
        Q(from_user=blocker, to_user=blocked) | Q(from_user=blocked, to_user=blocker)).delete()


def unblock(blocker, blocked):
    Block.objects.filter(blocker=blocker, blocked=blocked).delete()


def _make_friends(a, b):
    for user, friend in ((a, b), (b, a)):
        Friendship.objects.get_or_create(user=user, friend=friend)


def _name(user):
    profile = getattr(user, "profile", None)
    return profile.name if profile else user.get_username()


# ---------------------------------------------------------------- notifications
def notify(user, text, url):
    """Send a phone notification in the background, so the page doesn't wait on it."""
    if not push.configured():
        return
    user_id = user.pk

    def run():
        close_old_connections()
        try:
            from django.contrib.auth import get_user_model
            target = get_user_model().objects.filter(pk=user_id).first()
            if target:
                push.send_to_user(target, text, url=url)
        except Exception:
            log.warning("Friend notification to user %s failed", user_id, exc_info=True)
        finally:
            close_old_connections()

    transaction.on_commit(lambda: threading.Thread(target=run, daemon=True).start())
