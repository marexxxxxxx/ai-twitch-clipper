# Chat-Tracking & Trigger-Tuning (Paket A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chat-Signale (Unique Chatters, Hype-Keywords, Copypasta, Clip-Kommandos, Streamer/Mod-Kommandos) ergänzen, die Trigger-Logik konfigurierbar machen und alles zunächst im Shadow-Mode messen, ohne die bestehende Aufnahme-Pipeline zu verändern.

**Architecture:** Die neuen Chat-Signale leben in `SessionSignals` und werden wie die bestehenden 5-Sekunden-Burst-Signale berechnet. Ein gemeinsames Gewichtsmodul hält die zehn Signalgewichte und das Chat-Profil. Der Detector bekommt `required_ticks` und ein DB-gestütztes Stundenlimit. Im Shadow-Mode werden die neuen Gewichte für die Fusion auf 0 gesetzt, die Rohwerte aber in `signal_sample.components`/`detail` gespeichert. Die UI macht alle Parameter sichtbar und bietet ein „Jetzt clippen“-Sicherheitsnetz.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, SQLite (aiosqlite), pytest, Vanilla JS/Jinja2.

**Spec:** `docs/superpowers/specs/2026-11-04-chat-tracking-design.md`

## Global Constraints

- Keine neuen Laufzeitabhängigkeiten; nur Standardbibliothek und vorhandene Pakete.
- Bestehende DB-Werte nicht überschreiben: `threshold=0.4`, aktuelle Gewichte (`chat_rate=0.53`, `emote_burst=0.51`, `clip_created=0.30`, `viewers=0.15`, `clip_burst=0.10`) und die vorhandene Aufnahme bleiben erhalten.
- Neue Signale sind standardmäßig Shadow-Signale: Gewicht 0 in der Fusion, aber in `signal_sample.components` und `detail` gespeichert.
- `signal_sample.detail` enthält nur aggregierte Werte (Zähler, Mediane, Keyword-Treffer, Anzahl User). Keine Chat-Texte, keine User-Namen.
- Stundenlimit ist pro Streamer und über die `recording`-Tabelle persistent; manuelle Trigger umgehen es, zählen aber als Recording.
- Bestehende Tests müssen grün bleiben; neue Tests decken jede Aufgabe ab.
- Nur Feature-Dateien committen. **Nicht** committen: `.gitignore`, `docker-compose.yml`, `INTEGRATION.md`, `app/tools/inbox_bridge.py`. Spec und Plan unter `docs/superpowers/` dürfen committet werden.
- Sprache: UI-Texte deutsch, Code/Bezeichner englisch, Docstrings wie im Bestand.

## Review Focus

1. **Bestehende Installation:** `seed_defaults()` darf `threshold`/`weights` nicht anfassen und muss `required_ticks=2`/`max_clips_per_hour=60` setzen, damit das alte Verhalten bis zum expliziten Rollout erhalten bleibt. Test in Task 2.
2. **Shadow-Mode:** Die neuen Gewichte müssen den Legacy-Score exakt unverändert lassen. Test in Task 1 und Task 9.
3. **Copypasta-Fallback:** Fehlt `user-id`, muss `user` (Anzeigename) als Identität dienen; sonst kollabieren alle Nachrichten auf einen User. Test in Task 5.
4. **Stundenlimit:** Muss Recordings aus der DB zählen und Reconnects/Neustarts überleben; manuelle Trigger dürfen es umgehen, aber nicht unsichtbar bleiben. Test in Task 7/9.
5. **Datenschutz:** `detail` darf keine Chat-Texte oder User-Namen enthalten, auch nicht in Fehlerfällen. Test in Task 6/8.

---

### Task 1: Gemeinsame Gewichte und Keyword-Helfer

**Files:**
- Create: `app/detect/weights.py`
- Create: `app/detect/keywords.py`
- Modify: `app/detect/signals.py` (lokale `DEFAULT_WEIGHTS`/`sanitize_weights` entfernen, re-exportieren)
- Modify: `app/runtime_settings.py` (`DEFAULT_WEIGHTS` importieren)
- Test: `tests/test_weights.py`
- Test: `tests/test_keywords.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `DEFAULT_WEIGHTS: dict[str, float]` with all ten keys; new keys `0.0`.
  - `CHAT_PROFILE_WEIGHTS: dict[str, float]` with all ten keys and sum `1.0`.
  - `NEW_SIGNAL_KEYS: tuple[str, ...]` = `("unique_chatters", "hype_keywords", "copypasta", "clip_commands", "streamer_command")`.
  - `sanitize_weights(weights: dict[str, float] | None) -> dict[str, float]`.
  - `active_weights(weights: dict[str, float] | None, shadow_mode: bool) -> dict[str, float]`.
  - `DEFAULT_HYPE_KEYWORDS: str`.
  - `parse_keywords(raw: str | None) -> tuple[str, ...]`.
  - `keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern[str]`.
  - `has_hype_keyword(text: str, pattern: re.Pattern[str]) -> bool`.

- [ ] **Step 1: Write failing tests for weights and keywords**

```python
from app.detect.weights import (
    CHAT_PROFILE_WEIGHTS,
    DEFAULT_WEIGHTS,
    NEW_SIGNAL_KEYS,
    active_weights,
    sanitize_weights,
)

def test_default_weights_cover_all_signals():
    assert set(DEFAULT_WEIGHTS) == {
        "chat_rate", "emote_burst", "clip_created", "viewers", "clip_burst",
        "unique_chatters", "hype_keywords", "copypasta", "clip_commands", "streamer_command",
    }
    for key in NEW_SIGNAL_KEYS:
        assert DEFAULT_WEIGHTS[key] == 0.0

