"""Scheduling and polling.

Two modes share the same APScheduler instance:

* **Polling (default, Notion):** one interval job every
  ``CONTENT_POLL_INTERVAL_SECONDS`` queries the :class:`ContentSource` for
  due ``Scheduled`` items and runs them. No job is created per page.
* **Legacy per-item cron (YAML):** ``schedule_items`` keeps the original
  one-cron-job-per-item behaviour for the YAML source.

Jobs address items by ``ContentItem.id`` and resolve them through the
source, so raw source documents (YAML dictionaries, Notion payloads) never
enter this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from apscheduler.schedulers.blocking import BlockingScheduler

from app.config import DEFAULT_POLL_INTERVAL_SECONDS
from app.models import ContentItem
from app.orchestration import ContentOrchestrator
from app.sources.base import ContentSource

POLL_JOB_ID = "content-poll"


class ContentScheduler:
    def __init__(
        self,
        source: ContentSource,
        orchestrator: ContentOrchestrator,
        scheduler: Optional[object] = None,
        *,
        poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    ):
        self.source = source
        self.orchestrator = orchestrator
        self.poll_interval_seconds = poll_interval_seconds
        if scheduler is None:
            scheduler = BlockingScheduler()
            scheduler.configure(daemon=True)
        self.scheduler = scheduler

    # -- periodic polling --------------------------------------------------

    def add_polling_job(self, interval_seconds: Optional[int] = None) -> str:
        """Register the single interval job that drives the whole bot."""
        seconds = interval_seconds or self.poll_interval_seconds
        self.scheduler.add_job(
            self.poll_due_items,
            trigger="interval",
            seconds=seconds,
            id=POLL_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        return POLL_JOB_ID

    def poll_due_items(self, now: Optional[datetime] = None) -> list[str]:
        """Run every due ``Scheduled`` item once.

        One broken record (or one failing workflow) never stops the rest of
        the cycle, and a duplicate id inside the same cycle runs only once.
        Pending Notion synchronisations (publication already durable in
        SQLite) are reconciled first, without ever publishing anything.
        """
        self.reconcile_pending_syncs()
        moment = now or datetime.now(timezone.utc)
        items = self.source.get_scheduled_items(end=moment)

        processed: list[str] = []
        seen: set[str] = set()
        for item in items:
            if item.id in seen:
                continue
            seen.add(item.id)
            try:
                self.orchestrator.run(item)
            except Exception as exc:
                print(f"  ✗ El item '{item.id}' falló y no detiene el ciclo: {exc}")
                continue
            processed.append(item.id)
        return processed

    def reconcile_pending_syncs(self) -> list[str]:
        """Ask the orchestrator to heal stale source state (no publishing).

        Doubles without the hook (test doubles, YAML-only setups) simply
        skip reconciliation.
        """
        hook = getattr(self.orchestrator, "reconcile_pending_syncs", None)
        if hook is None:
            return []
        try:
            return hook() or []
        except Exception as exc:
            print(f"  ✗ La reconciliación de sincronizaciones falló: {exc}")
            return []

    # -- legacy per-item cron (YAML) ---------------------------------------

    def schedule_items(
        self, items: Optional[Iterable[ContentItem]] = None
    ) -> list[str]:
        """Schedule ``items`` (or everything the source knows about).

        Returns the ids of the jobs that were actually created.
        """
        if items is None:
            items = self.source.get_scheduled_items()

        job_ids: list[str] = []
        for item in items:
            if not isinstance(item, ContentItem):
                raise TypeError(
                    "schedule_items espera ContentItem, se obtuvo "
                    f"{type(item).__name__}"
                )
            if item.scheduled_at is None:
                print(f"  → (sin horario)  {item.topic}  [omitido: {item.id}]")
                continue

            moment: datetime = item.scheduled_at
            self.scheduler.add_job(
                self.run_job,
                trigger="cron",
                hour=moment.hour,
                minute=moment.minute,
                id=item.id,
                args=[item.id],
                replace_existing=True,
            )
            job_ids.append(item.id)
            print(f"  → {moment.strftime('%H:%M')}  {item.topic}")
        return job_ids

    def run_job(self, item_id: str) -> None:
        """Resolve the item through the source and execute the workflow."""
        item = self.source.get_item(item_id)
        if item is None:
            print(f"  ✗ No se encontró el item '{item_id}' en la fuente de contenido")
            return
        self.orchestrator.run(item)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        print("\n  Scheduler activo. Ctrl+C para detener.\n")
        try:
            self.scheduler.start()
        except KeyboardInterrupt:
            print("\n  Bot detenido.\n")

    def shutdown(self, wait: bool = False) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=wait)

    @property
    def job_ids(self) -> list[str]:
        return [job.id for job in self.scheduler.get_jobs()]


__all__ = ["ContentScheduler", "POLL_JOB_ID"]
