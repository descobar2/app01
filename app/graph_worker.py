"""Reconcile pending Microsoft Graph operations every five minutes.

Run separately: python -m app.graph_worker
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import or_, select

from .graph import graph_configured
from .main import Audit, DbSession, SupportSession, now, sync_graph, utc

RETRY_INTERVAL = timedelta(minutes=5)
MAX_ATTEMPTS = 4  # initial request + 3 automatic retries


async def reconcile_once() -> int:
    if not graph_configured():
        return 0
    current = now()
    with DbSession() as db:
        pending = db.scalars(
            select(SupportSession).where(
                SupportSession.state.in_(("sync_pending", "cancel_pending")),
                SupportSession.graph_attempts < MAX_ATTEMPTS,
                SupportSession.starts_at > current,
                or_(SupportSession.graph_last_attempt.is_(None),
                    SupportSession.graph_last_attempt <= current - RETRY_INTERVAL),
            ).order_by(SupportSession.graph_last_attempt).limit(50)
        ).all()
        for session in pending:
            await sync_graph(db, session)
            if session.graph_attempts == MAX_ATTEMPTS and session.state in ("sync_pending", "cancel_pending"):
                db.add(Audit(action="graph_retries_exhausted", entity="session",
                             entity_id=str(session.id), detail=session.graph_error or "Requiere conciliación"))
                db.commit()
        return len(pending)


async def main() -> None:
    while True:
        try:
            await reconcile_once()
        except Exception as exc:
            # Worker continues; no tokens, credentials or response bodies in output.
            print(f"Reconciliador Graph: {type(exc).__name__}", flush=True)
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())