def test_chat_profile_sums_to_one():
    assert set(CHAT_PROFILE_WEIGHTS) == set(DEFAULT_WEIGHTS)
    assert abs(sum(CHAT_PROFILE_WEIGHTS.values()) - 1.0) < 1e-9

def test_sanitize_fills_missing_keys_and_falls_back():
    assert sanitize_weights(None) == DEFAULT_WEIGHTS
    filled = sanitize_weights({"chat_rate": 0.5})
    assert filled["chat_rate"] == 0.5
    assert filled["unique_chatters"] == 0.0
    assert sanitize_weights({key: 0.0 for key in DEFAULT_WEIGHTS}) == DEFAULT_WEIGHTS

def test_active_weights_zeroes_new_signals_in_shadow_mode():
    weights = dict(CHAT_PROFILE_WEIGHTS)
    shadow = active_weights(weights, shadow_mode=True)
    assert shadow["chat_rate"] == weights["chat_rate"]
    for key in NEW_SIGNAL_KEYS:
        assert shadow[key] == 0.0
    assert active_weights(weights, shadow_mode=False)["unique_chatters"] == 0.15
```

```python
from app.detect.keywords import DEFAULT_HYPE_KEYWORDS, has_hype_keyword, keyword_pattern, parse_keywords

def test_parse_keywords_trims_dedupes_and_falls_back():
    assert parse_keywords(" geil, GEIL, lol ") == ("geil", "lol")
    assert parse_keywords("") == parse_keywords(DEFAULT_HYPE_KEYWORDS)

def test_keyword_pattern_uses_word_boundaries():
    pattern = keyword_pattern(("geil", "lol"))
    assert has_hype_keyword("das war geil", pattern)
    assert has_hype_keyword("LOL", pattern)
    assert not has_hype_keyword("geiler", pattern)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_weights.py tests/test_keywords.py -v`
Expected: FAIL because `app.detect.weights`/`app.detect.keywords` do not exist.

- [ ] **Step 3: Implement the two helper modules**

`app/detect/weights.py`:
- `DEFAULT_WEIGHTS` = existing five values plus five new keys at `0.0`.
- `CHAT_PROFILE_WEIGHTS` = design values (sum 1.0).
- `NEW_SIGNAL_KEYS`.
- `sanitize_weights()` fills missing keys from `DEFAULT_WEIGHTS`, converts to float, clamps each to `0.0..1.0`; if total ≤ 0, return a copy of `DEFAULT_WEIGHTS`.
- `active_weights()` calls `sanitize_weights`, then zeroes `NEW_SIGNAL_KEYS` when `shadow_mode` is true.

`app/detect/keywords.py`:
- `DEFAULT_HYPE_KEYWORDS` = the comma-separated list from design section 5.2.
- `parse_keywords()` splits on commas, strips, lowercases, dedupes, drops empties; empty input returns the parsed default list.
- `keyword_pattern()` builds a case-insensitive alternation; tokens matching `^[a-z0-9]+$` get `(?<!\w)…(?!\w)`, everything else is escaped literal.
- `has_hype_keyword()` returns `bool(pattern.search(text))`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_weights.py tests/test_keywords.py -v`
Expected: PASS.

- [ ] **Step 5: Re-export and remove duplicates**

In `app/detect/signals.py` replace the local `DEFAULT_WEIGHTS` and `sanitize_weights` with:
```python
from .weights import DEFAULT_WEIGHTS, sanitize_weights  # re-export for existing imports
```
In `app/runtime_settings.py` replace the local `DEFAULT_WEIGHTS` block with `from .detect.weights import DEFAULT_WEIGHTS`.

- [ ] **Step 6: Run the affected tests**

Run: `python -m pytest tests/test_weights.py tests/test_keywords.py tests/test_detector.py tests/test_settings_validation.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/detect/weights.py app/detect/keywords.py app/detect/signals.py app/runtime_settings.py tests/test_weights.py tests/test_keywords.py
git commit -m "feat: add shared signal weights and keyword helpers"
```

---

### Task 2: Einstellungen, Defaults und rückwärtskompatibles Seeding

**Files:**
- Modify: `app/runtime_settings.py`
- Test: `tests/test_settings_chat.py` (new)
- Test: `tests/test_settings_validation.py` (adjust/extend)

**Interfaces:**
- Consumes: `DEFAULT_WEIGHTS`, `DEFAULT_HYPE_KEYWORDS` from Task 1.
- Produces:
  - `EDITABLE_KEYS` includes `required_ticks`, `max_clips_per_hour`, `shadow_mode`, `streamer_command_triggers`, `hype_keywords`, `copypasta_min_users`, `copypasta_window_s`.
  - `default_values()` fresh: `threshold=0.40`, `required_ticks=1`, `max_clips_per_hour=6`, `shadow_mode=True`, `streamer_command_triggers=True`, `hype_keywords=DEFAULT_HYPE_KEYWORDS`, `copypasta_min_users=3`, `copypasta_window_s=10`.
  - `seed_defaults()` existing installation: `required_ticks=2`, `max_clips_per_hour=60`, leaves existing `threshold`/`weights` untouched.
  - `validate_updates()` validates new keys.

- [ ] **Step 1: Write failing tests**

```python
from app.runtime_settings import default_values

def test_fresh_defaults_enable_chat_tracking():
    values = default_values(AppConfig(storage_root=tmp_path, data_dir=tmp_path))
    assert values["threshold"] == 0.40
    assert values["required_ticks"] == 1
    assert values["max_clips_per_hour"] == 6
    assert values["shadow_mode"] is True
    assert values["streamer_command_triggers"] is True
    assert values["copypasta_min_users"] == 3
    assert values["copypasta_window_s"] == 10
```

