from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.db.models import CollectionJob
from app.services import collection_jobs as cj
from app.services.collection_jobs import CollectionJobService, JobRegistry, _run_job
from app.utils.errors import LLMUnavailable, NotFoundError, ValidationFailed

# --------------------------------------------------------------------------- #
# Shared fakes
# --------------------------------------------------------------------------- #


class _FakeSession:
    """Bare AsyncSession stand-in: only ``commit``/``rollback``/``add`` and the
    async-context-manager protocol are exercised — the repos are patched out.
    Doubles as the session object ``_FakeAsyncSessionFactory`` yields so
    ``async with sessionmaker() as session`` works inside ``_run_job``."""

    def __init__(self) -> None:
        self.committed = 0
        self.rolled_back = 0
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        self.rolled_back += 1

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeJobRepo:
    """In-memory CollectionJobRepository — records every call so the tests can
    assert on transitions and counters without a real DB."""

    jobs: dict[uuid.UUID, CollectionJob] = {}

    def __init__(self, session: object) -> None:
        self.session = session

    async def add(self, job: CollectionJob) -> CollectionJob:
        if job.id is None:
            job.id = uuid.uuid4()
        self.jobs[job.id] = job
        return job

    async def get(self, job_id: uuid.UUID) -> CollectionJob | None:
        return self.jobs.get(job_id)

    async def find_active(self, collection_id: uuid.UUID) -> CollectionJob | None:
        for job in self.jobs.values():
            if job.collection_id == collection_id and job.status in ("pending", "running"):
                return job
        return None

    async def mark_running(self, job_id: uuid.UUID, started_at: datetime) -> None:
        self.jobs[job_id].status = "running"
        self.jobs[job_id].started_at = started_at

    async def set_total(self, job_id: uuid.UUID, total: int) -> None:
        self.jobs[job_id].total = total

    async def increment(
        self,
        job_id: uuid.UUID,
        *,
        processed: int = 0,
        skipped: int = 0,
        failed: int = 0,
    ) -> None:
        job = self.jobs[job_id]
        job.processed += processed
        job.skipped += skipped
        job.failed += failed

    async def mark_done(self, job_id: uuid.UUID, finished_at: datetime) -> None:
        self.jobs[job_id].status = "done"
        self.jobs[job_id].finished_at = finished_at

    async def mark_failed(
        self, job_id: uuid.UUID, error: str, finished_at: datetime
    ) -> None:
        self.jobs[job_id].status = "failed"
        self.jobs[job_id].error = error
        self.jobs[job_id].finished_at = finished_at

    async def reconcile(self, *, error: str, finished_at: datetime) -> int:
        count = 0
        for job in self.jobs.values():
            if job.status in ("pending", "running"):
                job.status = "failed"
                job.error = error
                job.finished_at = finished_at
                count += 1
        return count


class _FakeCollectionRepo:
    def __init__(self, collection: SimpleNamespace | None) -> None:
        self.collection = collection

    async def get(self, collection_id: uuid.UUID) -> SimpleNamespace | None:
        return self.collection


class _FakeAsyncSessionFactory:
    """``async_sessionmaker`` lookalike: each call returns a fresh short-lived
    fake session. Used to patch ``get_sessionmaker`` so ``_run_job`` never
    touches a real engine."""

    def __call__(self) -> _FakeSession:
        return _FakeSession()


# --------------------------------------------------------------------------- #
# CollectionJobService.start
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolate_registry() -> Any:
    """Clear the in-process JobRegistry and the in-memory job store between
    tests so they don't leak state into each other."""
    JobRegistry._tasks.clear()
    _FakeJobRepo.jobs.clear()
    yield
    JobRegistry._tasks.clear()
    _FakeJobRepo.jobs.clear()


def _settings(*, llm_active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(llm_active=llm_active, llm_batch_concurrency=2)


def _make_job(
    collection_id: uuid.UUID, *, status: str = "pending", total: int = 0
) -> CollectionJob:
    """Build a CollectionJob with a concrete id and zeroed counters — without a
    flush SQLAlchemy leaves unset columns as ``None``, which would break the
    in-memory arithmetic in the fake repo."""
    return CollectionJob(
        id=uuid.uuid4(),
        collection_id=collection_id,
        status=status,
        total=total,
        processed=0,
        skipped=0,
        failed=0,
    )


async def _noop_run_job(job_id: uuid.UUID, collection_id: uuid.UUID) -> None:
    """Stand-in for the background coroutine so ``start`` tests don't launch
    real LLM/DB work. Still unregisters the task so the registry stays clean."""
    await asyncio.sleep(0)
    JobRegistry.unregister(job_id)


@pytest.mark.asyncio
async def test_start_rejects_when_llm_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cj, "get_settings", lambda: _settings(llm_active=False))
    svc = CollectionJobService(_FakeSession())  # type: ignore[arg-type]
    with pytest.raises(LLMUnavailable):
        await svc.start(uuid.uuid4())


