from __future__ import annotations

import argparse
import copy
import fcntl
import logging
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import ROOT, AppConfig
from .db import make_engine
from .integrations.errors import IntegrationError
from .models import Account, Content, Job, ModelCall, Publication, Schedule, WorkerHeartbeat, now, uid
from .services import AppError, acquire_write, enqueue
from .workflow import TaskContext, dispatch

log = logging.getLogger("yingxu.worker")
LEASE_SECONDS = 45


def _release_canceled_publication(session: Session, job: Job):
    if job.kind == "PREPARE_PUBLICATION":
        publication = session.get(Publication, job.payload["publication_id"])
        if publication and publication.status == "CANCELED":
            publication.reserved_serial = None


def _reset_content(session: Session, job: Job):
    if job.content_id and job.kind == "GENERATE_CONTENT":
        content = session.get(Content, job.content_id)
        if content and content.version == job.payload["version"]:
            content.status = (
                "REVIEW_REQUIRED"
                if content.video_asset_id and content.media_version == content.version
                else "DRAFT"
            )


def recover_expired(session: Session, queue: str, instant: datetime) -> int:
    expired = list(
        session.scalars(
            select(Job).where(
                Job.queue == queue,
                Job.status == "RUNNING",
                (Job.lease_until.is_(None) | (Job.lease_until <= instant.isoformat())),
            )
        )
    )
    for job in expired:
        completed_publication = (
            session.get(Publication, job.payload.get("publication_id"))
            if job.kind == "PREPARE_PUBLICATION"
            else None
        )
        calls = list(
            session.scalars(
                select(ModelCall).where(
                    ModelCall.job_id == job.id, ModelCall.status.in_(["STARTED", "UNKNOWN"])
                )
            )
        )
        for call in calls:
            call.status, call.error_code = "UNKNOWN", "WORKER_INTERRUPTED"
            events = copy.deepcopy(call.events)
            if events:
                events[-1].update({"status": "UNKNOWN", "completed_at": now()})
            call.events = events
        job.lease_owner = job.lease_until = None
        definitive_failure = bool(
            job.error_code
            and session.scalar(
                select(ModelCall.id).where(
                    ModelCall.job_id == job.id,
                    ModelCall.status == "FAILED",
                    ModelCall.error_code == job.error_code,
                )
            )
        )
        if (
            job.result.get("metric_id")
            or job.result.get("report_id")
            or (
                job.kind == "LOCAL_AI_TRANSCRIBE"
                and "transcript" in job.result
                and job.result.get("source_asset_id")
            )
            or (
                job.kind == "LOCAL_AI_PREVIEW"
                and job.result.get("asset_id")
                and job.result.get("local") is True
            )
            or (completed_publication and completed_publication.status == "PUBLISHED")
        ):
            job.status, job.current_step, job.progress = "SUCCEEDED", "已从持久化结果恢复完成状态", 100
        elif job.cancel_requested:
            job.status, job.current_step = "CANCELED", "已停止（中断恢复）"
            _release_canceled_publication(session, job)
            _reset_content(session, job)
        elif calls and all(call.kind == "video" and call.response.get("task_id") for call in calls):
            # A durable video task ID makes recovery a read-only poll, never another paid create.
            job.status, job.error_code, job.error_message = "QUEUED", None, None
            job.current_step = "继续查询已提交的视频任务"
        elif calls:
            job.status, job.error_code = "WAITING_USER", "MODEL_OUTCOME_UNKNOWN"
            job.error_message = "执行进程曾中断，模型调用结果需要核验；系统不会自动重发"
            job.current_step = "等待核验模型调用结果"
            _reset_content(session, job)
        elif definitive_failure:
            job.status, job.current_step = "FAILED", "服务商已明确拒绝请求；等待手动重试"
            _reset_content(session, job)
        elif queue == "device" and job.kind == "PREPARE_PUBLICATION":
            job.status, job.error_code = "WAITING_USER", "DEVICE_STATE_UNKNOWN"
            job.error_message = "设备任务中断，请先核验手机现场再继续；不会自动重复上传"
            job.current_step = "等待核验设备现场"
            if job.kind == "PREPARE_PUBLICATION":
                publication = session.get(Publication, job.payload["publication_id"])
                if publication and publication.status not in {"PUBLISHED", "CANCELED"}:
                    publication.status = (
                        "VERIFYING" if publication.evidence.get("submit_started_at") else "NEEDS_ATTENTION"
                    )
                    publication.evidence = {**publication.evidence, "reason": job.error_message}
        else:
            job.status, job.current_step = "QUEUED", "中断后恢复，复用已完成产物"
    return len(expired)


