from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, ModelCall, Schedule, now
from app.services import enqueue
from app.worker import Worker, tick_schedules


def add_job(engine, **fields):
    with Session(engine) as session:
        job = enqueue(
            session,
            kind=fields.pop("kind", "GENERATE_REPORT"),
            payload={},
            queue=fields.pop("queue", "content"),
        )
        for key, value in fields.items():
            setattr(job, key, value)
        session.commit()
        return job.id


def test_atomic_claim_allows_only_one_active_job_per_queue(engine, config):
    add_job(engine)
    add_job(engine)
    workers = [Worker(config, engine=engine), Worker(config, engine=engine)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda worker: worker.claim(), workers))
    assert sum(item is not None for item in claimed) == 1
    with Session(engine) as session:
        assert [job.status for job in session.scalars(select(Job).order_by(Job.created_at))] == [
            "RUNNING",
            "QUEUED",
        ]


def test_cancel_queued_job_prevents_dispatch(client, engine, config, monkeypatch):
    job_id = add_job(engine)
    assert client.post(f"/api/v1/jobs/{job_id}/cancel").json()["status"] == "CANCELED"
    monkeypatch.setattr(
        "app.worker.dispatch", lambda *_: (_ for _ in ()).throw(AssertionError("must not dispatch"))
    )
    assert Worker(config, engine=engine).run_once() is False


def test_interrupted_safe_task_is_reclaimed(engine, config):
    job_id = add_job(
        engine,
        status="RUNNING",
        lease_owner="dead-worker",
        lease_until=(datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
    )
    worker = Worker(config, engine=engine)
    job = worker.claim()
    assert job.id == job_id and job.status == "RUNNING" and job.lease_owner == worker.owner


def test_interrupted_paid_call_is_never_requeued(engine, config):
    job_id = add_job(
        engine,
        status="RUNNING",
        lease_owner="dead-worker",
        lease_until=(datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
    )
    with Session(engine) as session:
        session.add(
            ModelCall(
                job_id=job_id,
                request_key="a" * 64,
                kind="text",
                model="fixture",
                status="STARTED",
                events=[{"started_at": now(), "status": "STARTED"}],
            )
        )
        session.commit()
    assert Worker(config, engine=engine).claim() is None
    with Session(engine) as session:
        assert session.get(Job, job_id).status == "WAITING_USER"
        assert session.scalar(select(ModelCall)).status == "UNKNOWN"


def test_worker_marks_failure_with_safe_error_and_preserves_job(engine, config, monkeypatch):
    job_id = add_job(engine)
    monkeypatch.setattr(
        "app.worker.dispatch", lambda *_: (_ for _ in ()).throw(RuntimeError("secret body must not escape"))
    )
    assert Worker(config, engine=engine).run_once()
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job.status == "FAILED"
        assert "secret" not in job.error_message
        assert job.lease_owner is None


def test_recovery_does_not_rerun_completed_jobs(engine, config, monkeypatch):
    job_id = add_job(engine)
    called = []
    monkeypatch.setattr("app.worker.dispatch", lambda *_: called.append(True) or {"fixture": True})
    worker = Worker(config, engine=engine)
    assert worker.run_once() is True
    assert worker.run_once() is False
    assert called == [True]
    with Session(engine) as session:
        assert session.get(Job, job_id).status == "SUCCEEDED"


def schedule(engine, *, time="09:00", enabled_at="2026-01-01T00:00:00+00:00", last_date=None):
    with Session(engine) as session:
        item = Schedule(
            name="测试计划",
            kind="pipeline",
            time=time,
            timezone="America/New_York",
            enabled=True,
            enabled_at=enabled_at,
            daily_limit=1,
            last_scheduled_date=last_date,
        )
        session.add(item)
        session.commit()
        return item.id


def test_schedule_coalesces_missed_runs_and_is_idempotent(engine):
    identifier = schedule(engine)
    at = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
    assert tick_schedules(engine, at) == 1
    assert tick_schedules(engine, at) == 0
    with Session(engine) as session:
        jobs = list(session.scalars(select(Job)))
        assert len(jobs) == 1
        assert jobs[0].payload["scheduled_date"] == "2026-09-10"
        assert session.get(Schedule, identifier).last_scheduled_date == "2026-09-10"


def test_newly_enabled_schedule_does_not_execute_old_due_date(engine):
    schedule(engine, enabled_at="2026-09-10T12:00:00+00:00")
    assert tick_schedules(engine, datetime(2026, 9, 10, 12, 30, tzinfo=UTC)) == 0
    assert tick_schedules(engine, datetime(2026, 9, 10, 13, 0, tzinfo=UTC)) == 1


def test_dst_gap_and_fold_execute_only_once(engine):
    identifier = schedule(engine, time="02:30", last_date="2026-03-07")
    assert tick_schedules(engine, datetime(2026, 3, 8, 7, 15, tzinfo=UTC)) == 0
    assert tick_schedules(engine, datetime(2026, 3, 8, 7, 30, tzinfo=UTC)) == 1
    with Session(engine) as session:
        item = session.get(Schedule, identifier)
        item.time, item.last_scheduled_date = "01:30", "2026-10-31"
        session.commit()
    assert tick_schedules(engine, datetime(2026, 11, 1, 5, 30, tzinfo=UTC)) == 1
    assert tick_schedules(engine, datetime(2026, 11, 1, 6, 30, tzinfo=UTC)) == 0
