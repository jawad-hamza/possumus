from accounts.services.current_actor import reset_current_actor, set_current_actor


class CurrentActorMiddleware:
    """Must sit after AuthenticationMiddleware so request.user is available."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = set_current_actor(getattr(request, "user", None))
        try:
            return self.get_response(request)
        finally:
            reset_current_actor(token)
