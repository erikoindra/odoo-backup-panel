from django.conf import settings


def client_ip(request):
    """IP address of the client, as seen by the first trusted reverse proxy.

    Each trusted proxy (nginx: ``$proxy_add_x_forwarded_for``) appends the address it got the
    request from to ``X-Forwarded-For``. With N trusted proxies the client is therefore the
    N-th entry from the right. Anything further left was sent by the client and can be forged,
    so it is ignored. With no trusted proxy, or no header, ``REMOTE_ADDR`` is used.
    """
    count = settings.TRUSTED_PROXY_COUNT
    if count > 0:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if len(hops) >= count:
            return hops[-count]
    return request.META.get("REMOTE_ADDR", "")
