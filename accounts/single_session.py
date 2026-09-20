"""One signed-in browser per account.

Signing in on a second browser or device signs the first one out. How:

* every login (password, post-MFA, Google, signup) stores a fresh random token on the user row
  (`User.active_session_token`) and in that browser's session data (`SESSION_KEY`);
* `SingleSessionMiddleware` compares the two on every authenticated request. A session whose token no longer
  matches was superseded by a newer login: it is logged out and sent to the login page with an explanation.

The token lives in the session DATA, not the session key, so Django cycling the key (password change via
`update_session_auth_hash`) does not sign the user out of their own browser.

Accounts that were already signed in when this shipped have no token yet: the first request from any of their
sessions adopts one (a race between two such sessions is settled by a conditional UPDATE, so exactly one wins).

Switch off with SINGLE_SESSION_PER_USER=False (accounts then behave as before: any number of browsers).
"""

import hmac
import secrets

from django.conf import settings

SESSION_KEY = "single_session_token"


def enabled():
    return bool(getattr(settings, "SINGLE_SESSION_PER_USER", True))


def _new_token():
    return secrets.token_urlsafe(32)


def start_session(request, user):
    """Make THIS browser session the account's only current one. Called on every successful login."""
    from accounts.models import User

    token = _new_token()
    User.objects.filter(pk=user.pk).update(active_session_token=token)
    user.active_session_token = token
    request.session[SESSION_KEY] = token


def check_session(request):
    """True if this request's session is the account's current one (adopting the account's first token if it has
    none yet), False if a newer login has superseded it. Only call for an authenticated request."""
    from accounts.models import User

    user = request.user
    mine = request.session.get(SESSION_KEY) or ""
    current = user.active_session_token
    if not current:
        token = _new_token()
        if User.objects.filter(pk=user.pk, active_session_token="").update(active_session_token=token):
            user.active_session_token = token
            request.session[SESSION_KEY] = token
            return True
        # Someone else adopted or logged in between our read and our write: compare against the fresh value.
        current = User.objects.filter(pk=user.pk).values_list("active_session_token", flat=True).first() or ""
    return bool(mine) and bool(current) and hmac.compare_digest(mine.encode(), current.encode())
