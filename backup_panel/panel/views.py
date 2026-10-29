from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from panel import checks, dockerops, health
from panel.forms import DB_NAME, RESERVED, RestoreForm
from panel.models import Job


@login_required
def instance_list(request):
    """Servers with their stacks. Running stacks start as 'loading' rows that fetch their own summary;
    stopped ones are already known. `?refresh=1` makes those summaries read /backups again."""
    servers = dockerops.discover_all(use_cache=request.GET.get("refresh") != "1")
    groups = [
        {
            "server": server,
            "rows": [{"instance": i, "health": health.assess(i, None)} for i in server.instances],
        }
        for server in servers
    ]
    context = {
        "configured": dockerops.is_configured(),
        "servers": servers,
        "groups": groups,
        "instance_count": sum(len(server.instances) for server in servers),
        "refresh": request.GET.get("refresh") == "1",
    }
    return render(request, "panel/instance_list.html", context)


def resolve(endpoint_name, slug):
    """(endpoint, server, instance) for a URL. `instance` is None when the server cannot be read;
    an unknown endpoint or stack is a 404."""
    endpoint = dockerops.find_endpoint(endpoint_name)
    if endpoint is None:
        raise Http404("Unknown server")
    server = dockerops.look_at(endpoint)
    if server.error:
        return endpoint, server, None
    instance = next((i for i in server.instances if i.slug == slug), None)
    if instance is None:
        raise Http404("Stack not found")
    return endpoint, server, instance


PANEL_KEEP = timedelta(minutes=15)  # a finished backup or restore stays on the detail page this long


@login_required
def instance_detail(request, endpoint, slug):
    endpoint, server, instance = resolve(endpoint, slug)
    last_job = Job.latest_for(endpoint.name, slug)
    shown = last_job and (
        last_job.is_active or (last_job.finished_at and timezone.now() - last_job.finished_at < PANEL_KEEP)
    )
    context = {
        "endpoint": endpoint,
        "server": server,
        "instance": instance,
        "slug": slug,
        "active_job": Job.active_for(endpoint.name, slug),
        "last_job": last_job,
        "panel_job": last_job if shown else None,
        "health": health.assess(instance, None) if instance else None,
    }
    return render(request, "panel/instance_detail.html", context)


def read_backups(request, endpoint, slug):
    """Shared by both fragments: the context for one instance's backups, with any failure as a message."""
    endpoint, server, instance = resolve(endpoint, slug)
    context = {"endpoint": endpoint, "server": server, "instance": instance, "slug": slug, "listing": None, "error": ""}
    if instance is None:
        context["error"] = server.error
        return context
    if not instance.is_running:
        context["health"] = health.assess(instance, None)
        return context
    refresh = request.GET.get("refresh") == "1"
    try:
        context["listing"] = dockerops.list_backups_cached(endpoint, instance, use_cache=not refresh)
    except dockerops.DOCKER_ERRORS as e:
        context["error"] = dockerops.describe(e)
    context["health"] = health.assess(instance, context["listing"], context["error"])
    return context


@login_required
def backups_partial(request, endpoint, slug):
    return render(request, "panel/_backup_table.html", read_backups(request, endpoint, slug))


@login_required
def summary_partial(request, endpoint, slug):
    return render(request, "panel/_backup_summary.html", read_backups(request, endpoint, slug))


def ensure_listed(endpoint, instance, filename):
    """Only names that are really in /backups: the listing the table was built from, read again once
    if the name is not in it (a backup made a moment ago)."""
    for use_cache in (True, False):
        listing = dockerops.list_backups_cached(endpoint, instance, use_cache=use_cache)
        if filename in {b.name for b in listing.backups}:
            return
    raise dockerops.BackupNotFound(f"{filename} is not in the backup list.")


def error_page(request, status, title, message, back_url):
    return render(
        request, "panel/error.html", {"title": title, "message": message, "back_url": back_url}, status=status
    )


def finish(job, status, log):
    job.status, job.log, job.finished_at = status, log, timezone.now()
    job.save(update_fields=["status", "log", "finished_at"])


def audited(download, job):
    """Pass the download's pieces on, then record how it ended (done, or failed with the reason)."""
    sent = 0
    chunks = download.chunks()
    try:
        for chunk in chunks:
            sent += len(chunk)
            yield chunk
    except GeneratorExit:  # the browser went away
        finish(job, Job.Status.FAILED, f"Stopped by the client after {sent} of {download.size} bytes.")
        raise
    except Exception as e:
        finish(job, Job.Status.FAILED, dockerops.describe(e))
        raise
    else:
        finish(job, Job.Status.DONE, f"{sent} bytes sent.")
    finally:
        chunks.close()