def scheduled_instant(day, wall_time: str, zone: ZoneInfo) -> datetime:
    hour, minute = (int(value) for value in wall_time.split(":"))
    # fold=0 executes once on the first occurrence of an autumn repeated time.
    # UTC round-trip shifts a spring nonexistent time forward by the gap.
    local = datetime.combine(day, datetime.min.time(), tzinfo=zone).replace(hour=hour, minute=minute, fold=0)
    return local.astimezone(UTC)


_UNFINISHED_JOB_STATUSES = ("QUEUED", "RUNNING", "WAITING_USER")


def _tick_collect(session, schedule: Schedule, instant: datetime) -> int:
    """Interval collection: runs on enable, then every interval; missed runs coalesce into one."""
    interval = timedelta(minutes=schedule.interval_minutes or 180)
    enabled_at = datetime.fromisoformat(schedule.enabled_at or schedule.created_at)
    due = datetime.fromisoformat(schedule.last_run_at) + interval if schedule.last_run_at else enabled_at
    if schedule.last_run_at and datetime.fromisoformat(schedule.last_run_at) < enabled_at:
        due = enabled_at
    if instant < due:
        if schedule.next_run_at != due.isoformat():
            schedule.next_run_at = due.isoformat()
        return 0
    unfinished = session.scalars(
        select(Job).where(Job.kind == "COLLECT_BATCH", Job.status.in_(_UNFINISHED_JOB_STATUSES))
    )
    if any(job.payload.get("schedule_id") == schedule.id for job in unfinished):
        schedule.next_run_at = (instant + timedelta(minutes=1)).isoformat()
        return 0
    slot = instant.replace(second=0, microsecond=0).isoformat()
    enqueue(
        session,
        kind="COLLECT_BATCH",
        payload={"schedule_id": schedule.id, "scheduled_at": slot, "include_paid": bool(schedule.include_paid)},
        key=f"schedule:{schedule.id}:{slot}:collect",
    )
    zone = ZoneInfo(schedule.timezone)
    schedule.last_scheduled_date = instant.astimezone(zone).date().isoformat()
    schedule.last_run_at = instant.isoformat()
    schedule.next_run_at = (instant + interval).isoformat()
    return 1


def tick_schedules(engine, instant: datetime | None = None) -> int:
    instant = instant or datetime.now(UTC)
    triggered = 0
    with Session(engine) as session:
        acquire_write(session)
        schedules = list(session.scalars(select(Schedule).where(Schedule.enabled.is_(True))))
        for schedule in schedules:
            if schedule.kind == "collect":
                triggered += _tick_collect(session, schedule, instant)
                continue
            zone = ZoneInfo(schedule.timezone)
            local_date = instant.astimezone(zone).date()
            today = scheduled_instant(local_date, schedule.time, zone)
            due_date = local_date if instant >= today else local_date - timedelta(days=1)
            due = scheduled_instant(due_date, schedule.time, zone)
            next_due = scheduled_instant(due_date + timedelta(days=1), schedule.time, zone)
            next_value = next_due.isoformat()
            if schedule.next_run_at != next_value:
                schedule.next_run_at = next_value
            enabled_at = datetime.fromisoformat(schedule.enabled_at or schedule.created_at)
            if due < enabled_at or schedule.last_scheduled_date == due_date.isoformat():
                continue
            date_key = due_date.isoformat()
            if schedule.kind == "pipeline":
                enqueue(
                    session,
                    kind="PIPELINE_BATCH",
                    payload={
                        "schedule_id": schedule.id,
                        "scheduled_date": date_key,
                        "daily_limit": schedule.daily_limit,
                    },
                    key=f"schedule:{schedule.id}:{date_key}:pipeline",
                )
            else:
                for publication in session.scalars(
                    select(Publication).where(Publication.status == "PUBLISHED")
                ):
                    enqueue(
                        session,
                        kind="COLLECT_METRICS",
                        queue="device",
                        content_id=publication.content_id,
                        payload={"publication_id": publication.id},
                        key=f"schedule:{schedule.id}:{date_key}:metrics:{publication.id}",
                    )
            schedule.last_scheduled_date, schedule.last_run_at = date_key, instant.isoformat()
            triggered += 1
        session.commit()
    return triggered


