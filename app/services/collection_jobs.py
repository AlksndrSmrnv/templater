"""Background batch LLM processing of a collection's templates with progress.

Replaces the old synchronous ``process_collection_llm`` loop (which held the
request open for minutes over a sequential ``for``): a click on «Обработать
всю коллекцию LLM» now creates a :class:`CollectionJob` row (``pending``),
kicks off an :func:`asyncio.create_task`, and returns immediately so the UI
can poll a progress bar. The background coroutine opens its own DB session
(lives independently of the request session) and runs the templates through
``asyncio.gather`` capped by a :class:`asyncio.Semaphore` — the global
:class:`~app.llm.coordinator.LLMCoordinator` still gates the actual GigaChat
HTTP calls, so this only raises *template* parallelism, not *request*
parallelism.

One active job per collection is enforced: ``find_active`` is a fast-path
check, and a partial unique index (``uq_collection_jobs_one_active``) is the
race-condition backstop — two strictly concurrent POSTs can't both insert.
Per-template outcomes bump ``processed``/``skipped``/``failed`` atomically (no
read-modify-write) so the polling endpoint never sees torn state. On process
restart :meth:`CollectionJobService.reconcile` rewrites any still-pending/
running rows to ``failed`` — the in-process task is gone.

.. note:: Single-worker assumption. The in-process :class:`JobRegistry` and
   ``asyncio.create_task`` model is correct only with one uvicorn worker. Under
   ``--workers > 1`` a task launched on worker A is invisible to worker B, and
   ``reconcile`` on B's startup would wrongly mark A's live running jobs as
   failed. The deployment runs a single worker (Dockerfile), so this is fine —
   but don't add ``--workers`` without moving jobs to a shared queue (arq/Celery).

A single :class:`~app.llm.client.GigaChatClient` is shared across all gather'd
coroutines of a job. This is safe: the SDK's sync ``chat`` (wrapped in
``asyncio.to_thread``) guards OAuth-token refresh with a ``threading.RLock``
(double-checked), the underlying ``httpx.Client`` is a thread-safe connection
pool, and retry state is local to each call. The :class:`LLMCoordinator`
semaphore still caps concurrent GigaChat HTTP calls globally.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import CollectionJob
from app.db.session import get_sessionmaker
from app.llm.runner import llm_service
from app.repositories.collection import CollectionRepository
from app.repositories.collection_job import CollectionJobRepository
from app.repositories.template import TemplateRepository
from app.services.templates import TemplateService
from app.utils.errors import IntegrityViolation, LLMUnavailable, NotFoundError, ValidationFailed

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _commit(session: AsyncSession) -> None:
    """Commit, rolling back on integrity violation (mirrors ``commit_or_409``
    without pulling a routes-layer helper into a service)."""

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise IntegrityViolation("Операция нарушает ограничения целостности") from exc


class JobRegistry:
    """In-process registry of live background jobs: ``job_id -> asyncio.Task``.

    Holds a strong reference to each task (so the GC doesn't reap it mid-run)
    and is the handle :func:`cancel_all` uses on shutdown. Single event loop →
    no lock needed for the dict itself."""

    _tasks: dict[uuid.UUID, asyncio.Task[None]] = {}

    @classmethod
    def register(cls, job_id: uuid.UUID, task: asyncio.Task[None]) -> None:
        cls._tasks[job_id] = task

    @classmethod
    def unregister(cls, job_id: uuid.UUID) -> None:
        cls._tasks.pop(job_id, None)

    @classmethod
    def get(cls, job_id: uuid.UUID) -> asyncio.Task[None] | None:
        return cls._tasks.get(job_id)

    @classmethod
    async def cancel_all(cls) -> None:
        """Best-effort cancel every live task on shutdown. Waits for the tasks
        to finish their ``finally`` blocks (LLM context cleanup, cert temp-file
        removal) before returning, so the engine isn't disposed under them."""

        tasks = list(cls._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        cls._tasks.clear()


async def _process_one_template(
    job_id: uuid.UUID,
    template_id: uuid.UUID,
    llm_svc: object,
    sem: asyncio.Semaphore,
) -> None:
    """Analyse one template under the batch semaphore and bump its counter.

    Each template gets its own short-lived session — ``AsyncSession`` is not
    concurrency-safe, so the gather'd coroutines must not share one. The
    counter increment is a separate atomic ``UPDATE`` on its own session, so
    two coroutines finishing simultaneously never race on the job row.
    """

    async with sem:
        outcome = "failed"
        try:
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                svc = TemplateService(session)
                template = await svc.get(template_id)
                if template is None:
                    # Deleted while the job was running — count as skipped, not
                    # a failure: nothing wrong happened, there's just nothing
                    # to do for this id anymore.
                    outcome = "skipped"
                else:
                    await svc.analyze_and_persist(template, llm_service=llm_svc)
                    await _commit(session)
                    outcome = "processed"
        except ValidationFailed:
            # Unparsable body (GET/urlencoded/GraphQL) — same skip semantics as
            # the old sequential loop (collections.py used to catch this).
            outcome = "skipped"
        except Exception:
            log.warning(
                "LLM analysis failed for template %s in job %s",
                template_id,
                job_id,
                exc_info=True,
            )
            outcome = "failed"

        # Bump the counter on its own session. A failure here (DB blip) must
        # NOT escape into asyncio.gather — that would abort the whole job and
        # mark it failed while sibling coroutines keep committing results. Log
        # and move on: the worst case is a count that's off by one, not a
        # cascading failure.
        try:
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as job_session:
                await CollectionJobRepository(job_session).increment(
                    job_id, **{outcome: 1}
                )
                await job_session.commit()
        except Exception:
            log.warning(
                "Failed to record %s outcome for template %s in job %s",
                outcome,
                template_id,
                job_id,
                exc_info=True,
            )


async def _run_job(job_id: uuid.UUID, collection_id: uuid.UUID) -> None:
    """Background coroutine: flip the job to ``running``, gather all templates
    in parallel under the batch semaphore, then mark ``done`` (or ``failed`` if
    the orchestrator itself blew up). The LLM client context is opened once for
    the whole job — the GigaChat cert temp-files live until the end and are
    cleaned up in the context's ``finally``."""

    sessionmaker = get_sessionmaker()
    settings = get_settings()
    try:
        # ``session`` here is only used by ``llm_service`` to load editable
        # prompt overrides from the DB once at context entry (runner.py); it is
        # not touched afterwards, so holding it for the job's lifetime is fine.
        async with sessionmaker() as override_session, llm_service(
            session=override_session
        ) as llm_svc:
            async with sessionmaker() as job_session:
                repo = CollectionJobRepository(job_session)
                await repo.mark_running(job_id, _utcnow())
                templates = await TemplateRepository(job_session).list_by_collection(
                    collection_id
                )
                await repo.set_total(job_id, len(templates))
                await job_session.commit()

            sem = asyncio.Semaphore(settings.llm_batch_concurrency)
            await asyncio.gather(
                *(
                    _process_one_template(job_id, t.id, llm_svc, sem)
                    for t in templates
                )
            )

            async with sessionmaker() as job_session:
                await CollectionJobRepository(job_session).mark_done(job_id, _utcnow())
                await job_session.commit()
    except Exception as exc:
        log.exception("Collection job %s failed", job_id)
        try:
            async with sessionmaker() as job_session:
                await CollectionJobRepository(job_session).mark_failed(
                    job_id,
                    str(exc) or exc.__class__.__name__,
                    _utcnow(),
                )
                await job_session.commit()
        except Exception:
            log.exception(
                "Failed to mark collection job %s as failed — it may stay running",
                job_id,
            )
    finally:
        JobRegistry.unregister(job_id)


class CollectionJobService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = CollectionJobRepository(session)

    async def start(self, collection_id: uuid.UUID) -> CollectionJob:
        """Create a ``pending`` job for the collection and launch the
        background coroutine. Refuses if LLM is not configured, the collection
        is missing, or a job is already active for it. Returns the new job row
        (already committed) so the route can render an initial progress partial.
        """

        if not get_settings().llm_active:
            raise LLMUnavailable("LLM не настроена (нет URL или сертификатов в .env)")
        collection = await CollectionRepository(self.session).get(collection_id)
        if collection is None:
            raise NotFoundError("Коллекция не найдена")
        # Fast-path check — gives a clean error without a constraint violation
        # in the common case. The partial unique index is the backstop for the
        # race where two concurrent POSTs both pass this check.
        if await self.repo.find_active(collection_id) is not None:
            raise ValidationFailed(
                "Обработка коллекции уже идёт — дождитесь завершения"
            )

        job = CollectionJob(collection_id=collection_id, status="pending", total=0)
        await self.repo.add(job)
        try:
            await _commit(self.session)
        except IntegrityViolation:
            # Lost the race to insert the active job — the partial unique index
            # (uq_collection_jobs_one_active) blocked us. Surface it as the
            # same user-facing "уже идёт" error the fast-path would have.
            raise ValidationFailed(
                "Обработка коллекции уже идёт — дождитесь завершения"
            ) from None

        task = asyncio.create_task(_run_job(job.id, collection_id))
        JobRegistry.register(job.id, task)
        return job

    async def get(self, job_id: uuid.UUID) -> CollectionJob | None:
        return await self.repo.get(job_id)

    @staticmethod
    async def reconcile() -> int:
        """On startup, fail any jobs left pending/running by a previous process
        (their in-process task died with it). Returns the reconciled row count."""

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            count = await CollectionJobRepository(session).reconcile(
                error="Прервано перезапуском сервера",
                finished_at=_utcnow(),
            )
            await session.commit()
            return count