```python
async def test_existing_install_keeps_threshold_and_weights(tmp_path):
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from app.config import AppConfig
    from app.db import build_engine
    from app.models import Base, Setting
    from app.runtime_settings import read_all, seed_defaults
    from app.timeutil import utcnow

    config = AppConfig(storage_root=tmp_path, data_dir=tmp_path)
    engine = build_engine(tmp_path / "settings.db")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add_all([
            Setting(key="threshold", value=0.53, updated_at=utcnow()),
            Setting(key="weights", value={"chat_rate": 0.53, "emote_burst": 0.51}, updated_at=utcnow()),
        ])
        await session.commit()
        await seed_defaults(session, config)
        values = await read_all(session, config)
    assert values["threshold"] == 0.53
    assert values["weights"]["chat_rate"] == 0.53
    assert values["required_ticks"] == 2
    assert values["max_clips_per_hour"] == 60
    assert values["shadow_mode"] is True
    await engine.dispose()
```

```python
async def test_new_settings_are_validated():
    import pytest
    from app.runtime_settings import SettingsValidationError, validate_updates

    clean = await validate_updates({
        "required_ticks": 2,
        "max_clips_per_hour": 12,
        "shadow_mode": False,
        "streamer_command_triggers": False,
        "hype_keywords": "geil, lol",
        "copypasta_min_users": 4,
        "copypasta_window_s": 20,
    })
    assert clean["required_ticks"] == 2
    assert clean["max_clips_per_hour"] == 12
    assert clean["shadow_mode"] is False
    assert clean["streamer_command_triggers"] is False
    assert clean["hype_keywords"] == "geil, lol"
    assert clean["copypasta_min_users"] == 4
    assert clean["copypasta_window_s"] == 20
    for payload in (
        {"required_ticks": 0},
        {"max_clips_per_hour": 0},
        {"copypasta_min_users": 1},
        {"copypasta_window_s": 4},
        {"hype_keywords": "x" * 501},
        {"shadow_mode": "yes"},
    ):
        with pytest.raises(SettingsValidationError):
            await validate_updates(payload)
```

```python
async def test_weights_update_fills_new_keys():
    from app.runtime_settings import validate_updates

    clean = await validate_updates({"weights": {"chat_rate": 1.0}})
    assert clean["weights"]["unique_chatters"] == 0.0
    assert clean["weights"]["clip_created"] == 0.30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_settings_chat.py -v`
Expected: FAIL because the keys/defaults/validation are missing.

- [ ] **Step 3: Implement defaults, editable keys and validation**

- Extend `default_values()` with the values listed above.
- Extend `EDITABLE_KEYS`.
- In `validate_updates()`:
  - `required_ticks`: `_as_int(..., "Benötigte Ticks", 1, 5)`
  - `max_clips_per_hour`: `_as_int(..., "Max. Clips pro Stunde", 1, 60)`
  - `shadow_mode` / `streamer_command_triggers`: strict bool
  - `hype_keywords`: `str(value or "").strip()`, max 500 chars; empty allowed
  - `copypasta_min_users`: 2–10
  - `copypasta_window_s`: 5–30
  - weights loop already iterates `DEFAULT_WEIGHTS`, now 10 keys.
- In `seed_defaults()`:
  - `existing = {row.key ...}`; `is_existing = bool(existing)`.
  - Build `values = default_values(config)`.
  - If `is_existing`: set `values["required_ticks"] = 2`, `values["max_clips_per_hour"] = 60`, and if `"threshold" not in existing` set `values["threshold"] = 0.60` (old implicit default). Do not touch existing threshold/weights.
  - Insert only missing keys.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_settings_chat.py tests/test_settings_validation.py -v`
Expected: PASS. Update any test that assumed the old fresh `threshold=0.60` only if it genuinely tests fresh defaults; do not weaken existing validation assertions.

- [ ] **Step 5: Commit**

```bash
git add app/runtime_settings.py tests/test_settings_chat.py tests/test_settings_validation.py
git commit -m "feat: add chat tracking settings with safe migration defaults"
```

---

### Task 3: ChatMessage um user_id und badges erweitern

**Files:**
- Modify: `app/twitch/chat.py`
- Modify: `tests/test_signals.py` (existing parser tests live here)

**Interfaces:**
- Consumes: IRC tags `user-id`, `badges`.
- Produces: `ChatMessage(..., user_id: str = "", badges: str = "")`; `parse_privmsg()` fills both.

- [ ] **Step 1: Write failing parser tests**

```python
def test_parse_privmsg_reads_user_id_and_badges():
    line = (
        "@badges=broadcaster/1;display-name=Alpha;emotes=;user-id=123 "
        ":alpha!alpha@alpha.tmi.twitch.tv PRIVMSG #alpha :!clip"
    )
    message = parse_privmsg(line)
    assert message is not None
    assert message.user_id == "123"
    assert message.badges == "broadcaster/1"

def test_parse_privmsg_defaults_user_id_and_badges():
    line = "@display-name=Beta :beta!beta@beta.tmi.twitch.tv PRIVMSG #beta :haha"
    message = parse_privmsg(line)
    assert message is not None
    assert message.user_id == ""
    assert message.badges == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_signals.py -k user_id -v`
Expected: FAIL because `ChatMessage` has no `user_id`/`badges`.

- [ ] **Step 3: Implement**

- Add `user_id: str = ""` and `badges: str = ""` as the last fields of `ChatMessage` (preserve positional compatibility).
- In `parse_privmsg()`, pass `user_id=tags.get("user-id", "")` and `badges=tags.get("badges", "")`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_signals.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/twitch/chat.py tests/test_signals.py
git commit -m "feat: parse twitch user id and badges"
```