class Worker:
    def __init__(self, config: AppConfig, queue: str = "content", *, engine=None):
        if queue not in {"content", "device"}:
            raise ValueError("Unknown queue")
        self.config, self.queue = config, queue
        self.engine = engine if engine is not None else make_engine(config)
        self.owner = f"{queue}-{uid()}"

    def heartbeat(self, job_id: str | None = None) -> None:
        with Session(self.engine) as session:
            acquire_write(session)
            heartbeat = session.get(WorkerHeartbeat, self.queue)
            if heartbeat:
                heartbeat.worker_id, heartbeat.last_seen = self.owner, now()
            else:
                session.add(WorkerHeartbeat(queue=self.queue, worker_id=self.owner, last_seen=now()))
            if job_id:
                job = session.get(Job, job_id)
                if job and job.status == "RUNNING" and job.lease_owner == self.owner:
                    job.lease_until = (datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat()
            session.commit()

    def claim(self) -> Job | None:
        with Session(self.engine, expire_on_commit=False) as session:
            acquire_write(session)
            instant = datetime.now(UTC)
            recover_expired(session, self.queue, instant)
            if session.scalar(select(Job.id).where(Job.queue == self.queue, Job.status == "RUNNING")):
                session.commit()
                return None
            candidates = list(
                session.scalars(
                    select(Job)
                    .where(Job.queue == self.queue, Job.status == "QUEUED")
                    .order_by(Job.created_at)
                    .limit(100)
                )
            )
            chosen = None
            for job in candidates:
                if job.cancel_requested:
                    job.status = "CANCELED"
                    _release_canceled_publication(session, job)
                    _reset_content(session, job)
                    continue
                if job.kind == "COLLECT_METRICS":
                    publication = session.get(Publication, job.payload["publication_id"])
                    account = session.get(Account, publication.account_id) if publication else None
                    if account and session.scalar(
                        select(Publication.id).where(Publication.reserved_serial == account.device_serial)
                    ):
                        continue
                if job.kind == "PREPARE_PUBLICATION":
                    publication = session.get(Publication, job.payload["publication_id"])
                    if not publication or publication.status == "CANCELED":
                        job.status = "CANCELED"
                        _release_canceled_publication(session, job)
                        continue
                    if publication.status != "PREPARING" or publication.evidence.get("submit_started_at"):
                        job.status, job.error_code = "WAITING_USER", "PUBLICATION_OUTCOME_UNKNOWN"
                        job.current_step = "等待核验已有发布结果"
                        continue
                    serial = publication.snapshot.get("account", {}).get("device_serial")
                    if serial and session.scalar(
                        select(Publication.id).where(
                            Publication.reserved_serial == serial,
                            Publication.id != publication.id,
                        )
                    ):
                        continue
                    if not publication.reserved_serial:
                        publication.reserved_serial = serial
                chosen = job
                job.status, job.lease_owner = "RUNNING", self.owner
                job.lease_until = (instant + timedelta(seconds=LEASE_SECONDS)).isoformat()
                job.current_step, job.error_code, job.error_message = "开始执行", None, None
                break
            session.commit()
            return chosen

    def run_once(self) -> bool:
        self.heartbeat()
        job = self.claim()
        if not job:
            return False
        stopped = threading.Event()

        def maintain_lease():
            while not stopped.wait(3):
                try:
                    self.heartbeat(job.id)
                except Exception as error:  # noqa: BLE001 - heartbeat must not abandon the durable job on a transient error
                    log.warning("Heartbeat failed: %s", type(error).__name__)

        heartbeat_thread = threading.Thread(target=maintain_lease, daemon=True)
        heartbeat_thread.start()
        try:
            context = TaskContext(self.engine, self.config, job.id, self.owner)
            result = dispatch(context, job.kind)
            with Session(self.engine) as session:
                acquire_write(session)
                current = session.get(Job, job.id)
                if current.status != "RUNNING" or current.lease_owner != self.owner:
                    return True
                if current.cancel_requested and not (
                    current.result.get("metric_id")
                    or current.result.get("report_id")
                    or current.result.get("published_work_id")
                ):
                    current.status, current.current_step = "CANCELED", "已取消"
                    _release_canceled_publication(session, current)
                    _reset_content(session, current)
                else:
                    current.result = {**current.result, **result}
                    if current.kind == "PREPARE_PUBLICATION" and result.get("status") != "PUBLISHED":
                        current.status, current.progress, current.current_step = (
                            "WAITING_USER",
                            100 if result.get("status") == "WAITING_USER" else current.progress,
                            result["message"],
                        )
                    else:
                        current.status, current.progress, current.current_step = "SUCCEEDED", 100, "已完成"
                current.lease_owner = current.lease_until = None
                session.commit()
        except Exception as error:
            with Session(self.engine) as session:
                acquire_write(session)
                current = session.get(Job, job.id)
                if not current or current.status != "RUNNING" or current.lease_owner != self.owner:
                    return True
                if isinstance(error, (AppError, IntegrationError)):
                    code = error.code
                    message = getattr(error, "message", None) or str(error)
                else:
                    code, message = "TASK_FAILED", "任务执行失败，请查看运行记录"
                    log.exception("Job %s stopped with unexpected %s", job.id, type(error).__name__)
                if code == "DEVICE_BUSY":
                    # No phone action took place; wait for the inspector to release the local mutex.
                    current.status, current.current_step = "QUEUED", "等待设备检查结束"
                elif code == "LOCAL_AI_BUSY":
                    # Lock contention occurs before local inference, so a later claim is safe.
                    current.status, current.current_step = "QUEUED", "等待本地模型空闲"
                elif current.cancel_requested or code in {"TASK_CANCELED", "CANCELED"}:
                    current.status, current.current_step = "CANCELED", "已取消"
                    _release_canceled_publication(session, current)
                    _reset_content(session, current)
                else:
                    unknown_calls = list(
                        session.scalars(
                            select(ModelCall).where(
                                ModelCall.job_id == current.id, ModelCall.status.in_(["STARTED", "UNKNOWN"])
                            )
                        )
                    )
                    for call in unknown_calls:
                        call.status = "UNKNOWN"
                    current.status = (
                        "WAITING_USER" if unknown_calls or code == "MODEL_OUTCOME_UNKNOWN" else "FAILED"
                    )
                    current.error_code, current.error_message = code, message[:2000]
                    current.current_step = (
                        "等待核验模型调用" if current.status == "WAITING_USER" else "执行未完成"
                    )
                    if isinstance(error, AppError) and error.details:
                        current.result = {**current.result, "details": error.details}
                    _reset_content(session, current)
                    if current.kind == "PREPARE_PUBLICATION":
                        publication = session.get(Publication, current.payload["publication_id"])
                        if publication and publication.status not in {"PUBLISHED", "CANCELED"}:
                            publication.status = (
                                "VERIFYING"
                                if publication.evidence.get("submit_started_at")
                                else "NEEDS_ATTENTION"
                            )
                            publication.evidence = {
                                **publication.evidence,
                                "reason": message[:2000],
                                "error_code": code,
                            }
                            current.status = "WAITING_USER"
                current.lease_owner = current.lease_until = None
                session.commit()
            log.warning("Job %s stopped: %s", job.id, code)
        finally:
            stopped.set()
            heartbeat_thread.join(timeout=5)
            # Make an interrupted invocation immediately eligible for conservative recovery at next startup.
            with Session(self.engine) as session:
                current = session.get(Job, job.id)
                if current and current.status == "RUNNING" and current.lease_owner == self.owner:
                    current.lease_until = now()
                    session.commit()
        return True

    def serve(self):
        path = self.config.data_dir / "runs" / f"worker-{self.queue}.lock"
        with path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AppError("WORKER_ALREADY_RUNNING", f"{self.queue} 任务进程已经运行", 409) from None

            def stop(_signal, _frame):
                raise KeyboardInterrupt

            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            log.info("%s worker started", self.queue)
            last_tick = 0.0
            try:
                while True:
                    if self.queue == "content" and time.monotonic() - last_tick >= 15:
                        tick_schedules(self.engine)
                        last_tick = time.monotonic()
                    if not self.run_once():
                        time.sleep(2)
            except KeyboardInterrupt:
                log.info("%s worker stopped; unfinished work retained", self.queue)
            finally:
                with Session(self.engine) as session:
                    heartbeat = session.get(WorkerHeartbeat, self.queue)
                    if heartbeat and heartbeat.worker_id == self.owner:
                        session.delete(heartbeat)
                        session.commit()
                fcntl.flock(handle, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description="映序持久化任务进程")
    parser.add_argument("--queue", choices=["content", "device"], default="content")
    parser.add_argument("--once", action="store_true", help="处理一个待执行任务后退出")
    parser.add_argument("--workspace", type=Path, help="统一启动脚本使用的项目归属标识")
    args = parser.parse_args()
    if args.workspace and args.workspace.resolve() != ROOT:
        parser.error("项目归属标识与当前程序不一致")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx INFO records include complete query strings, including temporary signed media URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = AppConfig()
    config.configure_tools()
    worker = Worker(config, args.queue)
    if args.once:
        worker.run_once()
    else:
        worker.serve()


if __name__ == "__main__":
    main()
