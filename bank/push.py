"""Phone and browser notifications with phrases admins write.

Students switch them on in Settings. Each student has their own schedule: the first one comes at a
random point within the admins' interval, and every later one about one interval after the last
(give or take 20%), so students are spread out instead of all being sent one at the same moment.

Sending runs in the background at most once a minute, started by ordinary page requests (see
PushTickMiddleware). An outside cron job can also call the tick URL so notifications still go out
when nobody is on the site.
"""
import json
import logging
import random
import threading
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections
from django.utils import timezone

from .models import PushPhrase, PushPreference, PushSettings, PushSubscription

logger = logging.getLogger(__name__)

BATCH = 200                 # students per tick at most
TICK_EVERY = 60             # seconds between background ticks
GONE = {404, 410}           # the browser dropped this subscription


def configured():
    return bool(getattr(settings, "VAPID_PUBLIC_KEY", "") and getattr(settings, "VAPID_PRIVATE_KEY", ""))


def interval(push_settings=None):
    push_settings = push_settings or PushSettings.load()
    return timedelta(hours=max(0.5, float(push_settings.interval_hours)))


def first_time(now=None, push_settings=None):
    """A random moment within one interval, so students who switch on together are spread out."""
    return (now or timezone.now()) + interval(push_settings) * random.uniform(0.02, 1)


def next_time(now=None, push_settings=None):
    return (now or timezone.now()) + interval(push_settings) * random.uniform(0.8, 1.2)


def reschedule_everyone(push_settings):
    """After the admins change the interval, spread every student over the new one."""
    now = timezone.now()
    prefs = list(PushPreference.objects.filter(enabled=True))
    for pref in prefs:
        pref.next_send_at = first_time(now, push_settings)
    PushPreference.objects.bulk_update(prefs, ["next_send_at"], batch_size=500)


def subscribe(user, data):
    keys = data.get("keys") or {}
    endpoint, p256dh, auth = data.get("endpoint", ""), keys.get("p256dh", ""), keys.get("auth", "")
    if not (endpoint.startswith("https://") and p256dh and auth) or len(endpoint) > 1000:
        raise ValueError("That subscription is not valid.")
    PushSubscription.objects.update_or_create(
        endpoint=endpoint, defaults={"user": user, "p256dh": p256dh[:200], "auth": auth[:100]})
    pref, _ = PushPreference.objects.get_or_create(user=user)
    if not pref.enabled or not pref.next_send_at:
        pref.enabled = True
        pref.next_send_at = first_time()
        pref.save(update_fields=["enabled", "next_send_at"])


def unsubscribe(user, endpoint=""):
    subscriptions = PushSubscription.objects.filter(user=user)
    if endpoint:
        subscriptions.filter(endpoint=endpoint).delete()
    else:
        subscriptions.delete()
    if not PushSubscription.objects.filter(user=user).exists() or not endpoint:
        PushPreference.objects.filter(user=user).update(enabled=False, next_send_at=None)


def pick_phrase(last_id=None):
    phrases = list(PushPhrase.objects.filter(is_active=True).values_list("pk", "text"))
    if len(phrases) > 1:
        phrases = [phrase for phrase in phrases if phrase[0] != last_id]
    return random.choice(phrases) if phrases else None


def send_to_user(user, text, title=None, url="/"):
    """Send one notification to every device of `user`; returns how many devices got it."""
    from pywebpush import WebPushException, webpush

    title = title or PushSettings.load().title
    payload = json.dumps({"title": title, "body": text, "url": url})
    sent = 0
    for subscription in PushSubscription.objects.filter(user=user):
        try:
            webpush(
                subscription_info={"endpoint": subscription.endpoint,
                                   "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth}},
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_SUBJECT},
                ttl=6 * 60 * 60,
                timeout=10,
            )
            sent += 1
        except WebPushException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status in GONE:
                subscription.delete()
            else:
                logger.warning("Push to user %s failed: %s", user.pk, error)
        except Exception as error:          # network trouble: try again next time
            logger.warning("Push to user %s failed: %s", user.pk, error)
    return sent


def send_due(limit=BATCH):
    """Send each student whose time has come one random phrase, and set their next time."""
    if not configured():
        return 0
    push_settings = PushSettings.load()
    if not push_settings.enabled or not PushPhrase.objects.filter(is_active=True).exists():
        return 0
    if not cache.add("push-sending", 1, 5 * 60):      # another process is sending right now
        return 0
    sent = 0
    try:
        now = timezone.now()
        due = list(PushPreference.objects.filter(enabled=True, next_send_at__lte=now)
                   .select_related("user").order_by("next_send_at")[:limit])
        for pref in due:
            # Move the schedule first, so a failure never sends the same student two in a row.
            phrase = pick_phrase(pref.last_phrase_id)
            pref.next_send_at = next_time(now, push_settings)
            pref.last_sent_at = now
            pref.last_phrase_id = phrase[0]
            pref.save(update_fields=["next_send_at", "last_sent_at", "last_phrase"])
            if send_to_user(pref.user, phrase[1], push_settings.title):
                sent += 1
            elif not PushSubscription.objects.filter(user=pref.user).exists():
                PushPreference.objects.filter(pk=pref.pk).update(enabled=False, next_send_at=None)
    finally:
        cache.delete("push-sending")
    if sent:
        logger.info("Sent %d notification(s)", sent)
    return sent


def _tick():
    close_old_connections()
    try:
        send_due()
    except Exception:
        logger.exception("Sending notifications failed")
    finally:
        close_old_connections()


def maybe_tick():
    """Start a background send at most once a minute (called after page requests)."""
    if (configured() and getattr(settings, "PUSH_BACKGROUND_TICKS", True)
            and cache.add("push-tick", 1, TICK_EVERY)):
        threading.Thread(target=_tick, daemon=True).start()