---

### Task 4: Bucket-Signale für Unique Chatters, Hype-Keywords, Clip-Kommandos und Streamer-Kommandos

**Files:**
- Modify: `app/detect/signals.py`
- Test: `tests/test_signals.py`

**Interfaces:**
- Consumes: `ChatMessage.user_id`, `.badges`, `.text`; `keyword_pattern`, `has_hype_keyword`; `burst_score`.
- Produces:
  - `identity(message: ChatMessage) -> str` = `message.user_id or message.user`.
  - `Bucket.users: set[str]`, `Bucket.hype_keywords: int`, `Bucket.clip_commands: int`, `Bucket.hype_matches: set[str]`.
  - `SessionSignals(started_at=None, hype_keywords=None, copypasta_min_users=3, copypasta_window_s=10)`.
  - `unique_chatters_signal(now=None)`, `hype_keywords_signal(now=None)`, `clip_commands_signal(now=None)`, `streamer_command_signal(now=None)`.
  - `_window_values`/`_bucket_metric` support `"unique_chatters"`, `"hype_keywords"`, `"clip_commands"`.

- [ ] **Step 1: Write failing signal tests**

```python
def test_unique_chatters_counts_distinct_users():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10))
    message_ts = NOW - timedelta(seconds=5)
    signals.on_chat(ChatMessage(ts=message_ts, user="Alice", user_id="1", text="a", emotes=[], emote_only=False, hype_count=0), now=message_ts)
    signals.on_chat(ChatMessage(ts=message_ts, user="Alice", user_id="1", text="b", emotes=[], emote_only=False, hype_count=0), now=message_ts)
    signals.on_chat(ChatMessage(ts=message_ts, user="Bob", user_id="2", text="c", emotes=[], emote_only=False, hype_count=0), now=message_ts)
    assert signals._bucket_metric(NOW, "unique_chatters") == 2

def test_hype_keywords_and_clip_commands_are_counted():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10), hype_keywords="geil,lol")
    message_ts = NOW - timedelta(seconds=5)
    signals.on_chat(ChatMessage(ts=message_ts, user="A", user_id="1", text="das war geil", emotes=[], emote_only=False, hype_count=0), now=message_ts)
    signals.on_chat(ChatMessage(ts=message_ts, user="B", user_id="2", text="!clip", emotes=[], emote_only=False, hype_count=0), now=message_ts)
    assert signals._bucket_metric(NOW, "hype_keywords") == 1
    assert signals._bucket_metric(NOW, "clip_commands") == 1

def test_streamer_command_hits_and_expires():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10))
    message = ChatMessage(ts=NOW, user="Alpha", user_id="1", text="!clip", emotes=[], emote_only=False, hype_count=0, badges="broadcaster/1")
    signals.on_chat(message, now=NOW)
    assert signals.streamer_command_signal(NOW).score == 1.0
    assert signals.streamer_command_signal(NOW + timedelta(seconds=31)).score == 0.0

def test_regular_user_clip_command_is_not_streamer_command():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10))
    message = ChatMessage(ts=NOW, user="Viewer", user_id="2", text="!clip", emotes=[], emote_only=False, hype_count=0, badges="subscriber/12")
    signals.on_chat(message, now=NOW)
    assert signals.streamer_command_signal(NOW).score == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_signals.py -v`
Expected: FAIL because the bucket fields/signals do not exist.

- [ ] **Step 3: Implement bucket fields and signal methods**

- Extend `Bucket` with `users: set[str] = field(default_factory=set)`, `hype_keywords: int = 0`, `clip_commands: int = 0`, `hype_matches: set[str] = field(default_factory=set)`.
- Add the `identity()` helper and the clip-command regex `r"(?i)(?:^|\s)(?:!clip|clip\s+(?:it|that|das)|clippen|!highlight)(?:\s|$)"`.
- In `SessionSignals.__init__`, compile `self._hype_pattern = keyword_pattern(parse_keywords(hype_keywords))`, store copypasta params, initialize `self._streamer_command_until = None`.
- In `on_chat()`, update all counters and the users set. For hype matches, use `self._hype_pattern.findall(text)` and add lowercased matches. For streamer commands, check `badges` contains `broadcaster/1` or `moderator/1` and the clip regex matches; set `self._streamer_command_until = now + 30s`.
- Extend `_window_values()` and `_bucket_metric()` with the new metric names; missing buckets contribute 0.
- Add a generic `_burst_signal(now, metric, extra_key)` used by `unique_chatters_signal`, `hype_keywords_signal`, `clip_commands_signal`; keep `chat_rate_signal`/`emote_signal` behavior unchanged.
- `streamer_command_signal()` returns 1.0 while the hold is active, else 0.0; extra `{"hit": 1.0/0.0, "until": iso-or-None}`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_signals.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/detect/signals.py tests/test_signals.py
git commit -m "feat: add chat bucket signals"
```

---

### Task 5: Copypasta-Erkennung

**Files:**
- Modify: `app/detect/signals.py`
- Test: `tests/test_signals.py`

**Interfaces:**
- Consumes: `identity()`, `copypasta_min_users`, `copypasta_window_s`.
- Produces: `copypasta_signal(now=None) -> SignalDetail`; state `deque[(ts, normalized_text, identity)]`; hold 15 s.

- [ ] **Step 1: Write failing tests**

```python
def test_copypasta_requires_three_distinct_users():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10), copypasta_min_users=3)
    for user_id, user in (("1", "A"), ("2", "B"), ("3", "C")):
        signals.on_chat(ChatMessage(ts=NOW, user=user, user_id=user_id, text="das ist krass", emotes=[], emote_only=False, hype_count=0), now=NOW)
    assert signals.copypasta_signal(NOW).score == 1.0

