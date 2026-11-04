"""Supervisor: ein asyncio-Task je Streamer, Session-Lifecycle, Backoff."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from . import capacity
from .capture.buffer import RingBuffer, buffer_path, disk_allows_start, disk_should_stop
from .capture.recorder import RecorderError, SegmentRecorder
from .clip.assembler import ClipAssembler
from .config import AppConfig, get_config
from .db import get_sessionmaker
from .detect.detector import Detector, TriggerDecision
from .detect.signals import MODE_SETTLE_S, SessionSignals, sanitize_weights
from .logging_setup import log_hub
from .models import Recording, SignalSample, Streamer
from .runtime_settings import SettingsService, storage_path
from .timeutil import iso, utcnow
from .twitch.chat import ChatMessage, ChatMode, ChatReader
from .twitch.client import TwitchClient
from .twitch.gql import CLIP_BATCH_SIZE, chunk_logins
from .twitch.types import Channel, Clip, UserNotFound

logger = logging.getLogger(__name__)

MAX_BUFFER_S = 360
TRIGGER_BUFFER_RESET_S = 300
#: Cache gilt nach dem Dreifachen des Poll-Intervalls als veraltet.
CACHE_STALE_FACTOR = 3


class TriggerError(Exception):
    """Manueller Trigger nicht möglich."""


@dataclass(slots=True)
class StreamerRef:
    id: int
    login: str
    display_name: str
    enabled: bool
    twitch_user_id: str | None = None


@dataclass
class RuntimeState:
    streamer_id: int
    login: str
    state: str = "offline"
    viewers: int = 0
    stream_started_at: datetime | None = None
    buffer_s: float = 0.0
    last_score: float = 0.0
    last_components: dict[str, float] = field(default_factory=dict)
    last_detail: dict[str, Any] = field(default_factory=dict)
    last_tick_at: datetime | None = None
    last_error: str | None = None
    chat_connected: bool = False
    chat_mode: str = ""
    signals_suppressed_s: float = 0.0
    cooldown_remaining_s: float = 0.0
    session_started_at: datetime | None = None
    stream_id: str | None = None
    recording_id: int | None = None
    queue_position: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "streamer_id": self.streamer_id,
            "login": self.login,
            "state": self.state,
            "viewers": self.viewers,
            "stream_started_at": iso(self.stream_started_at),
            "buffer_s": round(self.buffer_s, 1),
            "last_score": round(self.last_score, 4),
            "last_components": self.last_components,
            "last_detail": self.last_detail,
            "last_tick_at": iso(self.last_tick_at),
            "last_error": self.last_error,
            "chat_connected": self.chat_connected,
            "chat_mode": self.chat_mode,
            "signals_suppressed_s": round(self.signals_suppressed_s, 1),
            "cooldown_remaining_s": round(self.cooldown_remaining_s, 1),
            "session_started_at": iso(self.session_started_at),
            "stream_id": self.stream_id,
            "recording_id": self.recording_id,
            "queue_position": self.queue_position,
        }


class LiveSession:
    """Eine Live-Aufnahme inkl. Chat, Signal-Ticks und Zusammenbau."""

    def __init__(
        self,
        supervisor: "Supervisor",
        streamer: StreamerRef,
        stream_id: str,
        settings: dict[str, Any],
    ) -> None:
        self.supervisor = supervisor
        self.streamer = streamer
        self.stream_id = stream_id
        self.settings = settings
        self.storage_root = storage_path(settings)
        self.buffer_dir = buffer_path(self.storage_root, streamer.login, stream_id)
        self.recorder: SegmentRecorder | None = None
        self.ring = RingBuffer(self.buffer_dir, keep_s=MAX_BUFFER_S)
        self.signals = SessionSignals()
        self.detector = Detector(
            threshold=float(settings.get("threshold") or 0.60),
            cooldown_s=int(settings.get("cooldown_s") or 180),
            pre_roll_s=int(settings.get("pre_roll_s") or 240),
            weights=sanitize_weights(settings.get("weights")),
        )
        self.chat: ChatReader | None = None
        self.runtime = supervisor.states.setdefault(
            streamer.id, RuntimeState(streamer_id=streamer.id, login=streamer.login)
        )
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._assemblies: set[asyncio.Task[None]] = set()
        self._assembly_lock = asyncio.Lock()
        self._assembly_pending = False
        self._trigger_lock = asyncio.Lock()
        self._stopped = False

    # -- Lifecycle ----------------------------------------------------------
    async def run(self) -> None:
        settings = await self.supervisor.settings_service.get(force=True)
        self.settings = settings
        self.storage_root = storage_path(settings)
        self.buffer_dir = buffer_path(self.storage_root, self.streamer.login, self.stream_id)
        self.ring = RingBuffer(self.buffer_dir, keep_s=MAX_BUFFER_S)

        if not disk_allows_start(self.storage_root):
            raise RecorderError(
                f"Zu wenig freier Speicher unter {self.storage_root} (< 2 GB); Aufnahme nicht gestartet."
            )

        self.runtime.state = "connecting"
        self.runtime.stream_id = self.stream_id
        self.runtime.last_error = None
        self.recorder = SegmentRecorder(
            login=self.streamer.login,
            quality=str(settings.get("quality") or "best"),
            buffer_dir=self.buffer_dir,
        )
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.supervisor.active_buffer_dirs.add(self.buffer_dir)

        try:
            await self.recorder.start()
        except RecorderError as exc:
            self.runtime.state = "error"
            self.runtime.last_error = str(exc)
            await self.recorder.stop()
            logger.warning("Aufnahme %s: %s", self.streamer.login, exc)
            raise

        self.runtime.state = "live"
        self.runtime.session_started_at = self.recorder.started_at
        self.runtime.buffer_s = 0.0
        self.signals = SessionSignals(started_at=utcnow())
        self.detector = Detector(
            threshold=float(settings.get("threshold") or 0.60),
            cooldown_s=int(settings.get("cooldown_s") or 180),
            pre_roll_s=int(settings.get("pre_roll_s") or 240),
            weights=sanitize_weights(settings.get("weights")),
        )
        logger.info(
            "LIVE: %s (Stream %s), Puffer %s",
            self.streamer.login,
            self.stream_id,
            self.buffer_dir,
        )

        self.chat = ChatReader(self.streamer.login, self._on_chat, on_mode=self._on_mode)
        self.chat.start()
        self._tasks = [
            asyncio.create_task(self._tick_loop(), name=f"tick:{self.streamer.login}"),
            asyncio.create_task(self._channel_loop(), name=f"channel:{self.streamer.login}"),
            asyncio.create_task(self._clip_loop(), name=f"clips:{self.streamer.login}"),
        ]
        loop = asyncio.get_running_loop()
        last_media_end = -1.0
        last_change = loop.time()
        try:
            while not self._stop.is_set():
                if self.recorder is None or not (self.recorder.ffmpeg_alive or self.recorder.alive):
                    if not self._stop.is_set():
                        logger.info("Aufnahme beendet: %s", self.streamer.login)
                    break
                media_end = self.recorder.media_end
                self.runtime.buffer_s = self.recorder.buffer_seconds(MAX_BUFFER_S)
                if media_end > last_media_end + 0.01:
                    last_media_end = media_end
                    last_change = loop.time()
                elif loop.time() - last_change > 90:
                    message = "Keine neuen Segmente seit 90 s – Aufnahme wird neu gestartet."
                    logger.warning("%s (%s)", message, self.streamer.login)
                    self.runtime.last_error = message
                    break
                await asyncio.sleep(1.0)
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.chat is not None:
            await self.chat.stop()
            self.chat = None
        if self.recorder is not None:
            await self.recorder.stop()
        # Laufende Zusammenbauten kurz auslaufen lassen (Stream-Ende ⇒ truncated).
        if self._assemblies:
            done, pending = await asyncio.wait(self._assemblies, timeout=90)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self.supervisor.active_buffer_dirs.discard(self.buffer_dir)
        if self.runtime.state in ("live", "connecting"):
            self.runtime.state = "offline"
            self.runtime.buffer_s = 0.0
        # Puffer dieser Session entfernen; fertige Aufnahmen liegen woanders.
        from .capture.buffer import remove_buffer_dir

        remove_buffer_dir(self.buffer_dir)

    # -- Signale ------------------------------------------------------------
    def _on_chat(self, message: ChatMessage) -> None:
        self.signals.on_chat(message)

    def _on_mode(self, mode: ChatMode) -> None:
        now = utcnow()
        self.signals.note_mode(mode, now)
        self.runtime.chat_mode = mode.label
        self.runtime.signals_suppressed_s = self._mode_suppression(now)

    def _mode_suppression(self, now: datetime) -> float:
        """Restzeit des 120-s-Settles nach einem Moduswechsel (0 = vorbei)."""
        changed_at = self.signals.mode_changed_at
        if changed_at is None:
            return 0.0
        remaining = MODE_SETTLE_S - (now - changed_at).total_seconds()
        return max(0.0, float(remaining))

    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Signal-Tick fehlgeschlagen (%s)", self.streamer.login)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> None:
        if self.recorder is None:
            return
        now = utcnow()
        settings = await self.supervisor.settings_service.get()
        self.detector.configure(
            threshold=float(settings.get("threshold") or 0.60),
            cooldown_s=int(settings.get("cooldown_s") or 180),
            pre_roll_s=int(settings.get("pre_roll_s") or 240),
            weights=sanitize_weights(settings.get("weights")),
        )
        components, detail = self.signals.snapshot(now)
        buffer_s = self.recorder.buffer_seconds(MAX_BUFFER_S)
        decision = self.detector.update(
            components,
            now=now,
            buffer_s=buffer_s,
            anchor_ts=_anchor_from_detail(detail),
        )

        self.runtime.buffer_s = buffer_s
        self.runtime.last_score = decision.score
        self.runtime.last_components = components
        self.runtime.last_detail = detail
        self.runtime.last_tick_at = now
        self.runtime.cooldown_remaining_s = self.detector.cooldown_remaining(now)
        self.runtime.chat_connected = bool(self.chat and self.chat.connected)
        self.runtime.signals_suppressed_s = self._mode_suppression(now)

        await self._save_signal(now, decision.score, components)

        # Disk-Guard: alten Pufferpfad UND aktuellen Zielpfad prüfen, damit ein
        # Speicherort-Wechsel den Guard nicht auf dem falschen Volume laufen lässt.
        target_root = storage_path(settings)
        guarded_roots = [self.storage_root]
        if target_root != self.storage_root:
            guarded_roots.append(target_root)
        for root in guarded_roots:
            if disk_should_stop(root):
                message = f"Weniger als 1 GB frei unter {root} – Aufnahme wird beendet."
                logger.error("%s (%s)", message, self.streamer.login)
                self.runtime.last_error = message
                self._stop.set()
                return

        # Puffer regelmäßig beschneiden und den Recorder-Zustand mitziehen.
        try:
            removed = await self.ring.prune(self.recorder.segments(), self.recorder.media_end)
            if removed:
                self.recorder.forget([segment.name for segment in removed])
        except Exception:  # noqa: BLE001
            logger.debug("Puffer-Pruning fehlgeschlagen", exc_info=True)

        if decision.trigger:
            logger.info(
                "Trigger erkannt: %s score=%.3f (%s)",
                self.streamer.login,
                decision.score,
                decision.reason,
            )
            try:
                await self.trigger("auto", decision, manual=False)
            except TriggerError as exc:
                logger.warning("Trigger nicht ausgeführt (%s): %s", self.streamer.login, exc)
            except Exception:  # noqa: BLE001
                logger.exception("Trigger-Verarbeitung fehlgeschlagen (%s)", self.streamer.login)

    async def _save_signal(self, now: datetime, score: float, components: dict[str, float]) -> None:
        try:
            async with get_sessionmaker()() as session:
                session.add(
                    SignalSample(
                        streamer_id=self.streamer.id,
                        ts=now,
                        score=float(score),
                        components=components,
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.debug("SignalSample konnte nicht gespeichert werden", exc_info=True)

    async def _channel_loop(self) -> None:
        offline_misses = 0
        last_version = -1
        timeout = max(1.0, float(self.supervisor.config.live_poll_s))
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=timeout)
                break
            except asyncio.TimeoutError:
                pass
            try:
                channel = await self.supervisor.cached_channel(self.streamer.login)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Live-Check %s fehlgeschlagen: %s", self.streamer.login, exc)
                continue
            # Nur echte, neue Cache-Snapshots zählen: Ein unveränderter Cache
            # (z. B. Twitch down) darf die Offline-Erkennung nicht auslösen.
            version = self.supervisor.cache_version
            if version == last_version:
                continue
            last_version = version
            if channel is None:
                # Unbekannter/fehlender Cache-Eintrag ist kein Offline-Signal.
                offline_misses = 0
                continue
            if not channel.is_live:
                offline_misses += 1
                if offline_misses >= 2:
                    logger.info("Stream offline: %s", self.streamer.login)
                    self._stop.set()
                    break
                continue
            offline_misses = 0
            self.runtime.viewers = channel.viewers
            self.runtime.stream_started_at = channel.started_at
            self.runtime.buffer_s = self.recorder.buffer_seconds(MAX_BUFFER_S) if self.recorder else 0.0
            self.signals.add_viewers(channel.viewers, utcnow())
            await self.supervisor._touch_live(self.streamer.id)

    async def _clip_loop(self) -> None:
        timeout = max(1.0, float(self.supervisor.config.clip_poll_s))
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=timeout)
                break
            except asyncio.TimeoutError:
                pass
            try:
                clips = await self.supervisor.cached_clips(self.streamer.login)
                fresh = self.signals.add_clips(clips, utcnow())
                if fresh:
                    logger.info("%d neue(r) Clip(s) bei %s erkannt", len(fresh), self.streamer.login)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("Clip-Poll %s fehlgeschlagen: %s", self.streamer.login, exc)

    # -- Trigger / Zusammenbau ---------------------------------------------
    async def trigger(
        self,
        kind: str,
        decision: TriggerDecision | None = None,
        manual: bool = True,
        reason: str | None = None,
    ) -> int:
        async with self._trigger_lock:
            if self.recorder is None:
                raise TriggerError("Keine aktive Aufnahme.")
            settings = await self.supervisor.settings_service.get(force=True)
            pre_roll = int(settings.get("pre_roll_s") or 240)
            if self.recorder.buffer_seconds(MAX_BUFFER_S) < pre_roll:
                raise TriggerError(
                    f"Puffer enthält erst {self.recorder.buffer_seconds(MAX_BUFFER_S):.0f} s, "
                    f"benötigt werden {pre_roll} s."
                )
            if self._assembly_lock.locked() or self._assembly_pending:
                raise TriggerError("Für diesen Streamer läuft bereits ein Zusammenbau.")
            target_root = storage_path(settings)
            if not disk_allows_start(target_root):
                raise TriggerError(
                    f"Zu wenig freier Speicher unter {target_root} (< 2 GB); "
                    "Aufnahme nicht gestartet."
                )

            now = utcnow()
            # Medienzeit aus der CSV-Zeitbasis, nicht aus der Wanduhr: Bei einem
            # Stall würde sonst das Vorlauf-Fenster aus dem Puffer rutschen.
            media_trigger = max(0.0, self.recorder.media_time(now))
            media_end = self.recorder.media_end
            if media_end > 0:
                media_trigger = min(media_trigger, media_end)
            window_start = max(0.0, media_trigger - pre_roll)
            started_at = self.recorder.wall_time(window_start)
            ended_at = self.recorder.wall_time(media_trigger + int(settings.get("post_roll_s") or 60))
            score = float(decision.score if decision else self.detector.last_score)
            components = dict(decision.components if decision else self.detector.last_components)
            # Detailstand im Trigger-Moment einfrieren; der Zusammenbau läuft
            # erst nach dem Nachlauf und darf keinen neueren Tick verwenden.
            detail_snapshot = copy.deepcopy(self.runtime.last_detail or {})
            trigger_info: dict[str, Any] = {
                "kind": kind,
                "reason": reason
                or (decision.reason if decision else "Manuell ausgelöst"),
                "score": round(score, 4),
                "threshold": float(settings.get("threshold") or 0.60),
                "components": components,
                "components_detail": detail_snapshot,
                "weights": sanitize_weights(settings.get("weights")),
                "notes": list(decision.notes) if decision else [],
                "buffer_s": round(self.recorder.buffer_seconds(MAX_BUFFER_S), 1),
                "anchor": _anchor_iso(decision.anchor_ts if decision else None),
                "media_trigger_s": round(media_trigger, 3),
                "stream_id": self.stream_id,
                "session_started_at": iso(self.recorder.started_at),
                "chat_mode": self.runtime.chat_mode,
            }

            async with get_sessionmaker()() as session:
                recording = Recording(
                    streamer_id=self.streamer.id,
                    streamer_login=self.streamer.login,
                    started_at=started_at,
                    ended_at=ended_at,
                    score=score,
                    trigger=trigger_info,
                    status="pending",
                    created_at=now,
                )
                session.add(recording)
                await session.commit()
                recording_id = recording.id

            if manual:
                # Auch ein manueller Trigger belegt den Cooldown, damit der
                # Detektor nicht direkt danach dieselbe Szene erneut aufnimmt.
                self.detector.mark_triggered(now)

            self.runtime.recording_id = recording_id
            self._assembly_pending = True
            task = asyncio.create_task(
                self._assemble(recording_id, trigger_info, kind),
                name=f"assemble:{self.streamer.login}:{recording_id}",
            )
            self._assemblies.add(task)
            task.add_done_callback(self._assemblies.discard)
            return recording_id

    async def _assemble(self, recording_id: int, trigger_info: dict[str, Any], kind: str) -> None:
        async with self._assembly_lock:
            await _update_recording(recording_id, status="assembling")
            try:
                settings = await self.supervisor.settings_service.get(force=True)
                pre_roll = int(settings.get("pre_roll_s") or 240)
                post_roll = int(settings.get("post_roll_s") or 60)
                storage_root = storage_path(settings)
                media_trigger = float(trigger_info.get("media_trigger_s") or 0.0)
                if media_trigger <= 0 and self.recorder is not None:
                    media_trigger = max(0.0, self.recorder.media_time(utcnow()))
                    if self.recorder.media_end > 0:
                        media_trigger = min(media_trigger, self.recorder.media_end)
                result = await self.supervisor.assembler.build(
                    recording_id=recording_id,
                    login=self.streamer.login,
                    stream_id=self.stream_id,
                    buffer_dir=self.buffer_dir,
                    recorder=self.recorder,
                    ring_buffer=self.ring,
                    storage_root=storage_root,
                    media_trigger=media_trigger,
                    pre_roll_s=pre_roll,
                    post_roll_s=post_roll,
                    score=float(trigger_info.get("score") or 0.0),
                    components=trigger_info.get("components") or {},
                    weights=trigger_info.get("weights") or {},
                    trigger_info=trigger_info,
                    components_detail=trigger_info.get("components_detail") or {},
                )
                if result.status in ("ready", "truncated") and result.path is not None:
                    await _update_recording(
                        recording_id,
                        status=result.status,
                        path=str(result.path),
                        filename=result.path.name,
                        duration_s=result.duration_s,
                        size_bytes=result.size_bytes,
                        thumbnail_path=str(result.thumbnail_path) if result.thumbnail_path else None,
                        started_at=result.window_start,
                        ended_at=result.window_end,
                        error=None,
                    )
                    logger.info(
                        "Aufnahme fertig: %s (%s, %.1fs, %s)",
                        result.path.name,
                        result.status,
                        result.duration_s,
                        self.streamer.login,
                    )
                else:
                    await _update_recording(recording_id, status="failed", error=result.error or "Unbekannter Fehler")
                    logger.error("Zusammenbau fehlgeschlagen (%s): %s", self.streamer.login, result.error)
                # Puffer nach dem Zusammenbau auf 300 s zurückschneiden.
                if self.recorder is not None:
                    removed = await self.ring.prune(
                        self.recorder.segments(),
                        self.recorder.media_end,
                        keep_s=TRIGGER_BUFFER_RESET_S,
                    )
                    if removed:
                        self.recorder.forget([segment.name for segment in removed])
            except asyncio.CancelledError:
                await _update_recording(recording_id, status="failed", error="Dienst wurde beendet.")
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Zusammenbau abgebrochen (%s)", self.streamer.login)
                await _update_recording(recording_id, status="failed", error=str(exc)[:1000])
            finally:
                self._assembly_pending = False
                self.runtime.recording_id = None


def _anchor_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    try:
        return iso(datetime.fromtimestamp(float(timestamp), tz=timezone.utc).replace(tzinfo=None))
    except (TypeError, ValueError, OSError):
        return None


def _anchor_from_detail(detail: dict[str, Any]) -> float | None:
    clip_detail = detail.get("clip_created") or {}
    anchor = clip_detail.get("anchor") if isinstance(clip_detail, dict) else None
    try:
        return float(anchor) if anchor is not None else None
    except (TypeError, ValueError):
        return None


async def _update_recording(recording_id: int, **values: Any) -> None:
    try:
        async with get_sessionmaker()() as session:
            recording = await session.get(Recording, recording_id)
            if recording is None:
                return
            for key, value in values.items():
                setattr(recording, key, value)
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Recording %s konnte nicht aktualisiert werden", recording_id)


class Supervisor:
    def __init__(
        self,
        twitch: TwitchClient,
        settings_service: SettingsService,
        assembler: ClipAssembler | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.twitch = twitch
        self.settings_service = settings_service
        self.assembler = assembler or ClipAssembler()
        self.config = config or get_config()
        self.states: dict[int, RuntimeState] = {}
        self.sessions: dict[int, LiveSession] = {}
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self.active_buffer_dirs: set[Path] = set()

        # Zentraler Poll-Cache (Login -> Channel/Clips). Alle Sessions lesen
        # hier, damit pro Zyklus nur ein Batch je 15 Streamer an Twitch geht.
        # Clips haben einen eigenen, kleineren Poll (CLIP_BATCH_SIZE).
        self.channel_cache: dict[str, Channel] = {}
        self.clips_cache: dict[str, list[Clip]] = {}
        self.cache_updated_at = 0.0
        self.cache_version = 0
        self.gql_requests_last_cycle = 0
        self.clip_requests_last_cycle = 0
        self.enabled_logins: list[str] = []
        self.total_streamers = 0
        self._cache_lock = asyncio.Lock()
        self._last_refresh_attempt = 0.0

        self._reconcile_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._clip_poll_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # -- Start/Stop ---------------------------------------------------------
    async def start(self) -> None:
        await self._warn_memory_guard_start()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="supervisor-poll")
        self._clip_poll_task = asyncio.create_task(
            self._clip_poll_loop(), name="supervisor-clip-poll"
        )
        self._reconcile_task = asyncio.create_task(self._reconcile_loop(), name="supervisor-reconcile")
        logger.info("Supervisor gestartet")

    async def _warn_memory_guard_start(self) -> None:
        """Warnt einmalig, wenn der Guard nur über den Host-Fallback greift."""
        _used, limit = capacity.memory_usage()
        if limit is not None:
            return
        available, _total = capacity.host_memory()
        if available is None:
            return
        try:
            settings = await self.settings_service.get()
        except Exception:  # noqa: BLE001
            settings = {}
        streams = capacity.max_concurrent_streams(settings)
        # Nur warnen, wenn der freie Host-RAM für die konfigurierte Grenze
        # tatsächlich knapp ist (Richtwert ~120 MB je Stream plus Reserve).
        needed_mb = streams * 120 + 512
        available_mb = available // capacity.MB
        if available_mb >= needed_mb:
            return
        logger.warning(
            "Host hat nur %.1f GB verfügbar; %d Streams brauchen ~%.1f GB – Limit prüfen.",
            available / (1024**3),
            streams,
            needed_mb / 1024,
        )

    async def stop(self) -> None:
        self._stop.set()
        for task_attr in ("_poll_task", "_clip_poll_task", "_reconcile_task"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                setattr(self, task_attr, None)
        # Sessions zuerst stoppen, damit Prozesse sauber beendet werden.
        for session in list(self.sessions.values()):
            with contextlib.suppress(Exception):
                await session.stop()
        for task in list(self.tasks.values()):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.sessions.clear()
        logger.info("Supervisor gestoppt")

    # -- Reconcile ----------------------------------------------------------
    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Supervisor-Reconcile fehlgeschlagen")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    async def _reconcile(self) -> None:
        async with get_sessionmaker()() as session:
            rows = list((await session.execute(select(Streamer))).scalars())
        enabled_ids = {row.id for row in rows if row.enabled}
        self.enabled_logins = [row.login for row in rows if row.enabled]
        self.total_streamers = len(rows)

        for row in rows:
            if not row.enabled:
                continue
            task = self.tasks.get(row.id)
            if task is None or task.done():
                self.states.setdefault(row.id, RuntimeState(streamer_id=row.id, login=row.login))
                self.tasks[row.id] = asyncio.create_task(
                    self._run_streamer(row.id), name=f"streamer:{row.login}"
                )

        for streamer_id in list(self.tasks):
            if streamer_id not in enabled_ids:
                task = self.tasks.pop(streamer_id)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                session = self.sessions.pop(streamer_id, None)
                if session is not None:
                    with contextlib.suppress(Exception):
                        await session.stop()
                state = self.states.get(streamer_id)
                if state is not None:
                    state.state = "offline"
                    state.queue_position = None
        self._refresh_queue_positions()

    # -- Zentraler Poll-Cache ----------------------------------------------
    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Zentraler Poll-Zyklus fehlgeschlagen")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(1.0, float(self.config.live_poll_s))
                )
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self) -> None:
        """Ein Batch-Zyklus für alle aktivierten Streamer."""
        logins = await self._load_enabled_logins()
        self.enabled_logins = logins
        self._last_refresh_attempt = time.monotonic()
        if not logins:
            async with self._cache_lock:
                self._evict_disabled(set())
            self.cache_updated_at = time.monotonic()
            self.cache_version += 1
            self.gql_requests_last_cycle = 0
            self.clip_requests_last_cycle = 0
            return
        async with self._cache_lock:
            await self._refresh_cache(logins)

    async def _clip_poll_loop(self) -> None:
        """Separater Clip-Poll in eigenem Intervall (``clip_poll_s``)."""
        while not self._stop.is_set():
            try:
                await self._clip_poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Clip-Poll-Zyklus fehlgeschlagen")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(1.0, float(self.config.clip_poll_s))
                )
            except asyncio.TimeoutError:
                pass

    async def _clip_poll_once(self) -> None:
        """Holt Clips nur für aktuell live gemeldete Streamer.

        Chunks laufen über den Client (``CLIP_BATCH_SIZE``). Die HTTP-Anfragen
        passieren bewusst **außerhalb** von ``_cache_lock``, damit ein langer
        Clip-Poll den Live-Poll nicht blockiert. Bei einem Chunk-Fehler bleibt
        der letzte bekannte Clip-Cache stehen; der Loop wartet anschließend
        das normale Intervall ab (kein Tight-Loop).
        """
        async with self._cache_lock:
            live_logins = sorted(
                login for login, channel in self.channel_cache.items() if channel.is_live
            )
        if not live_logins:
            self.clip_requests_last_cycle = 0
            return
        fetch = getattr(self.twitch, "fetch_clips_batch", None)
        # Clip-POSTs je Zyklus: ein POST pro Chunk (bzw. je Login im Fallback).
        # Der Live-Zähler bleibt davon unberührt (eigene Zählvariable).
        if callable(fetch):
            self.clip_requests_last_cycle = len(chunk_logins(live_logins, CLIP_BATCH_SIZE))
        else:  # pragma: no cover - Fallback für Fremd-Clients
            self.clip_requests_last_cycle = len(live_logins)
        try:
            if callable(fetch):
                clips_by_login = await fetch(live_logins)
            else:  # pragma: no cover - Fallback für Fremd-Clients
                clips_by_login = {}
                for login in live_logins:
                    clips_by_login[login] = await self.twitch.fetch_clips(login)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Clip-Poll fehlgeschlagen (%d Live-Streamer), nutze letzten Cache: %s",
                len(live_logins),
                exc,
            )
            return
        async with self._cache_lock:
            for login, clips in clips_by_login.items():
                self.clips_cache[login] = list(clips)

    def _evict_disabled(self, enabled: set[str]) -> None:
        """Entfernt deaktivierte/gelöschte Logins aus beiden Poll-Caches."""
        for login in list(self.channel_cache):
            if login not in enabled:
                self.channel_cache.pop(login, None)
        for login in list(self.clips_cache):
            if login not in enabled:
                self.clips_cache.pop(login, None)

    async def _refresh_cache(self, logins: list[str]) -> bool:
        """Holt einen Batch und aktualisiert die Caches; True bei Erfolg."""
        enabled = {
            str(login).strip().lower()
            for login in (self.enabled_logins or logins)
        }
        self._evict_disabled(enabled)
        reset = getattr(self.twitch, "reset_request_counter", None)
        if callable(reset):
            reset()
        try:
            channels = await self.twitch.fetch_channels_batch(logins)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.gql_requests_last_cycle = self._requests_last_cycle()
            logger.warning(
                "Batch-Poll fehlgeschlagen (%d Streamer), nutze letzten Cache: %s",
                len(logins),
                exc,
            )
            return False
        self.gql_requests_last_cycle = self._requests_last_cycle()
        for login, channel in channels.items():
            # Live-Status ohne Clips; der Clip-Cache wird ausschließlich vom
            # separaten Clip-Poll gefüllt (siehe _clip_poll_once).
            self.channel_cache[login] = channel
        self.cache_updated_at = time.monotonic()
        self.cache_version += 1
        return True

    async def _ensure_cache(self) -> None:
        """Lädt den Cache einmalig nach, wenn er älter als 3 Poll-Intervalle ist."""
        if not self._cache_is_stale():
            return
        interval = max(1.0, float(self.config.live_poll_s))
        if time.monotonic() - self._last_refresh_attempt < interval:
            return
        async with self._cache_lock:
            if not self._cache_is_stale():
                return
            if time.monotonic() - self._last_refresh_attempt < interval:
                return
            self._last_refresh_attempt = time.monotonic()
            logins = await self._load_enabled_logins()
            self.enabled_logins = logins
            await self._refresh_cache(logins)

    def _cache_is_stale(self) -> bool:
        if not self.channel_cache:
            return True
        interval = max(1.0, float(self.config.live_poll_s)) * CACHE_STALE_FACTOR
        return time.monotonic() - self.cache_updated_at > interval

    async def _load_enabled_logins(self) -> list[str]:
        async with get_sessionmaker()() as session:
            rows = list((await session.execute(select(Streamer))).scalars())
        self.total_streamers = len(rows)
        return [row.login for row in rows if row.enabled]

    def _requests_last_cycle(self) -> int:
        value = getattr(self.twitch, "gql_requests_last_cycle", None)
        if value is None:
            value = getattr(self.twitch, "requests_last_cycle", 0)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    async def cached_channel(self, login: str) -> Channel | None:
        """Kanal aus dem zentralen Poll-Cache (ohne eigenen Twitch-Request)."""
        await self._ensure_cache()
        return self.channel_cache.get(login.strip().lower())

    async def cached_clips(self, login: str) -> list[Clip]:
        """Clips aus dem zentralen Poll-Cache (ohne eigenen Twitch-Request)."""
        await self._ensure_cache()
        return list(self.clips_cache.get(login.strip().lower(), []))

    def _active_session_count(self) -> int:
        return sum(
            1 for state in self.states.values() if state.state in ("connecting", "live")
        )

    def _refresh_queue_positions(self) -> None:
        queued = sorted(
            (state for state in self.states.values() if state.state == "queued"),
            key=lambda state: state.streamer_id,
        )
        for position, state in enumerate(queued):
            state.queue_position = position
        for state in self.states.values():
            if state.state != "queued":
                state.queue_position = None

    async def kick(self) -> None:
        """Sofortigen Reconcile anstoßen (nach UI-Änderungen)."""
        with contextlib.suppress(Exception):
            await self._reconcile()

    async def remove_streamer(self, streamer_id: int) -> None:
        task = self.tasks.pop(streamer_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        session = self.sessions.pop(streamer_id, None)
        if session is not None:
            with contextlib.suppress(Exception):
                await session.stop()
        self.states.pop(streamer_id, None)

    # -- Streamer-Task ------------------------------------------------------
    async def _run_streamer(self, streamer_id: int) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            streamer = await self._load_streamer(streamer_id)
            if streamer is None or not streamer.enabled:
                return
            state = self.states.setdefault(
                streamer_id, RuntimeState(streamer_id=streamer.id, login=streamer.login)
            )
            state.login = streamer.login
            try:
                channel = await self.cached_channel(streamer.login)
                state.last_error = None
                if channel is None or not channel.is_live:
                    state.state = "offline"
                    state.viewers = 0
                    await self._set_last_error(streamer_id, None)
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=float(self.config.live_poll_s))
                    except asyncio.TimeoutError:
                        pass
                    backoff = 5.0
                    continue

                settings = await self.settings_service.get()
                limit = capacity.max_concurrent_streams(settings)

                # Admission Control: Slot und Speicher-Guard. Der Zustandswechsel
                # passiert ohne await nach der Prüfung, damit zwei Tasks nicht
                # gleichzeitig denselben freien Slot belegen.
                if self._active_session_count() >= limit:
                    state.state = "queued"
                    state.queue_position = None
                    self._refresh_queue_positions()
                    logger.debug(
                        "Kein freier Slot für %s (%d/%d belegt) – eingereiht",
                        streamer.login,
                        self._active_session_count(),
                        limit,
                    )
                    return

                guard_reason = capacity.memory_guard_reason()
                if guard_reason:
                    state.state = "queued"
                    state.last_error = guard_reason
                    self._refresh_queue_positions()
                    logger.warning("Aufnahme %s zurückgestellt: %s", streamer.login, guard_reason)
                    return

                state.state = "connecting"
                state.queue_position = None
                state.viewers = channel.viewers
                state.stream_started_at = channel.started_at
                await self._touch_live(streamer_id, clear_error=True)
                stream_id = channel.stream_id or f"live-{int(utcnow().timestamp())}"
                session = LiveSession(self, streamer, str(stream_id), settings)
                self.sessions[streamer_id] = session
                try:
                    await session.run()
                finally:
                    self.sessions.pop(streamer_id, None)
                state.state = "offline"
                state.buffer_s = 0.0
                backoff = 5.0
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                state.state = "offline"
                raise
            except UserNotFound as exc:
                message = str(exc)
                state.state = "error"
                state.last_error = message
                await self._set_last_error(streamer_id, message)
                logger.error("Streamer %s: %s", streamer.login, message)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=600.0)
                except asyncio.TimeoutError:
                    pass
            except (RecorderError, TriggerError) as exc:
                state.state = "error"
                state.last_error = str(exc)
                await self._set_last_error(streamer_id, str(exc))
                logger.warning("Streamer %s: %s", streamer.login, exc)
                await self._sleep_backoff(backoff)
                backoff = min(300.0, backoff * 2)
            except Exception as exc:  # noqa: BLE001
                state.state = "error"
                state.last_error = str(exc)[:500]
                await self._set_last_error(streamer_id, str(exc)[:1000])
                logger.exception("Streamer-Task %s fehlgeschlagen", streamer.login)
                await self._sleep_backoff(backoff)
                backoff = min(300.0, backoff * 2)

    async def _sleep_backoff(self, backoff: float) -> None:
        delay = backoff + random.uniform(0, min(5.0, backoff * 0.2))
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _load_streamer(self, streamer_id: int) -> StreamerRef | None:
        async with get_sessionmaker()() as session:
            row = await session.get(Streamer, streamer_id)
            if row is None:
                return None
            return StreamerRef(
                id=row.id,
                login=row.login,
                display_name=row.display_name,
                enabled=row.enabled,
                twitch_user_id=row.twitch_user_id,
            )

    async def _set_last_error(self, streamer_id: int, message: str | None) -> None:
        try:
            async with get_sessionmaker()() as session:
                row = await session.get(Streamer, streamer_id)
                if row is not None:
                    row.last_error = message
                    await session.commit()
        except Exception:  # noqa: BLE001
            logger.debug("last_error konnte nicht gespeichert werden", exc_info=True)

    async def _touch_live(self, streamer_id: int, clear_error: bool = False) -> None:
        try:
            async with get_sessionmaker()() as session:
                row = await session.get(Streamer, streamer_id)
                if row is not None:
                    row.last_live_at = utcnow()
                    if clear_error:
                        row.last_error = None
                    await session.commit()
        except Exception:  # noqa: BLE001
            logger.debug("last_live_at konnte nicht gespeichert werden", exc_info=True)

    # -- API-Hilfen ---------------------------------------------------------
    def session_for_login(self, login: str) -> LiveSession | None:
        login = login.strip().lower()
        for session in self.sessions.values():
            if session.streamer.login == login:
                return session
        return None

    async def trigger(self, login: str, reason: str | None = None) -> int:
        session = self.session_for_login(login)
        if session is None:
            raise TriggerError(f"Für '{login}' läuft keine Aufnahme (nicht live?).")
        return await session.trigger("manual", manual=True, reason=reason or "Manuell ausgelöst")

    def snapshot(self, streamer_id: int) -> dict[str, Any] | None:
        state = self.states.get(streamer_id)
        return state.as_dict() if state else None