@login_required
@require_GET
def download(request, endpoint, slug, filename):
    detail_url = reverse("instance_detail", args=[endpoint, slug])
    if not dockerops.ZIP_NAME.fullmatch(filename):
        raise Http404("Invalid file name")
    endpoint, server, instance = resolve(endpoint, slug)
    if instance is None:
        return error_page(request, 502, "Server cannot be reached", server.error, detail_url)
    try:
        ensure_listed(endpoint, instance, filename)
        stream = dockerops.open_backup_stream(dockerops.docker_client(endpoint).api, instance, filename)
    except dockerops.BackupNotFound as e:
        return error_page(request, 404, "File not found", str(e), detail_url)
    except dockerops.ContainerNotRunning as e:
        return error_page(request, 409, "Container stopped", str(e), detail_url)
    except dockerops.DOCKER_ERRORS as e:
        return error_page(request, 502, "Download could not start", dockerops.describe(e), detail_url)

    job = Job.objects.create(
        kind=Job.Kind.DOWNLOAD,
        status=Job.Status.RUNNING,
        endpoint=endpoint.name,
        instance_slug=slug,
        container_name=instance.container_name,
        filename=filename,
        created_by=request.user,
        started_at=timezone.now(),
    )
    response = StreamingHttpResponse(audited(stream, job), content_type="application/zip")
    response["Content-Length"] = str(stream.size)
    response["Content-Disposition"] = f'attachment; filename="{stream.name}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Accel-Buffering"] = "no"  # nginx must not buffer a zip of hundreds of MB
    return response


# --- backup now, restore, job pages ----------------------------------------------------------


def enqueue(request, endpoint, instance, kind, **fields):
    """A new waiting Job, or None when this instance already has a backup or restore waiting or running.
    The check and the insert are one write transaction, so two clicks cannot both get through."""
    with transaction.atomic():
        if Job.active_for(endpoint.name, instance.slug):
            return None
        return Job.objects.create(
            kind=kind,
            endpoint=endpoint.name,
            instance_slug=instance.slug,
            container_name=instance.container_name,
            created_by=request.user,
            **fields,
        )


def busy_page(request, endpoint, slug):
    active = Job.active_for(endpoint.name, slug)
    back = reverse("job_status", args=[active.pk]) if active else reverse("instance_detail", args=[endpoint.name, slug])
    return error_page(
        request,
        409,
        "A job is still running",
        "A backup or restore for this stack is running. Wait until it finishes.",
        back,
    )


def usable_instance(request, endpoint, slug):
    """(endpoint, instance, None) for a stack that can take a job now, else (None, None, error response)."""
    detail_url = reverse("instance_detail", args=[endpoint, slug])
    endpoint, server, instance = resolve(endpoint, slug)
    if instance is None:
        return None, None, error_page(request, 502, "Server cannot be reached", server.error, detail_url)
    if not instance.is_running:
        message = f"Container {instance.container_name} is {instance.status}. Start it in Portainer first."
        return None, None, error_page(request, 409, "Container stopped", message, detail_url)
    return endpoint, instance, None


@login_required
@require_POST
def backup_now(request, endpoint, slug):
    endpoint, instance, problem = usable_instance(request, endpoint, slug)
    if problem:
        return problem
    job = enqueue(request, endpoint, instance, Job.Kind.BACKUP)
    if job is None:
        return busy_page(request, endpoint, slug)
    return redirect("instance_detail", endpoint.name, slug)  # its job panel follows the job


def restore_blocked(request, endpoint, instance, slug):
    """An error page when this stack cannot take a restore at all, else None."""
    root = instance.config.get("FILESTORE_ROOT", "/odoo-data/filestore")
    if not root.startswith("/odoo-data/"):
        message = f"FILESTORE_ROOT ({root}) is outside /odoo-data, so restoring from the panel is refused."
        return error_page(
            request,
            409,
            "Restore is not possible",
            message,
            reverse("instance_detail", args=[endpoint.name, slug]),
        )
    return None