def test_copypasta_ignores_same_user_repeats():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10), copypasta_min_users=3)
    for _ in range(3):
        signals.on_chat(ChatMessage(ts=NOW, user="A", user_id="1", text="das ist krass", emotes=[], emote_only=False, hype_count=0), now=NOW)
    assert signals.copypasta_signal(NOW).score == 0.0

def test_copypasta_falls_back_to_display_name():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10), copypasta_min_users=3)
    for user in ("A", "B", "C"):
        signals.on_chat(ChatMessage(ts=NOW, user=user, user_id="", text="das ist krass", emotes=[], emote_only=False, hype_count=0), now=NOW)
    assert signals.copypasta_signal(NOW).score == 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_signals.py -k copypasta -v`
Expected: FAIL because `copypasta_signal` is missing.

- [ ] **Step 3: Implement**

- Add `_normalize_text(text)`: lowercase, remove URLs, collapse whitespace, strip; ignore emote-only and normalized length < 4.
- Add `_recent_texts: deque[tuple[datetime, str, str]]` (maxlen 2000), `_copypasta_until`, `_copypasta_users: set[str]`.
- In `on_chat()`, append `(now, normalized, identity(message))` for eligible messages; prune entries older than 30 s; group by normalized text; if any text has at least `copypasta_min_users` distinct identities within `copypasta_window_s`, set `_copypasta_until = now + 15s` and `_copypasta_users` to those identities.
- `copypasta_signal(now)` returns 1.0 while `now < _copypasta_until`, else 0.0; extra `{"active": 1.0/0.0, "users": len(...), "window_s": copypasta_window_s}`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_signals.py -k copypasta -v`
Expected: PASS, including the 15-s expiry test.

- [ ] **Step 5: Commit**

```bash
git add app/detect/signals.py tests/test_signals.py
git commit -m "feat: detect chat copypasta bursts"
```

---

### Task 6: Snapshot und Detail-Ausgabe integrieren

**Files:**
- Modify: `app/detect/signals.py`
- Test: `tests/test_signals.py`

**Interfaces:**
- Consumes: all new signal methods from Tasks 4/5.
- Produces: `snapshot()` returns 10 components and detail entries:
  - `unique_chatters`: `{value, median, samples}`
  - `hype_keywords`: `{value, median, samples, matched}`
  - `copypasta`: `{active, users, window_s}`
  - `clip_commands`: `{value, median, samples}`
  - `streamer_command`: `{hit, until}`
  - existing 4 detail keys unchanged.

- [ ] **Step 1: Write failing snapshot test**

```python
def test_snapshot_contains_all_signals_and_safe_detail():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10))
    components, detail = signals.snapshot(NOW)
    assert set(components) == {
        "chat_rate", "emote_burst", "clip_created", "viewers", "clip_burst",
        "unique_chatters", "hype_keywords", "copypasta", "clip_commands", "streamer_command",
    }
    assert "unique_chatters" in detail
    assert "hype_keywords" in detail
    assert "copypasta" in detail
    assert "clip_commands" in detail
    assert "streamer_command" in detail
    assert "text" not in detail["hype_keywords"]
    assert "user" not in detail["copypasta"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_signals.py -k snapshot_contains_all -v`
Expected: FAIL because snapshot still returns five components.

- [ ] **Step 3: Implement**

- Call the five new signal methods in `snapshot()`.
- Add their rounded scores to `components`.
- Add their extras to `detail`; ensure `hype_keywords` carries `matched` as a sorted comma-joined string, `streamer_command` carries `hit`/`until`, `copypasta` carries `active`/`users`/`window_s`.
- Do not add raw `text` or user names to detail.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_signals.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/detect/signals.py tests/test_signals.py
git commit -m "feat: expose chat signal snapshot details"
```

---

### Task 7: Detector-Logik für Ticks und Stundenlimit

**Files:**
- Modify: `app/detect/detector.py`
- Test: `tests/test_detector.py`

**Interfaces:**
- Consumes: `COMPONENT_LABELS`, `fuse`, weights.
- Produces:
  - `COMPONENT_LABELS` with 10 labels.
  - `Detector(..., max_clips_per_hour: int = 60)`.
  - `configure(..., required_ticks: int | None = None, max_clips_per_hour: int | None = None)`.
  - `can_trigger(now=None, buffer_s=0.0, recent_triggers=0) -> tuple[bool, list[str]]`.
  - `update(..., recent_triggers=0)`.
  - `snapshot()` includes `required_ticks`, `max_clips_per_hour`, `triggers_last_hour`.

- [ ] **Step 1: Write failing detector tests**

```python
def test_required_ticks_one_triggers_on_first_tick():
    detector = Detector(threshold=0.40, required_ticks=1, weights=WEIGHTS)
    decision = detector.update(components(0.9), now=NOW, buffer_s=300)
    assert decision.trigger

