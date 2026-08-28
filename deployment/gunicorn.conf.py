"""Gunicorn config for production.

Run from the project root with: gunicorn -c deployment/gunicorn.conf.py config.wsgi:application
Override any of these via environment variables — see each line below.
"""
import multiprocessing
import os

if "PORT" in os.environ:
    # Railway/Render (and most PaaS): their router connects to this
    # container directly on the port *they* assign, so it must be 0.0.0.0.
    bind = f"0.0.0.0:{os.environ['PORT']}"
else:
    # VPS behind Nginx: keep Gunicorn off the public interface, Nginx proxies to it.
    bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:8000")

# gthread, not sync: chat responses are long-lived SSE streams (StreamingHttpResponse)
# held open for the whole AI reply. Sync workers would tie up a full worker
# process per open stream; threads let one worker serve many concurrent chats.
#
# multiprocessing.cpu_count() reads the HOST machine's core count, not what
# a small PaaS container is actually allocated (often just a fraction of a
# core) - on Railway/Render this formula can ask for far more worker
# processes than the container's memory budget allows, and gunicorn just
# hangs/crash-loops with no clear error. Cap it at 3 unless overridden.
workers = int(os.environ.get("GUNICORN_WORKERS", min(multiprocessing.cpu_count() * 2 + 1, 3)))
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", 4))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 60))  # chat streaming responses can run long; raise if needed
graceful_timeout = 30
max_requests = 1000  # recycle workers periodically to bound memory growth
max_requests_jitter = 100

accesslog = "-"   # stdout — capture via systemd/journald
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