@pytest.mark.asyncio
async def test_start_rejects_missing_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cj, "get_settings", lambda: _settings())
    monkeypatch.setattr(cj, "CollectionRepository", lambda session: _FakeCollectionRepo(None))
    monkeypatch.setattr(cj, "_run_job", _noop_run_job)
    svc = CollectionJobService(_FakeSession())  # type: ignore[arg-type]
    svc.repo = _FakeJobRepo(svc.session)  # type: ignore[assignment]
    with pytest.raises(NotFoundError):
        await svc.start(uuid.uuid4())


@pytest.mark.asyncio
async def test_start_rejects_when_job_already_active(monkeypatch: pytest.MonkeyPatch) -> None:
    collection_id = uuid.uuid4()
    monkeypatch.setattr(cj, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        cj, "CollectionRepository", lambda session: _FakeCollectionRepo(SimpleNamespace(id=collection_id))
    )
    monkeypatch.setattr(cj, "_run_job", _noop_run_job)
    svc = CollectionJobService(_FakeSession())  # type: ignore[arg-type]
    svc.repo = _FakeJobRepo(svc.session)  # type: ignore[assignment]
    # Seed an active job for this collection.
    _FakeJobRepo.jobs[uuid.uuid4()] = _make_job(
        collection_id, status="running", total=3
    )
    with pytest.raises(ValidationFailed):
        await svc.start(collection_id)


@pytest.mark.asyncio
async def test_start_creates_pending_job_and_registers_task(monkeypatch: pytest.MonkeyPatch) -> None:
    collection_id = uuid.uuid4()
    monkeypatch.setattr(cj, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        cj, "CollectionRepository", lambda session: _FakeCollectionRepo(SimpleNamespace(id=collection_id))
    )
    monkeypatch.setattr(cj, "_run_job", _noop_run_job)
    session = _FakeSession()
    svc = CollectionJobService(session)  # type: ignore[arg-type]
    svc.repo = _FakeJobRepo(session)  # type: ignore[assignment]

    job = await svc.start(collection_id)

    assert job.status == "pending"
    assert job.collection_id == collection_id
    assert job.total == 0
    assert session.committed == 1
    assert JobRegistry.get(job.id) is not None
    # Let the noop task finish (it unregisters itself at the end) so it doesn't
    # linger in the registry. Awaiting the task directly is deterministic —
    # ``sleep(0)`` only yields once and may not let the coroutine complete.
    task = JobRegistry.get(job.id)
    if task is not None:
        await task
    assert JobRegistry.get(job.id) is None


# --------------------------------------------------------------------------- #
# _run_job — the background coroutine
# --------------------------------------------------------------------------- #


class _FakeTplRepo:
    def __init__(self, templates: list[SimpleNamespace]) -> None:
        self.templates = templates

    async def list_by_collection(self, collection_id: uuid.UUID) -> list[SimpleNamespace]:
        return list(self.templates)


class _FakeTplService:
    """Records calls; behaviour per template id is configurable via ``behaviour``."""

    def __init__(self, templates: list[SimpleNamespace], behaviour: dict[uuid.UUID, str]) -> None:
        self.templates = templates
        self.behaviour = behaviour
        self.calls: list[uuid.UUID] = []

    async def get(self, template_id: uuid.UUID) -> SimpleNamespace | None:
        return next((t for t in self.templates if t.id == template_id), None)

    async def analyze_and_persist(
        self, template: SimpleNamespace, *, llm_service: Any | None = None
    ) -> SimpleNamespace:
        self.calls.append(template.id)
        mode = self.behaviour.get(template.id, "ok")
        if mode == "unparsable":
            raise ValidationFailed("unparsable body")
        if mode == "fail":
            raise RuntimeError("LLM blew up")
        return template


class _FakeLlmContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


def _patch_run_job_env(
    monkeypatch: pytest.MonkeyPatch,
    templates: list[SimpleNamespace],
    behaviour: dict[uuid.UUID, str],
) -> _FakeTplService:
    """Wire all the module-level dependencies ``_run_job`` reads so it runs
    fully in-memory. Returns the fake template service for call assertions."""

    monkeypatch.setattr(cj, "get_settings", lambda: _settings())
    monkeypatch.setattr(cj, "get_sessionmaker", lambda: _FakeAsyncSessionFactory())
    monkeypatch.setattr(cj, "llm_service", lambda *, session=None: _FakeLlmContext())
    monkeypatch.setattr(cj, "CollectionJobRepository", _FakeJobRepo)
    monkeypatch.setattr(cj, "TemplateRepository", lambda session: _FakeTplRepo(templates))
    fake_tpl_svc = _FakeTplService(templates, behaviour)
    monkeypatch.setattr(cj, "TemplateService", lambda session: fake_tpl_svc)
    return fake_tpl_svc