def test_hourly_cap_blocks_trigger():
    detector = Detector(threshold=0.40, required_ticks=1, max_clips_per_hour=2, weights=WEIGHTS)
    detector.update(components(0.9), now=NOW, buffer_s=300, recent_triggers=2)
    allowed, reasons = detector.can_trigger(NOW, buffer_s=300, recent_triggers=2)
    assert not allowed
    assert any("Stunde" in reason for reason in reasons)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_detector.py -v`
Expected: FAIL for the new parameters/method.

- [ ] **Step 3: Implement**

- Add the five labels: `unique_chatters` „Unique Chatter“, `hype_keywords` „Hype-Keywords“, `copypasta` „Copypasta“, `clip_commands` „Clip-Kommandos“, `streamer_command` „Streamer/Mod-Kommando“.
- Add `max_clips_per_hour` to `__init__` (`max(1, int(...))`) and `configure`.
- `can_trigger()` checks cooldown, buffer ≥ pre_roll, `recent_triggers < max_clips_per_hour`; returns `(False, reasons)` with German reasons for each blocker.
- `update()` calls `can_trigger()` and triggers only when score/hysteresis and `allowed`.
- Keep default `required_ticks=2` so existing tests and old behavior remain.
- Snapshot includes the new fields; store `last_recent_triggers` each update.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_detector.py -v`
Expected: PASS, including the existing 2-tick test.

- [ ] **Step 5: Commit**

```bash
git add app/detect/detector.py tests/test_detector.py
git commit -m "feat: make trigger hysteresis and hourly cap configurable"
```

---

### Task 8: SignalSample.detail und SQLite-Migration

**Files:**
- Modify: `app/models.py`
- Modify: `app/db.py`
- Modify: `app/main.py`
- Test: `tests/test_db_migration.py` (new)

**Interfaces:**
- Consumes: `SignalSample`.
- Produces:
  - `SignalSample.detail: Mapped[dict[str, Any] | None]`.
  - `ensure_signal_detail_column(engine: AsyncEngine | None = None) -> None` in `app/db.py`.
  - `main.lifespan` calls it after `create_schema()`.

- [ ] **Step 1: Write failing migration tests**

```python
async def test_existing_sqlite_gets_detail_column(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE signal_sample (id INTEGER PRIMARY KEY, ts DATETIME, score FLOAT, components JSON)"))
    await ensure_signal_detail_column(engine)
    async with engine.connect() as conn:
        columns = (await conn.execute(text("PRAGMA table_info(signal_sample)"))).all()
    assert "detail" in {row[1] for row in columns}

async def test_migration_is_idempotent(tmp_path):
    # call twice; no exception, exactly one detail column
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_db_migration.py -v`
Expected: FAIL because the column/helper is missing.

- [ ] **Step 3: Implement**

- Add `detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)` to `SignalSample`.
- Add `ensure_signal_detail_column(engine: AsyncEngine | None = None)`: resolve engine via `init_engine()` if `None`; run `PRAGMA table_info(signal_sample)`; if the table exists and `detail` is missing, execute `ALTER TABLE signal_sample ADD COLUMN detail JSON`.
- Call `await ensure_signal_detail_column()` after `await create_schema()` in `main.lifespan`.
- Fresh DBs get the column via `Base.metadata.create_all`; the helper is a no-op there.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_db_migration.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models.py app/db.py app/main.py tests/test_db_migration.py
git commit -m "feat: persist aggregate chat signal details"
```

---

### Task 9: Supervisor-Integration, Shadow-Mode und Streamer-Trigger

**Files:**
- Modify: `app/supervisor.py`
- Test: `tests/test_supervisor_chat.py` (new)
- Test: `tests/test_trigger_snapshot.py` (adjust if needed)

**Interfaces:**
- Consumes: `active_weights`, `sanitize_weights`, `Detector.can_trigger`, `Recording`.
- Produces:
  - `effective_weights_for(settings: dict) -> dict[str, float]`.
  - `async recent_trigger_count(session, streamer_id: int | None, now: datetime) -> int`.
  - `LiveSession` builds `SessionSignals` with keyword/copypasta config, Detector with `required_ticks`/`max_clips_per_hour`.
  - `_save_signal(now, score, components, detail)`.
  - Direct streamer/mod trigger after a successful trigger, only when `shadow_mode` is off.
  - `[shadow]` log for new components ≥ 0.7, max once per 30 s.

- [ ] **Step 1: Write failing tests**

```python
def test_effective_weights_for_shadow_mode():
    settings = {"weights": {"chat_rate": 0.53, "emote_burst": 0.51}, "shadow_mode": True}
    weights = effective_weights_for(settings)
    assert weights["chat_rate"] == 0.53
    assert weights["unique_chatters"] == 0.0

