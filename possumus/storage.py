"""Static file storage that never turns a missing collectstatic into a 500."""
from whitenoise.storage import CompressedManifestStaticFilesStorage


class ForgivingManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    """Hashed, compressed static files when the manifest exists; plain names when it does not.

    The stock manifest storage raises ValueError for any {% static %} path that is missing
    from staticfiles.json. That is every page when collectstatic has not run yet (a fresh
    checkout, the test runner), so fall back to the unhashed URL and let the page render.
    """

    manifest_strict = False

    def stored_name(self, name):
        try:
            return super().stored_name(name)
        except ValueError:
            return name
