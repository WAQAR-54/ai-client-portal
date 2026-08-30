from django.conf import settings
from django.utils import translation

from accounts.geo import language_for_ip


class GeoLanguageMiddleware:
    """Picks a starting UI language from the visitor's IP country, for
    visitors who haven't chosen a language yet (no language cookie set).
    This only ever sets the *initial* guess - once a cookie exists (the
    visitor picked one, or this middleware already set one on an earlier
    request) it's left alone. For logged-in users it's harmless busywork at
    worst: UserLanguagePreferenceMiddleware's DB-stored preference always
    wins over whatever this or LocaleMiddleware guessed, further down the
    chain.

    Must run after SessionMiddleware and before LocaleMiddleware - it mutates
    request.COOKIES so LocaleMiddleware's own cookie-based detection (right
    after it in the chain) picks up the guess on this same request, then sets
    a real Set-Cookie on the response so the guess sticks for subsequent
    requests without a lookup every time. Deliberately doesn't key off
    request.user: AuthenticationMiddleware (which populates it) runs later in
    the chain than LocaleMiddleware requires this middleware to sit, so
    request.user isn't available yet here.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        cookie_name = settings.LANGUAGE_COOKIE_NAME
        detected = None
        if cookie_name not in request.COOKIES:
            ip_address = request.META.get("REMOTE_ADDR")
            detected = language_for_ip(ip_address)
            request.COOKIES[cookie_name] = detected

        response = self.get_response(request)

        if detected:
            response.set_cookie(cookie_name, detected)
        return response


class UserLanguagePreferenceMiddleware:
    """Makes the logged-in user's stored language preference
    (User.preferred_language) the active UI language for every request they
    make, regardless of cookies/browser - that's what makes the choice
    persist across logins and devices rather than being tied to one
    browser's cookie jar, per the spec ("not just per-session").

    Must run after AuthenticationMiddleware (needs request.user) and after
    LocaleMiddleware (this deliberately overrides its cookie/header-based
    guess for authenticated users) - see the MIDDLEWARE ordering comment in
    config/settings.py.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            preferred = request.user.preferred_language
            if translation.get_language() != preferred:
                translation.activate(preferred)
                request.LANGUAGE_CODE = translation.get_language()
        return self.get_response(request)
