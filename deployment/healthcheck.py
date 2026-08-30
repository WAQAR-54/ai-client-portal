"""Docker healthcheck for the web service.

Any real HTTP response - even a 4xx from a Host-header mismatch, which is
expected here since this hits the container by IP/localhost rather than
whatever ALLOWED_HOSTS value is configured - proves Gunicorn/Django is
alive and serving requests. Only a connection failure (crashed process,
import error at startup, etc.) counts as unhealthy. Uses only the stdlib
since the slim runtime image doesn't include curl.
"""

import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://localhost:8000/", timeout=3)
except urllib.error.HTTPError:
    sys.exit(0)
except Exception:
    sys.exit(1)
else:
    sys.exit(0)
