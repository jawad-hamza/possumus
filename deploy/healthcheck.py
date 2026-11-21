"""Docker HEALTHCHECK: GET /healthz/ inside the container.

Sends the first DJANGO_ALLOWED_HOSTS name as Host, since Django rejects any other host.
Exit 0 = healthy (app answers and the database is reachable), 1 = unhealthy.
"""

import http.client
import os
import sys

hosts = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "").split(",") if h.strip()]
host = (hosts[0] if hosts else "localhost").lstrip(".")
port = int(os.getenv("GUNICORN_BIND", "0.0.0.0:8000").rsplit(":", 1)[-1])

try:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    conn.request("GET", "/healthz/", headers={"Host": host})
    status = conn.getresponse().status
except OSError as exc:
    print(f"healthcheck: {exc}", file=sys.stderr)
    sys.exit(1)
sys.exit(0 if status == 200 else 1)
