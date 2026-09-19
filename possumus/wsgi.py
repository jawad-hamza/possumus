"""
WSGI entry point.

Used by gunicorn on a VPS (`gunicorn possumus.wsgi:application`), by waitress in the
Windows desktop build (desktop/launcher.py) and by Vercel's Django preset, which reads
WSGI_APPLICATION from settings and loads `application` from this module.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "possumus.settings")

application = get_wsgi_application()

# Generic Python hosts look for a callable named `app`.
app = application