async def test_recent_trigger_count_is_db_backed(session_factory):
    from datetime import datetime, timedelta
    from app.models import Recording, Streamer
    from app.supervisor import recent_trigger_count

    now = datetime(2026, 11, 4, 12, 0, 0)
    async with session_factory() as session:
        alice = Streamer(login="alice", display_name="Alice")
        bob = Streamer(login="bob", display_name="Bob")
        session.add_all([alice, bob])
        await session.flush()
        session.add_all([
            Recording(streamer_id=alice.id, streamer_login="alice", status="ready", created_at=now - timedelta(minutes=5), path="", filename=""),
            Recording(streamer_id=alice.id, streamer_login="alice", status="ready", created_at=now - timedelta(hours=2), path="", filename=""),
            Recording(streamer_id=bob.id, streamer_login="bob", status="ready", created_at=now - timedelta(minutes=5), path="", filename=""),
        ])
        await session.commit()
        count = await recent_trigger_count(session, alice.id, now)
    assert count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_supervisor_chat.py -v`
Expected: FAIL because the helpers/behavior are missing.

- [ ] **Step 3: Implement**

- Add `effective_weights_for(settings)` using `active_weights(sanitize_weights(settings.get("weights")), bool(settings.get("shadow_mode", True)))`.
- Add `recent_trigger_count(session, streamer_id, now)` counting `Recording` rows with matching `streamer_id` and `created_at >= now - timedelta(hours=1)`.
- In `LiveSession.__init__`/`run()`, pass `required_ticks=int(settings.get("required_ticks") or 2)`, `max_clips_per_hour=int(settings.get("max_clips_per_hour") or 60)` to `Detector`.
- Build `SessionSignals` with `hype_keywords=settings.get("hype_keywords")`, `copypasta_min_users=int(settings.get("copypasta_min_users") or 3)`, `copypasta_window_s=int(settings.get("copypasta_window_s") or 10)`.
- In `tick()`:
  - `settings = await ...`; `shadow_mode = bool(settings.get("shadow_mode", True))`.
  - `weights = effective_weights_for(settings)`.
  - `detector.configure(..., weights=weights, required_ticks=..., max_clips_per_hour=...)`.
  - `recent = await recent_trigger_count(session, self.streamer.id, now)` (use a short session scope).
  - `decision = detector.update(..., weights=weights, recent_triggers=recent)`.
  - `await self._save_signal(now, decision.score, components, detail)`.
  - After the normal `if decision.trigger`, check `(detail.get("streamer_command") or {}).get("hit")`; if hit, `streamer_command_triggers`, not shadow, and not `decision.trigger`: call `can_trigger`; if allowed, `await self.trigger("auto", decision, manual=False, reason="Streamer/Mod-Clipping")`; only after `trigger()` returns successfully, call `detector.mark_triggered(now)`.
  - If shadow and any new component ≥0.7, log `[shadow]` at most once per 30 s with the component names/values.
- Update `_save_signal` to pass `detail=detail`.
- Ensure manual `Supervisor.trigger` remains bypassing cooldown/cap but the Recording counts for `recent_trigger_count`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_supervisor_chat.py tests/test_trigger_snapshot.py -v`
Expected: PASS; fix any fake `_save_signal` call signatures.

- [ ] **Step 5: Commit**

```bash
git add app/supervisor.py tests/test_supervisor_chat.py tests/test_trigger_snapshot.py
git commit -m "feat: wire chat signals and shadow mode into supervisor"
```

---

### Task 10: Shadow-Report-Werkzeug

**Files:**
- Create: `app/tools/shadow_report.py`
- Test: `tests/test_shadow_report.py`

**Interfaces:**
- Consumes: SQLite `signal_sample` (components/detail), `streamer`, `CHAT_PROFILE_WEIGHTS`.
- Produces: `build_report(db_path: str | Path, hours: int = 48, streamer: str | None = None, simulate_profile: bool = False) -> str`; CLI `python -m app.tools.shadow_report --db /data/clipper.db --hours 48 [--streamer LOGIN] [--simulate-profile]`.

- [ ] **Step 1: Write failing report test**

```python
def test_shadow_report_contains_signals_and_simulation(tmp_path):
    db = tmp_path / "clipper.db"
    # create signal_sample with one row containing new components/detail
    report = build_report(db, hours=48, streamer=None, simulate_profile=True)
    assert "unique_chatters" in report
    assert "would-trigger" in report.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_shadow_report.py -v`
Expected: FAIL because module is missing.

- [ ] **Step 3: Implement**

- Use `sqlite3` stdlib; open read-only (`mode=ro`) if possible.
- Select rows since cutoff, join streamer login; parse JSON components/detail.
- Per streamer and signal: tick count, p50/p95/max, count ≥0.7, top 3 timestamps.
- With `--simulate-profile`: compute `fuse(components, CHAT_PROFILE_WEIGHTS)`, then simulate threshold 0.40, `required_ticks=1`, cooldown 180 s, max 6/hour; count would-trigger moments.
- Print a readable German report.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_shadow_report.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/tools/shadow_report.py tests/test_shadow_report.py
git commit -m "feat: add shadow signal report"
```

---

### Task 11: Einstellungs-UI und Chat-Profil-Button

**Files:**
- Modify: `app/templates/settings.html`
- Modify: `app/static/app.js`
- Modify: `app/web/routes.py`
- Test: `tests/test_web_pages.py`

**Interfaces:**
- Consumes: `default_values` keys, `CHAT_PROFILE_WEIGHTS`.
- Produces:
  - Settings fields: `required_ticks`, `max_clips_per_hour`, `shadow_mode`, `streamer_command_triggers`, `hype_keywords`, `copypasta_min_users`, `copypasta_window_s`.
  - Ten weight inputs `weight_<key>`.
  - Button `#apply-chat-profile` with data from the route.
  - JS payload reads all fields dynamically.

- [ ] **Step 1: Write failing page test**

```python
def test_settings_page_has_chat_controls(tmp_path):
    # existing FakeSettingsService setup
    page = client.get("/settings").text
    for needle in (
        "required_ticks", "max_clips_per_hour", "shadow_mode",
        "streamer_command_triggers", "hype_keywords",
        "copypasta_min_users", "copypasta_window_s",
        "weight_unique_chatters", "apply-chat-profile",
    ):
        assert needle in page
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web_pages.py -k chat_controls -v`
Expected: FAIL.

- [ ] **Step 3: Implement template, route and JS**

- `web/routes.py` settings route passes `chat_profile=CHAT_PROFILE_WEIGHTS`.
- Add the new fields/sections; weight grid hardcodes ten keys with the five new labels marked „(Schatten)“ when `values.shadow_mode`.
- Add `<form id="settings-form" data-chat-profile='{{ chat_profile | tojson }}'>`.
- In `app.js`:
  - On submit, build weights via `form.querySelectorAll('input[name^="weight_"]')` into an object; include all new fields.
  - `#apply-chat-profile` click: confirm; read `form.dataset.chatProfile`; set the number inputs `threshold`, `required_ticks`, `max_clips_per_hour`; set the checkboxes `streamer_command_triggers.checked = true` and `shadow_mode.checked = false`; fill all ten `weight_<key>` inputs; then `form.requestSubmit()`.
