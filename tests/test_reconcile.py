from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.models import Recording
from app.retention import RECONCILE_ERROR, reconcile_interrupted_recordings

CREATED = datetime(2026, 11, 4, 12, 0, 0)


async def test_reconcile_marks_interrupted_failed_and_removes_part_files(
    tmp_path: Path, session_factory
) -> None:
    storage = tmp_path / "data"
    part_dir = storage / "alice" / "2026-11-04"
    part_dir.mkdir(parents=True)

    async with session_factory() as session:
        pending = Recording(
            streamer_login="alice", status="pending", created_at=CREATED, path="", filename=""
        )
        assembling = Recording(
            streamer_login="alice", status="assembling", created_at=CREATED, path="", filename=""
        )
        ready = Recording(
            streamer_login="alice",
            status="ready",
            created_at=CREATED,
            path=str(part_dir / "120000_050_99.mp4"),
            filename="120000_050_99.mp4",
        )
        session.add_all([pending, assembling, ready])
        await session.commit()
        pending_id, assembling_id, ready_id = pending.id, assembling.id, ready.id

    pending_part = part_dir / f".120000_050_{pending_id}.mp4.part.mp4"
    assembling_part = part_dir / f".120000_050_{assembling_id}.mp4.part.mp4"
    ready_part = part_dir / f".120000_050_{ready_id}.mp4.part.mp4"
    for part in (pending_part, assembling_part, ready_part):
        part.write_bytes(b"partial")

    count = await reconcile_interrupted_recordings(
        {"storage_root": str(storage)}, session_factory
    )

    assert count == 2
    async with session_factory() as session:
        pending = await session.get(Recording, pending_id)
        assembling = await session.get(Recording, assembling_id)
        ready = await session.get(Recording, ready_id)
        assert pending.status == "failed"
        assert pending.error == RECONCILE_ERROR
        assert assembling.status == "failed"
        assert assembling.error == RECONCILE_ERROR
        assert ready.status == "ready"
        assert ready.error is None

    assert not pending_part.exists()
    assert not assembling_part.exists()
    assert ready_part.exists()


async def test_reconcile_without_interrupted_rows_is_noop(tmp_path: Path, session_factory) -> None:
    async with session_factory() as session:
        session.add(Recording(streamer_login="bob", status="ready", created_at=CREATED, path=""))
        await session.commit()
    assert await reconcile_interrupted_recordings({"storage_root": str(tmp_path)}, session_factory) == 0
