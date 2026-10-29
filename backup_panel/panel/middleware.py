from django.conf import settings
from django.http import HttpResponse


class HtmxLoginRedirectMiddleware:
    """A page fragment fetched by HTMX after the session ran out would otherwise get the login page
    swapped into a table cell. Answer with HX-Redirect instead, so the whole browser window goes to
    the login page (and comes back afterwards)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            request.headers.get("HX-Request")
            and response.status_code == 302
            and response["Location"].startswith(f"/{settings.LOGIN_URL.strip('/')}/")
        ):
            redirect = HttpResponse(status=200)
            redirect["HX-Redirect"] = response["Location"]
            return redirect
        return response
