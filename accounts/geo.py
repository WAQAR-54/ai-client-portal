"""IP -> country -> UI language guess, used only as the *starting* language
for visitors who haven't chosen one yet (see accounts/middleware.py). Never
overrides an explicit choice - once a cookie or a logged-in user's
preferred_language exists, this is never consulted again.

Uses geoip2fast: a pure-Python, offline IP-to-country lookup with its
database bundled in the pip package. No API key, no per-request network
call, no MaxMind account - fits a middleware that runs on every anonymous
request.
"""

from functools import lru_cache

from geoip2fast import GeoIP2Fast

# Arab League members / countries where Arabic is an official language.
_ARABIC_COUNTRIES = {
    "DZ",
    "BH",
    "KM",
    "DJ",
    "EG",
    "IQ",
    "JO",
    "KW",
    "LB",
    "LY",
    "MR",
    "MA",
    "OM",
    "PS",
    "QA",
    "SA",
    "SO",
    "SD",
    "SY",
    "TN",
    "AE",
    "YE",
    "EH",
}
_URDU_COUNTRIES = {"PK"}


@lru_cache(maxsize=1)
def _geoip():
    # Loaded once per process and reused - construction parses the bundled
    # database, so it's too slow to redo on every request.
    return GeoIP2Fast(verbose=False)


def country_code_for_ip(ip_address):
    """Best-effort ISO country code for an IP address, or None for
    private/local/unresolvable addresses (e.g. every request in local
    development, or anyone behind a VPN/proxy geoip can't place).

    Shared lookup behind language_for_ip below and billing/regions.py's
    region detection for the public pricing page - both need "what country
    is this visitor in" and reuse this one cached GeoIP2Fast instance
    rather than each doing their own lookup.
    """
    if not ip_address:
        return None
    try:
        result = _geoip().lookup(ip_address)
    except Exception:
        return None
    if result.is_private or not result.country_code:
        return None
    return result.country_code


def language_for_ip(ip_address):
    """Best-effort language code ("en"/"ur"/"ar") for an IP address.

    Falls back to "en" for private/local/unresolvable addresses (e.g. every
    request in local development, or anyone behind a VPN/proxy geoip can't
    place) rather than guessing.
    """
    country_code = country_code_for_ip(ip_address)
    if not country_code:
        return "en"
    if country_code in _ARABIC_COUNTRIES:
        return "ar"
    if country_code in _URDU_COUNTRIES:
        return "ur"
    return "en"
