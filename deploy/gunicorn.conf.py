"""gunicorn settings for the production container (Dockerfile, compose.yaml).

Every value can be overridden through the environment, e.g. GUNICORN_WORKERS in
/srv/zehnms/.env or in compose.yaml.
"""

import multiprocessing
import os


def _int(name, default):
    value = os.getenv(name, "")
    return int(value) if value.strip() else default


bind = os.getenv("GUNICORN_BIND", "127.0.0.1:8000")

# Each Django worker holds roughly 80-120 MB. 2 x CPU + 1, capped so a small VPS does not swap.
workers = _int("GUNICORN_WORKERS", min(multiprocessing.cpu_count() * 2 + 1, 8))
# Threads let one worker overlap database waits, so reports and PDF invoices stay responsive.
worker_class = "gthread"
threads = _int("GUNICORN_THREADS", 2)

# Long reports and backups get a minute; nginx waits a little longer than that.
timeout = _int("GUNICORN_TIMEOUT", 60)
graceful_timeout = 30
keepalive = 5

# Recycle workers now and then so a slow leak can never grow without bound.
max_requests = _int("GUNICORN_MAX_REQUESTS", 1000)
max_requests_jitter = 100

# Who may tell gunicorn the original scheme. In the container the published port is bound to the
# host's loopback only, so compose.yaml sets "*"; outside a container keep it to localhost.
forwarded_allow_ips = os.getenv("GUNICORN_FORWARDED_ALLOW_IPS", "127.0.0.1,::1")

# Heartbeat files on tmpfs: a slow disk can otherwise make healthy workers look dead.
worker_tmp_dir = "/dev/shm"

# Everything to stdout/stderr: `docker compose logs -f web`.
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
# No query strings in the log: password reset and verification links carry tokens there.
access_log_format = '%({x-forwarded-for}i)s "%(m)s %(U)s" %(s)s %(B)s %(M)sms "%(a)s"'

proc_name = "zehnms"

# gunicorn 26's runtime control socket (gunicornc). Not used here, and the container's code
# folder is read-only, so it would only log an error at every start.
control_socket_disable = True
