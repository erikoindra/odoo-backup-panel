import signal
import time

from django.core.management.base import BaseCommand

from panel import jobs


class Command(BaseCommand):
    help = "Worker: runs queued backup and restore jobs one at a time (one worker, on purpose)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="run the jobs that are waiting now, then exit")
        parser.add_argument("--interval", type=float, default=2.0, help="seconds between looks at the queue")

    def handle(self, *args, once=False, interval=2.0, **options):
        stop = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.append(True))  # finish the current job, then leave

        recovered = jobs.recover_orphans()
        if recovered:
            self.stdout.write(f"{recovered} interrupted job(s) marked failed.")
        if not once:
            self.stdout.write(f"{jobs.sweep_temp_dirs()} container(s) cleaned of leftovers in /tmp.")
        self.stdout.write("Worker ready.")
        while not stop:
            job = jobs.run_next()
            if job:
                self.stdout.write(f"job #{job.pk} {job.kind}: {job.title}")
                continue  # look again at once: more may be waiting
            if once:
                break
            time.sleep(interval)
        self.stdout.write("Worker stopped.")
