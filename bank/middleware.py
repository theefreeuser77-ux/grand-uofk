from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.http import HttpResponseNotFound


class SingleSessionMiddleware:
    """Require login for the site and allow only one active session per user."""

    CACHE_PREFIX = "bank:active-session:"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        login_path = settings.LOGIN_URL
        static_prefix = "/" + settings.STATIC_URL.lstrip("/")
        public_pwa_paths = {"/manifest.webmanifest", "/service-worker.js"}
        allow_unauthenticated = (
            path == "/accounts/login/"
            or path == "/accounts/login"
            or path.startswith("/accounts/signup")
            or path.startswith("/api/question/") and (path.endswith(("/save/", "/comment/", "/note/")))
            or path.startswith("/push/tick/")      # outside cron job; checks its own secret key
        )

        # Signup is disabled; keep the path out of the public site and return 404.
        if path.startswith("/accounts/signup"):
            return HttpResponseNotFound("Signup is disabled.")

        # The login page and static assets must remain reachable anonymously.
        if (not request.user.is_authenticated and not allow_unauthenticated
            and path not in public_pwa_paths and not path.startswith(static_prefix)):
            return redirect_to_login(request.get_full_path(), login_path)

        if request.user.is_authenticated:
            cache_key = self.CACHE_PREFIX + str(request.user.pk)
            active_key = cache.get(cache_key)
            current_key = request.session.session_key

            if active_key and active_key != current_key:
                if not Session.objects.filter(session_key=active_key).exists():
                    cache.set(cache_key, current_key, None)
                else:
                    logout(request)
                    return redirect_to_login(request.get_full_path(), login_path)

            # If the cache was restarted/cleared, adopt the current session.
            if current_key and not active_key:
                cache.set(cache_key, current_key, None)

        return self.get_response(request)


class PushTickMiddleware:
    """After page requests, let due notifications go out in the background (at most once a minute)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith("/" + settings.STATIC_URL.lstrip("/")):
            from . import push
            try:
                push.maybe_tick()
            except Exception:       # notifications must never break a page
                pass
        return response