- Dashboard labels in `streamerCard` add the five new labels and a shadow hint when present.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_web_pages.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/templates/settings.html app/static/app.js app/web/routes.py tests/test_web_pages.py
git commit -m "feat: expose chat tracking settings and profile"
```

---

### Task 12: „Jetzt clippen“-Button auf der Streamer-Seite

**Files:**
- Modify: `app/templates/streamers.html`
- Modify: `app/static/app.js`
- Test: `tests/test_web_pages.py`

**Interfaces:**
- Consumes: existing `POST /api/trigger` `{login, reason}`.
- Produces: per-row button `.js-clip`, JS handler, success/error toast.

- [ ] **Step 1: Write failing page test**

```python
def test_streamers_page_has_manual_clip_button(tmp_path):
    page = client.get("/streamers").text
    assert "js-clip" in page
    assert "Jetzt clippen" in page
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web_pages.py -k manual_clip -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

- Add the button to each row's action cell:
  `<button class="btn btn-small js-clip" type="button">Jetzt clippen</button>`.
- In `initStreamers()` click handler, handle `js-clip`:
  - `const login = row.dataset.login;`
  - `button.disabled = true;`
  - `await api("/api/trigger", { method: "POST", body: { login, reason: "Manuell über UI" } });`
  - `toast("Clip wird erzeugt.", "success");`
  - On error, show `error.message` and re-enable.
- Manual triggers intentionally bypass cooldown/hour cap; they create a Recording and therefore count for automatic triggers.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_web_pages.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/templates/streamers.html app/static/app.js tests/test_web_pages.py
git commit -m "feat: add manual clip button"
```

---

### Task 13: Dokumentation, Gesamttests und Rollout

**Files:**
- Modify: `README.md`
- Modify: `README-DOCKER.md`
- Commit: `docs/superpowers/specs/2026-11-04-chat-tracking-design.md`
- Commit: `docs/superpowers/plans/2026-11-04-chat-tracking.md`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: documented settings, shadow workflow, report command, rollout steps.

- [ ] **Step 1: Update documentation**

- `README.md`: new signals, shadow mode, `required_ticks`, `max_clips_per_hour`, profile button, manual button, `shadow_report.py` command.
- `README-DOCKER.md`: settings table/rows for the new keys and a short “Chat-Tracking (Paket A)” section.
- Do not touch `INTEGRATION.md` (integration work, not this feature).

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest -q`
Expected: all tests pass; no new warnings that indicate failures.

- [ ] **Step 3: Commit docs, spec and plan**

```bash
git add README.md README-DOCKER.md docs/superpowers/specs/2026-11-04-chat-tracking-design.md docs/superpowers/plans/2026-11-04-chat-tracking.md
git commit -m "docs: document chat tracking and rollout"
```

- [ ] **Step 4: Build and restart only the clipper service**

```bash
cd /home/user/twitch-clips/ai-twitch-clipper
docker compose build clipper
docker compose up -d --no-deps clipper
docker compose ps
```

Expected: `ai-twitch-clipper` healthy on host port 8010; forwarder/watcher unchanged.

- [ ] **Step 5: Apply the shadow rollout settings without resetting current weights**

Read current settings first:
```bash
curl -s http://127.0.0.1:8010/api/settings | python -m json.tool
```
Then PUT with the current weights plus new keys at 0, e.g.:
```json
{
  "threshold": 0.40,
  "required_ticks": 1,
  "max_clips_per_hour": 6,
  "shadow_mode": true,
  "streamer_command_triggers": true,
  "weights": {
    "chat_rate": 0.53,
    "emote_burst": 0.51,
    "clip_created": 0.30,
    "viewers": 0.15,
    "clip_burst": 0.10,
    "unique_chatters": 0.0,
    "hype_keywords": 0.0,
    "copypasta": 0.0,
    "clip_commands": 0.0,
    "streamer_command": 0.0
  }
}
```
If the current weights differ, use the values returned by GET. Never reset to `DEFAULT_WEIGHTS`.

- [ ] **Step 6: Verify shadow data and report**

```bash
curl -s http://127.0.0.1:8010/api/dashboard | python -m json.tool
docker compose exec clipper python -m app.tools.shadow_report --db /data/clipper.db --hours 1
docker compose exec clipper python -m app.tools.shadow_report --db /data/clipper.db --hours 48 --simulate-profile
```
Expected: `signal_sample.components` has the five new keys; `detail` has aggregate values; report shows per-signal stats and would-trigger estimate.

- [ ] **Step 7: After 24–48 h**

Review the report. If plausible, use “Chat-Profil anwenden” in the UI (sets profile weights, threshold 0.40, required_ticks 1, max 6, shadow off). Observe another 24 h; if too many clips, raise threshold/required_ticks or lower the hourly cap.

---

## Self-Review Notes

- Spec sections 4–11 map to Tasks 1–13; section 12 tests are covered per task; section 13 risks are addressed by shadow mode, hourly cap, keyword config and privacy detail.
- No task changes the watcher, bridge, assembler or cleanup pipeline.
- The plan deliberately keeps `required_ticks=2` for existing installs until the explicit rollout, then sets 1.
- Manual triggers bypass cooldown/cap by design but are counted via the `recording` table.
