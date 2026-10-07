"""Workflow orchestration.

Coordinates generate → validate → publish → log → source write-back through
abstractions only: no YAML, no Notion JSON, no Tweepy and no Gemini SDK
knowledge lives here.

Reliability layer (Pass 3): before any dangerous side effect the
orchestrator consults the durable technical state in SQLite
(:class:`ExecutionStore`). A publication is persisted **before** Notion is
updated, so a failed Notion sync can never cause a second X post.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.execution_log import ExecutionLog
from app.generator import ContentGenerator, GenerationError
from app.images.base import ImageGenerationError
from app.images.models import GeneratedImage
from app.images.service import prepare_item_image
from app.models import ContentItem, ContentStatus, PublishResult
from app.publishers.base import PlatformPublisher
from app.sources.base import ContentSource
from app.storage import (
    ExecutionRecord,
    ExecutionStatus,
    ExecutionStore,
    compute_content_hash,
)
from app.validation import validate_content, validate_item


@dataclass
class WorkflowResult:
    item: ContentItem
    status: ContentStatus
    publish_result: Optional[PublishResult] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        """The workflow finished without a failure.

        ``published`` after a real publish, ``ready`` after a DRY RUN.
        """
        return self.status in (ContentStatus.PUBLISHED, ContentStatus.READY)


class ContentOrchestrator:
    """Runs the full content workflow for a single :class:`ContentItem`.

    ``source`` (optional) receives every meaningful state change so the
    editorial calendar stays in sync. ``store`` (optional) is the durable
    idempotency guard: with it, a known publication is never repeated.
    ``dry_run`` stops the workflow at ``Ready``: a simulated publish is
    never reported as a publication. ``image_generator`` (optional) powers
    the ``generate_image`` path; without it, items requesting an image
    fail explicitly instead of silently publishing text-only.
    """

    def __init__(
        self,
        generator: ContentGenerator,
        publisher: PlatformPublisher,
        audit_log: ExecutionLog,
        *,
        source: Optional[ContentSource] = None,
        dry_run: bool = False,
        store: Optional[ExecutionStore] = None,
        image_generator=None,
        image_model: Optional[str] = None,
    ):
        self.generator = generator
        self.publisher = publisher
        self.audit_log = audit_log
        self.source = source
        self.dry_run = dry_run
        self.store = store
        self.image_generator = image_generator
        self.image_model = image_model

    # -- public entry point -------------------------------------------------

    def run(self, item: ContentItem) -> WorkflowResult:
        print(
            f"\n  [{item.id}] Generando contenido — tema: {item.topic}"
        )

        gate = self._gate(item)
        if gate is not None:
            return gate

        precheck = validate_item(item)
        if not precheck.ok:
            return self._fail(item, "; ".join(precheck.errors))

        self._store(item, "increment_attempt")

        item.status = ContentStatus.GENERATING
        item.error = None
        self._sync(item, status=ContentStatus.GENERATING)
        self._store(item, "mark_generating")

        try:
            content = self.generator.generate(item)
        except GenerationError as exc:
            return self._fail(item, str(exc))
        except Exception as exc:  # defensive: generators must not leak errors
            return self._fail(item, f"error inesperado generando contenido: {exc}")

        item.generated_content = content
        item.status = ContentStatus.READY
        item.error = None
        self._sync(item, generated_content=content, status=ContentStatus.READY)
        self._store(
            item,
            "mark_ready",
            generated_content=content,
            content_hash=compute_content_hash(item.id, item.platform, content),
        )
        print(f'  Contenido generado:\n  "{content}"')

        validation = validate_content(item, content)
        if not validation.ok:
            return self._fail(item, "; ".join(validation.errors))

        media: Optional[GeneratedImage] = None
        if item.generate_image:
            try:
                media = self._prepare_image(item, content)
            except ImageGenerationError as exc:
                # No silent fallback: the editor asked for an image.
                return self._fail(item, str(exc))

        if not self.dry_run:
            item.status = ContentStatus.PUBLISHING
            stored = self._store(item, "mark_publishing")
            if self.store is not None and stored is None:
                # Without a durable "publishing" record we cannot guarantee
                # idempotency: refuse the external side effect.
                return self._fail(
                    item,
                    "no se pudo registrar el intento en SQLite; "
                    "se aborta antes de publicar",
                )
            if not self._sync(item, status=ContentStatus.PUBLISHING):
                print(
                    "  ⚠ AVISO: la transición Publishing no llegó a la fuente; "
                    "el registro técnico ya está en 'publishing'."
                )

        publish_result = self.publisher.publish(item, content, media=media)

        if publish_result.status == "error":
            return self._publish_failed(item, publish_result, media=media)

        if self.dry_run or publish_result.status == "simulated":
            return self._finish_simulated(item, publish_result, media=media)

        return self._finish_published(item, publish_result, media=media)

    def _prepare_image(
        self, item: ContentItem, content: str
    ) -> Optional[GeneratedImage]:
        """Resolve, reuse-or-generate and validate the item image."""
        if self.image_generator is None:
            raise ImageGenerationError(
                "el item pide imagen pero no hay generador de imágenes "
                "configurado"
            )
        return prepare_item_image(
            item,
            content,
            store=self.store,
            image_generator=self.image_generator,
            image_model=self.image_model,
        )

    # -- idempotency gate ---------------------------------------------------

    def _gate(self, item: ContentItem) -> Optional[WorkflowResult]:
        """Consult durable state before any dangerous side effect."""
        if self.store is None:
            return None
        try:
            execution = self.store.get_execution(item.id, item.platform)
        except Exception as exc:
            # Conservative: an unreadable idempotency store must not be
            # treated as "never published". Fail this cycle, retry next poll.
            message = f"no se pudo leer el estado técnico en SQLite: {exc}"
            print(f"  ✗ {message}")
            self._safe_audit(
                item,
                item.status,
                publish_status="store_error",
                error=message,
            )
            return WorkflowResult(item=item, status=ContentStatus.FAILED, error=message)

        if execution is None:
            return None
        if execution.is_published:
            return self._already_published(item, execution)
        if execution.execution_status is ExecutionStatus.MANUAL_REVIEW:
            return self._blocked(
                item,
                error=execution.last_error
                or "estado técnico en revisión manual; no se ejecuta automáticamente",
                external_post_id=execution.external_post_id,
            )
        if (
            execution.execution_status is ExecutionStatus.PUBLISHING
            and not execution.external_post_id
        ):
            return self._interrupted_publishing(item, execution)
        # pending / generating / ready / failed / simulated: safe to run.
        return None

    def _already_published(
        self, item: ContentItem, execution: ExecutionRecord
    ) -> WorkflowResult:
        """Never publish again: reconcile Notion instead."""
        if execution.content_hash and item.generated_content:
            current_hash = compute_content_hash(
                item.id, item.platform, item.generated_content
            )
            if current_hash != execution.content_hash:
                print(
                    "  ⚠ El contenido de la fuente cambió desde la publicación; "
                    "se mantiene el bloqueo (identidad: content_item_id + plataforma)."
                )
        print(
            f"  ⚠ Item ya publicado (post externo "
            f"{execution.external_post_id or 'desconocido'}); se omite la "
            "publicación y se reconcilia la fuente."
        )

        sync_error = None
        if execution.needs_notion_sync or item.status is not ContentStatus.PUBLISHED:
            sync_error = self._sync_to_published(item, execution)

        self._safe_audit(
            item,
            ContentStatus.PUBLISHED,
            publish_status="duplicate_blocked",
            external_post_id=execution.external_post_id,
            error=sync_error,
        )
        publish_result = PublishResult(
            status="published",
            platform=item.platform,
            id=execution.external_post_id,
            url=item.published_url,
        )
        return WorkflowResult(
            item=item,
            status=ContentStatus.PUBLISHED,
            publish_result=publish_result,
            error=sync_error,
        )

    def _interrupted_publishing(
        self, item: ContentItem, execution: ExecutionRecord
    ) -> WorkflowResult:
        """Crash between the publishing record and the X response.

        The external outcome is unknown, so the record is moved to the
        terminal manual-review state *before* anything else: no automatic
        run will ever publish this item again.
        """
        error = (
            "la ejecución se interrumpió durante la publicación: el resultado "
            "externo es desconocido; revisión manual requerida "
            "(verificar en X antes de reprogramar)"
        )
        self._store(item, "mark_manual_review", error=error)
        return self._blocked(
            item, error=error, external_post_id=execution.external_post_id
        )

    def _blocked(
        self,
        item: ContentItem,
        *,
        error: str,
        external_post_id: Optional[str] = None,
    ) -> WorkflowResult:
        print(f"  ✗ ITEM BLOQUEADO [{item.id}]: {error}")
        item.status = ContentStatus.FAILED
        item.error = error
        self._sync(item, status=ContentStatus.FAILED, error=error)
        self._safe_audit(
            item,
            ContentStatus.FAILED,
            publish_status="error",
            error=error,
            external_post_id=external_post_id,
        )
        return WorkflowResult(item=item, status=ContentStatus.FAILED, error=error)

    # -- workflow endings ---------------------------------------------------

    def _fail(self, item: ContentItem, error: str) -> WorkflowResult:
        item.status = ContentStatus.FAILED
        item.error = error
        self._store(item, "mark_failed", error=error)
        self._sync(item, status=ContentStatus.FAILED, error=error)
        self.audit_log.record(
            item,
            item.status,
            publish_status="error",
            error=error,
        )
        print(f"  ✗ {error}")
        return WorkflowResult(item=item, status=item.status, error=error)

    def _publish_failed(
        self,
        item: ContentItem,
        publish_result: PublishResult,
        media: Optional[GeneratedImage] = None,
    ) -> WorkflowResult:
        item.status = ContentStatus.FAILED
        error = publish_result.error or "error desconocido al publicar"
        self._store(item, "mark_failed", error=error)
        self._sync(item, status=ContentStatus.FAILED, error=error)
        self.audit_log.record_publish(
            item,
            publish_result,
            item.status,
            error=error,
            generated_image_path=media.local_path if media else None,
            generated_image_hash=media.sha256 if media else None,
        )
        print(f"  ✗ Error al publicar: {error}")
        return WorkflowResult(
            item=item,
            status=item.status,
            publish_result=publish_result,
            error=error,
        )

    def _finish_simulated(
        self,
        item: ContentItem,
        publish_result: PublishResult,
        media: Optional[GeneratedImage] = None,
    ) -> WorkflowResult:
        # DRY RUN: el ciclo termina en Ready y jamás afirma una
        # publicación real (aunque un publisher lo reclamara).
        if self.dry_run and publish_result.status != "simulated":
            publish_result = PublishResult(
                status="simulated",
                platform=publish_result.platform,
                dry_run=True,
            )
        item.status = ContentStatus.READY
        if not self.dry_run:
            # Revertimos el Publishing que sí llegó a escribirse.
            self._sync(item, status=ContentStatus.READY)
        self._store(item, "mark_simulated")
        self.audit_log.record_publish(
            item,
            publish_result,
            ContentStatus.READY,
            generated_image_path=media.local_path if media else None,
            generated_image_hash=media.sha256 if media else None,
        )
        print("  [DRY RUN] No se publicó realmente — modo simulación activo.")
        return WorkflowResult(
            item=item,
            status=ContentStatus.READY,
            publish_result=publish_result,
        )

    def _finish_published(
        self,
        item: ContentItem,
        publish_result: PublishResult,
        media: Optional[GeneratedImage] = None,
    ) -> WorkflowResult:
        # ORDER MATTERS: durable record first, Notion second.
        stored = self._store(
            item,
            "mark_published",
            external_post_id=publish_result.id or "",
            media_id=publish_result.media_id,
            critical=True,
        )
        if self.store is not None and stored is None:
            print(
                "  ⚠ AVISO CRÍTICO: X publicó el post pero SQLite no pudo "
                "guardarlo; se intenta igualmente sincronizar la fuente. "
                "Si ambos fallan, el próximo ciclo pedirá revisión manual."
            )

        item.status = ContentStatus.PUBLISHED
        item.published_url = publish_result.url
        item.error = None
        synced, sync_error = self._sync_detailed(
            item,
            status=ContentStatus.PUBLISHED,
            published_url=publish_result.url,
            error=None,
        )
        if synced:
            self._store(item, "mark_synced")
        else:
            self._store(item, "mark_sync_pending", error=sync_error)
            print(
                "  ⚠ AVISO CRÍTICO: X publicó el post y SQLite lo registró "
                f"(post {publish_result.id}), pero la sincronización con la "
                "fuente falló. NO se reintentará la publicación: el próximo "
                "ciclo sólo reintentará la sincronización."
            )
        self.audit_log.record_publish(
            item,
            publish_result,
            item.status,
            generated_image_path=media.local_path if media else None,
            generated_image_hash=media.sha256 if media else None,
        )
        if synced:
            print(f"  ✓ Publicado — ID: {publish_result.id}")
        return WorkflowResult(
            item=item, status=item.status, publish_result=publish_result
        )

    # -- reconciliation ------------------------------------------------------

    def reconcile_pending_syncs(self) -> list[str]:
        """Push known publications to the source. Never publishes.

        Used by the poll cycle so a stale Notion status (crash after X
        success, or a failed final sync) heals without needing the item to
        be Scheduled again.
        """
        if self.store is None or self.source is None:
            return []

        reconciled: list[str] = []
        for execution in self.store.pending_syncs():
            try:
                item = self.source.get_item(execution.content_item_id)
            except Exception as exc:
                print(
                    f"  ✗ Reconciliación: no se pudo leer "
                    f"'{execution.content_item_id}': {exc}"
                )
                continue
            if item is None:
                print(
                    "  ✗ Reconciliación: la página "
                    f"'{execution.content_item_id}' ya no existe en la fuente; "
                    "se mantiene pendiente."
                )
                continue

            sync_error = self._sync_to_published(item, execution)
            if sync_error is None:
                self._safe_audit(
                    item,
                    ContentStatus.PUBLISHED,
                    publish_status="reconciled",
                    external_post_id=execution.external_post_id,
                )
                reconciled.append(execution.content_item_id)
            else:
                print(
                    "  ✗ Reconciliación pendiente para "
                    f"'{execution.content_item_id}': {sync_error}"
                )
        return reconciled

    def _sync_to_published(
        self, item: ContentItem, execution: ExecutionRecord
    ) -> Optional[str]:
        """Write Published (+ URL when known) to the source; update SQLite."""
        changes: dict = {
            "status": ContentStatus.PUBLISHED,
            "error": None,
        }
        url = item.published_url or self._rebuild_url(execution)
        if url:
            changes["published_url"] = url

        synced, sync_error = self._sync_detailed(item, **changes)
        if synced:
            item.status = ContentStatus.PUBLISHED
            item.error = None
            if url:
                item.published_url = url
            self._store(item, "mark_synced")
            return None
        self._store(item, "mark_sync_pending", error=sync_error)
        return sync_error or "sincronización fallida"

    def _rebuild_url(self, execution: ExecutionRecord) -> Optional[str]:
        builder = getattr(self.publisher, "build_post_url", None)
        if builder is None or not execution.external_post_id:
            return None
        try:
            return builder(execution.external_post_id)
        except Exception:
            return None

    # -- helpers -------------------------------------------------------------

    def _store(
        self, item: ContentItem, method: str, *, critical: bool = False, **kwargs
    ) -> Optional[object]:
        """Run one store operation; never let storage break the workflow."""
        if self.store is None:
            return None
        try:
            operation = getattr(self.store, method)
            return operation(item.id, item.platform, **kwargs)
        except Exception as exc:
            level = "AVISO CRÍTICO" if critical else "AVISO"
            print(f"  ⚠ {level}: SQLite no pudo ejecutar '{method}' para {item.id}: {exc}")
            return None

    def _safe_audit(self, item: ContentItem, status, **kwargs) -> None:
        try:
            self.audit_log.record(item, status, **kwargs)
        except Exception:  # the audit trail must never break the workflow
            pass

    def _sync_detailed(self, item: ContentItem, **changes) -> tuple[bool, Optional[str]]:
        """Push state changes back to the content source.

        A failing write-back is logged loudly and never re-triggers a
        publish; durable reconciliation belongs to the next poll cycle.
        """
        if self.source is None:
            return True, None
        try:
            self.source.update_item(item.id, **changes)
            return True, None
        except Exception as exc:
            message = f"sincronización fallida [{item.id}]: {exc}"
            print(f"\n  ⚠ AVISO: {message}")
            try:
                self.audit_log.record(
                    item,
                    item.status,
                    publish_status="sync_error",
                    error=message,
                )
            except Exception:  # the audit trail must never break the workflow
                pass
            return False, str(exc)

    def _sync(self, item: ContentItem, **changes) -> bool:
        return self._sync_detailed(item, **changes)[0]


__all__ = ["ContentOrchestrator", "WorkflowResult"]