def wizard_start(form):
    """The wizard step to open: the first one with an error."""
    if form.errors.get("filename"):
        return 1
    if form.errors.get("new_db"):
        return 2
    return 3 if form.errors else 1


@login_required
@require_http_methods(["GET", "POST"])
def restore(request, endpoint, slug):
    """The restore wizard: pick a backup, name the new database, confirm. The checks in step 2 are
    early feedback (restore_check); the worker looks again when the job runs."""
    endpoint, instance, problem = usable_instance(request, endpoint, slug)
    if problem:
        return problem
    detail_url = reverse("instance_detail", args=[endpoint.name, slug])
    if blocked := restore_blocked(request, endpoint, instance, slug):
        return blocked
    try:
        listing = dockerops.list_backups_cached(endpoint, instance, use_cache=request.method == "GET")
    except dockerops.DOCKER_ERRORS as e:
        return error_page(request, 502, "Restore could not start", dockerops.describe(e), detail_url)
    if not listing.backups:
        return error_page(request, 404, "No backups yet", "There is no backup file to restore.", detail_url)

    names = {b.name for b in listing.backups}
    if request.method == "POST":
        form = RestoreForm(request.POST)
        if form.is_valid() and form.cleaned_data["filename"] not in names:
            form.add_error("filename", "That file is not in the backup list.")
        if form.is_valid():
            data = form.cleaned_data
            job = enqueue(
                request,
                endpoint,
                instance,
                Job.Kind.RESTORE,
                filename=data["filename"],
                target_db=data["new_db"],
                neutralize=data["neutralize"],
            )
            if job is None:
                return busy_page(request, endpoint, slug)
            return redirect("job_status", job.pk)
        selected = request.POST.get("filename", "")
    else:
        asked = request.GET.get("file", "")
        selected = asked if asked in names else listing.latest.name
        form = RestoreForm(initial={"filename": selected, "new_db": dockerops.suggest_db_name(selected)})
    context = {
        "endpoint": endpoint,
        "instance": instance,
        "slug": slug,
        "listing": listing,
        "selected": selected if selected in names else listing.latest.name,
        "form": form,
        "start": wizard_start(form) if request.method == "POST" else 1,
        "active_job": Job.active_for(endpoint.name, slug),
    }
    return render(request, "panel/restore.html", context, status=400 if request.method == "POST" else 200)


@login_required
@require_POST
def restore_check(request, endpoint, slug):
    """Step 2 of the wizard: what can be known before the restore is queued. A POST because it starts
    short-lived read-only helper containers."""
    db = request.POST.get("new_db", "").strip()
    neutralize = request.POST.get("neutralize") == "on"
    endpoint, server, instance = resolve(endpoint, slug)
    if instance is None:
        items = [checks.Check(False, "Server reachable", server.error)]
    elif not instance.is_running:
        items = [
            checks.Check(
                False, "Backup container running", f"Container {instance.container_name} is {instance.status}."
            )
        ]
    elif not DB_NAME.fullmatch(db) or db in RESERVED:
        items = [checks.Check(False, "Database name valid", "Use lowercase letters, digits and underscores (max 63).")]
    else:
        try:
            items = checks.run_checks(endpoint, instance, db, neutralize)
        except dockerops.DOCKER_ERRORS as e:
            items = [checks.Check(False, "Pemeriksaan", dockerops.describe(e))]
    return render(request, "panel/_restore_checks.html", {"checks": items, "all_ok": all(c.ok for c in items)})


def job_response(request, job, full_template, partial_template):
    """A job's page for a browser, or just its fragment for HTMX. When a backup or restore has ended, the
    remembered /backups listing is dropped and the page is told to reload its table."""
    template = partial_template if request.headers.get("HX-Request") else full_template
    standalone = not request.headers.get("HX-Request") or request.GET.get("page") == "1"  # on its own page: no Tutup
    response = render(request, template, {"job": job, "standalone": standalone})
    if request.headers.get("HX-Request") and not job.is_active and job.kind != Job.Kind.DOWNLOAD:
        dockerops.forget_listing(job.endpoint, job.instance_slug)
        response["HX-Trigger"] = "backups-changed"
    return response


@login_required
def job_status(request, pk):
    return job_response(request, get_object_or_404(Job, pk=pk), "panel/job_status.html", "panel/_job_panel.html")


@login_required
def job_list(request):
    return render(request, "panel/job_list.html", {"jobs": Job.objects.select_related("created_by")[:200]})
