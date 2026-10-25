import asyncio
from datetime import timedelta
import random
from sqlalchemy import select, or_, and_
from ..archive.models import outbox, now
from ..core.errors import AppError

class Worker:
    def __init__(self, db, jitter=random.random): self.db, self.jitter = db, jitter

    def claim(self, worker_id, lease_seconds=120, batch_size=4):
        with self.db.engine.begin() as conn:
            query = select(outbox).where(outbox.c.attempts < 5, or_(and_(outbox.c.state=="pending",outbox.c.available_at<=now()),and_(outbox.c.state=="running",outbox.c.lease_until<now()))).order_by(outbox.c.available_at, outbox.c.created_at).with_for_update(skip_locked=True).limit(batch_size)
            rows = list(conn.execute(query).mappings())
            return [dict(conn.execute(outbox.update().where(outbox.c.id==row["id"]).values(state="running",worker_id=worker_id,attempts=row["attempts"]+1,lease_until=now()+timedelta(seconds=lease_seconds)).returning(outbox)).mappings().one()) for row in rows]

    def owned(self, conn, job_id, worker_id):
        row = conn.execute(select(outbox).where(outbox.c.id==job_id).with_for_update()).mappings().one()
        if row["worker_id"]!=worker_id or row["state"]!="running" or row["lease_until"] <= now():
            raise AppError("LEASE_LOST", "任务租约已失效", 409)
        return row

    def heartbeat(self, job_id, worker_id, checkpoint=None):
        with self.db.engine.begin() as conn:
            row = self.owned(conn,job_id,worker_id)
            payload = row["payload"]
            if checkpoint is not None: payload = {**payload,"checkpoint":checkpoint}
            conn.execute(outbox.update().where(outbox.c.id==job_id).values(lease_until=now()+timedelta(seconds=120),payload=payload))

    def finish(self, job_id, worker_id):
        with self.db.engine.begin() as conn:
            self.owned(conn,job_id,worker_id)
            conn.execute(outbox.update().where(outbox.c.id==job_id).values(state="completed",lease_until=None))

    def fail(self, job_id, worker_id, code):
        with self.db.engine.begin() as conn:
            row = self.owned(conn,job_id,worker_id)
            delay = min(300,5*2**(row["attempts"]-1)) + self.jitter()
            conn.execute(outbox.update().where(outbox.c.id==job_id).values(state="failed" if row["attempts"]>=5 else "pending",lease_until=None,available_at=now()+timedelta(seconds=delay),payload={**row["payload"],"last_error_code":code}))

    async def run_job(self, job_id, worker_id, handler):
        with self.db.engine.begin() as conn: job = dict(self.owned(conn,job_id,worker_id))
        stop = asyncio.Event()
        async def renew():
            while not stop.is_set():
                try: await asyncio.wait_for(stop.wait(),timeout=30)
                except TimeoutError: self.heartbeat(job_id,worker_id)
        heartbeat = asyncio.create_task(renew())
        try:
            await handler(job)
            self.finish(job_id,worker_id)
            return {"state":"completed"}
        except asyncio.CancelledError: raise
        except AppError as error:
            # A lease lost while the handler was running is not a job failure to record; the
            # new owner will retry it. Calling fail() here would itself raise LEASE_LOST.
            if error.code=="LEASE_LOST": return {"state":"lease_lost"}
            self.fail(job_id,worker_id,error.code)
            return {"state":"retry_or_failed","code":error.code}
        except Exception as error:
            code = error.code if isinstance(error,AppError) else "JOB_FAILED"
            self.fail(job_id,worker_id,code)
            return {"state":"retry_or_failed","code":code}
        finally:
            stop.set()
            await heartbeat