@pytest.mark.asyncio
async def test_run_job_processes_all_templates_and_marks_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection_id = uuid.uuid4()
    templates = [SimpleNamespace(id=uuid.uuid4()) for _ in range(3)]
    _patch_run_job_env(monkeypatch, templates, {})

    job = _make_job(collection_id)
    _FakeJobRepo.jobs[job.id] = job

    await _run_job(job.id, collection_id)

    assert job.status == "done"
    assert job.total == 3
    assert job.processed == 3
    assert job.skipped == 0
    assert job.failed == 0
    assert job.finished_at is not None
    assert job.started_at is not None


@pytest.mark.asyncio
async def test_run_job_skips_unparsable_and_counts_llm_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection_id = uuid.uuid4()
    ok = SimpleNamespace(id=uuid.uuid4())
    unparsable = SimpleNamespace(id=uuid.uuid4())
    failing = SimpleNamespace(id=uuid.uuid4())
    templates = [ok, unparsable, failing]
    _patch_run_job_env(
        monkeypatch,
        templates,
        {unparsable.id: "unparsable", failing.id: "fail"},
    )

    job = _make_job(collection_id)
    _FakeJobRepo.jobs[job.id] = job

    await _run_job(job.id, collection_id)

    assert job.status == "done"
    assert (job.processed, job.skipped, job.failed) == (1, 1, 1)
    assert job.total == 3


@pytest.mark.asyncio
async def test_run_job_skips_template_deleted_mid_run(monkeypatch: pytest.MonkeyPatch) -> None:
    collection_id = uuid.uuid4()
    present = SimpleNamespace(id=uuid.uuid4())
    # ``deleted`` is in the template list the orchestrator sees at start, but
    # ``FakeTplService.get`` won't find it — emulating a delete between the
    # total-count query and the per-template analysis.
    deleted = SimpleNamespace(id=uuid.uuid4())
    svc = _patch_run_job_env(monkeypatch, [present], {})  # only `present` is gettable
    # But the orchestrator's list_by_collection must still see both to set
    # total=2; override the repo to return the full list.
    monkeypatch.setattr(
        cj, "TemplateRepository", lambda session: _FakeTplRepo([present, deleted])
    )

    job = _make_job(collection_id)
    _FakeJobRepo.jobs[job.id] = job

    await _run_job(job.id, collection_id)

    assert job.status == "done"
    assert job.total == 2
    assert (job.processed, job.skipped, job.failed) == (1, 1, 0)
    # The deleted id was never sent to analyze_and_persist (get returned None).
    assert deleted.id not in svc.calls


@pytest.mark.asyncio
async def test_run_job_marks_failed_when_orchestrator_blows_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection_id = uuid.uuid4()
    templates = [SimpleNamespace(id=uuid.uuid4())]
    _patch_run_job_env(monkeypatch, templates, {})

    job = _make_job(collection_id)
    _FakeJobRepo.jobs[job.id] = job

    # Make the LLM context itself blow up — the orchestrator can't recover.
    class _BoomContext:
        async def __aenter__(self) -> object:
            raise RuntimeError("cert decode failed")

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(cj, "llm_service", lambda *, session=None: _BoomContext())

    await _run_job(job.id, collection_id)

    assert job.status == "failed"
    assert "cert decode failed" in (job.error or "")
    assert job.finished_at is not None


# --------------------------------------------------------------------------- #
# CollectionJobService.reconcile
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reconcile_fails_pending_and_running_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    pending = _make_job(uuid.uuid4())
    running = _make_job(uuid.uuid4(), status="running", total=2)
    done = _make_job(uuid.uuid4(), status="done", total=1)
    _FakeJobRepo.jobs[pending.id] = pending
    _FakeJobRepo.jobs[running.id] = running
    _FakeJobRepo.jobs[done.id] = done

    monkeypatch.setattr(cj, "get_sessionmaker", lambda: _FakeAsyncSessionFactory())
    monkeypatch.setattr(cj, "CollectionJobRepository", _FakeJobRepo)

    count = await CollectionJobService.reconcile()

    assert count == 2
    assert pending.status == "failed"
    assert running.status == "failed"
    assert done.status == "done"  # terminal jobs are left alone
    assert "Прервано" in (pending.error or "")


# --------------------------------------------------------------------------- #
# JobRegistry.cancel_all
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_job_registry_cancel_all_cancels_and_awaits() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _slow() -> None:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(_slow())
    job_id = uuid.uuid4()
    JobRegistry.register(job_id, task)
    await started.wait()

    await JobRegistry.cancel_all()

    assert cancelled.is_set()
    assert JobRegistry.get(job_id) is None
