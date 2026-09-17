"""Docker healthcheck for the web service.

Hits config/urls.py::healthz, which actually runs a real database query
(SELECT 1) rather than just proving Gunicorn/Django itself is alive and
serving requests - the previous version of this script hit "/" and
treated ANY response (even a 4xx) as healthy, which would report
"healthy" even while the database was completely unreachable (every real
page would 500, but "/" still returns a redirect Gunicorn can render
without ever touching the database). A non-2xx here (503 from healthz
itself, or a connection failure) is genuinely unhealthy. Uses only the
stdlib since the slim runtime image doesn't include curl.
"""

import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://localhost:8000/healthz/", timeout=3)
except Exception:
    sys.exit(1)
else:
    sys.exit(0)
