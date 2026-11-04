# AI Twitch Clipper — Spezifikation (MARAAA-237)

Status: verbindlich für die Umsetzung. Abweichungen nur mit Begründung im README.

## 1. Ziel

Ein selbst gehosteter Dienst, der eine konfigurierbare Liste von Twitch-Streamern
permanent live überwacht, wahrscheinlich virale Momente erkennt und daraus
automatisch **Sequenzen von 5 Minuten** auf die Festplatte schreibt. Ein
nachgelagertes Modul (separat, nicht Teil dieses Projekts) schneidet aus diesen
Sequenzen die eigentlichen Clips.

Auslieferung als lauffähiger Docker-Container (docker compose), inkl. Weboberfläche.

## 2. Harte Anforderungen (aus der Aufgabe)

| # | Anforderung | Umsetzung |
|---|---|---|
| A1 | Streamer hinzufügen/entfernen, dauerhaft gespeichert | SQLite + UI/API |
| A2 | Immer live, keine VODs | Live-Erkennung + fortlaufende Aufnahme in Ringpuffer |
| A3 | Sequenz ca. 5 min, die den guten Moment enthält | 240 s Vorlauf + 60 s Nachlauf (konfigurierbar) |
| A4 | Automatischer Download bei erkanntem Moment | Trigger → Zusammenbau aus Ringpuffer |
| A5 | Speicherort wählbar | ENV `STORAGE_ROOT` (Volume) + Setting `storage_root` im UI, validiert |
| A6 | Fertig ausführbar als Docker-Container | Dockerfile + docker-compose.yml + README |
| A7 | Nachvollziehbar, warum ein Moment als viral gilt | Score-Breakdown im JSON-Sidecar + UI |

## 3. Technische Grundlage (in dieser Umgebung verifiziert)

- **Live-Status / User-Auflösung / Clips:** Twitch GraphQL anonym mit öffentlicher
  Web-Client-ID (`kimne78kx3ncx6brgo4mv6wki5h1ko`). Verifiziert: liefert
  `stream{id,viewersCount,createdAt,game}`, `user.id`, `clips(first,criteria:{period:LAST_DAY})`
  mit `createdAt`, `durationSeconds`, `viewCount`.
- **Optional Helix:** wenn `TWITCH_CLIENT_ID` + `TWITCH_CLIENT_SECRET` gesetzt sind,
  Helix als primäre Quelle (App-Token). Kein Zwang — das Projekt läuft ohne Zugangsdaten.
- **Stream-URL:** `streamlink` (pip, >= 7) löst `twitch.tv/<login>` in eine HLS-Playlist auf.
- **Aufnahme:** `streamlink --stdout` → `ffmpeg -c copy -f segment -segment_time 2
  -segment_list index.csv -segment_list_type csv`. Verifiziert: CSV liefert exakte
  Start-/Endzeiten je Segment (Sekunden relativ zum Aufnahmebeginn).
- **Chat:** IRC-WebSocket `wss://irc-ws.chat.twitch.tv:443`, anonym als `justinfan<random>`,
  `CAP REQ :twitch.tv/tags twitch.tv/commands`. Verifiziert.
- **Zusammenbau:** ffmpeg concat-Demuxer mit `-c copy -movflags +faststart`. Verifiziert.

Keine Neukodierung im Normalfall (schnell, CPU-arm). Nur wenn Segmentformate
inkompatibel sind, einmalig Remux auf MPEG-TS versuchen, sonst als `failed` markieren.

## 4. Architektur

```
app/
  main.py               FastAPI-App, Lifespan startet Supervisor + Retention
  config.py             pydantic-settings, ENV-Defaults
  db.py                 SQLAlchemy 2 async, SQLite (aiosqlite), Migration via create_all
  models.py             Streamer, Recording, Setting, SignalSample, LogEntry
  twitch/gql.py         anonymer GQL-Client (Retry, Timeout, Fehlerklassen)
  twitch/helix.py       optionaler Helix-Client, gleiche Schnittstelle
  twitch/chat.py        IRC-WS-Client, liefert Nachrichten-Buckets
  capture/recorder.py   streamlink|ffmpeg-Segmentrekorder, liest CSV-Index live
  capture/buffer.py     Ringpuffer-Verwaltung, Retention, Disk-Guard
  detect/signals.py     Chatrate, Emote-Burst, Clip-Neu, Viewer-Delta
  detect/detector.py    Normalisierung, Fusion, Hysterese, Cooldown, Zustandsautomat
  clip/assembler.py     Fensterwahl, Warten auf Nachlauf, concat, Thumbnail, Sidecar
  supervisor.py         ein asyncio-Task je überwachtem Streamer, Backoff/Restart
  storage.py            Pfadprüfung, Disk-Usage, atomare Writes
  retention.py          Aufräumen nach Policy
  api/*.py              REST-Endpunkte
  web/routes.py         Server-gerenderte Seiten (Jinja2)
  templates/ static/    UI (deutsch), kein Build-Schritt
  tools/simulate_trigger.py  CLI: Trigger manuell auslösen (Abnahmetest)
tests/                  pytest
Dockerfile, docker-compose.yml, .env.example, Makefile, README.md
```

**Stack:** Python 3.12, FastAPI, uvicorn, httpx, websockets, SQLAlchemy 2 + aiosqlite,
Jinja2, pydantic-settings, pytest/pytest-asyncio. Kein Node, kein Build-Step.

## 5. Erkennung „virales Moment“ (Detektor)

Alle Signale werden pro Stream-Session in 5-s-Buckets geführt; Historie 15 min.

1. **Chat-Rate** (`chat_rate`): Nachrichten/5 s gegen rollierenden Median der
   letzten 15 min (Fenster schließt die letzten 60 s aus). Score =
   `clamp((rate - 2.0*median) / (4*median + 4), 0, 1)`. Mindestdaten: 5 min,
   sonst Score 0. Trimmt Ausreißer auf 99. Perzentil.
2. **Emote-Burst** (`emote_burst`): Anteil reiner Emote-Nachrichten plus bekannte
   Hype-Emotes (`KEKW, LUL, OMEGALUL, Pog, PogChamp, monkaS, PepeHands, catJAM,
   Clap, ICANT, HUH`) je 5 s gegen eigenen Median. Score analog.
3. **Neuer Clip** (`clip_created`): Poll alle 30 s. Neuer Clip mit `createdAt` in den
   letzten 180 s ⇒ Score 1.0; Anker = `createdAt - duration/2`, begrenzt auf
   `[jetzt-300 s, jetzt]`. Mehrere neue Clips im Fenster: Score bleibt 1.0, zählt aber
   als Verstärker (`clip_burst` = min(1, n/3), Gewicht 0.1 zusätzlich).
4. **Viewer-Delta** (`viewers`): Poll alle 20 s. Relative Änderung über 120 s;
   Score = `clamp(delta/0.10, 0, 1)`, nur positiv.
5. **Optional Audio** (`audio`, Standard aus): `ffmpeg astats` auf separatem
   Audio-Pull, RMS-Sprung. Nur dokumentieren, nicht im Default-Pfad.

**Fusion:** `score = Σ w_i * s_i`, Defaults
`chat_rate 0.35, emote_burst 0.20, clip_created 0.30, viewers 0.15` (+ `clip_burst` 0.10,
Summe wird auf 1 normalisiert). Trigger, wenn `score ≥ threshold` (Default 0.60) in
**zwei aufeinanderfolgenden** Ticks (Hysterese) **und** `cooldown` (Default 180 s) abgelaufen
**und** Puffer mindestens `pre_roll` Sekunden enthält. Cooldown beginnt mit dem Trigger.

Jeder Tick wird als `SignalSample` gespeichert (score + Einzelwerte) — Grundlage für
Sparkline und Sidecar.

## 6. Aufnahme und Zusammenbau

- Segmentlänge 2 s, Ringpuffer 360 s je Streamer (Deckung: 240 s Vorlauf + Reserve).
- Ablage: `{storage_root}/_buffer/{login}/{stream_id}/seg_*.ts` + `index.csv`.
- Qualität Default `720p60,best` (konfigurierbar; reduziert Disk-Last deutlich).
- Dateiname final: `{storage_root}/{login}/{YYYY-MM-DD}/{HHMMSS}_{score100}_{recording_id}.mp4`
  plus `.json` (Sidecar) und `.jpg` (Thumbnail bei 20 % Laufzeit).
- Ablauf bei Trigger zum Zeitpunkt T:
  1. Fenster `[T - pre_roll, T + post_roll]`.
  2. Warten, bis der Rekorder `T + post_roll` erreicht hat (Timeout 5 min über Soll).
  3. Segmente vollständig innerhalb des Fensters wählen (Randsegmente ganz nehmen,
     dadurch Länge ± Segmentlänge; akzeptiert: 300 s ± 10 s).
  4. concat → temporäre Datei → `os.replace` (atomar) → Sidecar → Thumbnail.
  5. Puffer auf 300 s zurückschneiden.
  6. Status `ready` (oder `truncated`, wenn der Stream vorher endete; `failed` bei Fehler).
- Endet der Stream während des Nachlaufs: mit vorhandenem Material bauen, `truncated: true`.
- Disk-Guard: bei < 2 GB frei keine neuen Aufnahmen starten, Warnung im UI + Log;
  bei < 1 GB laufende Aufnahme sauber beenden.

## 7. Datenmodell

- `streamer(id, login UNIQUE, display_name, twitch_user_id, enabled, created_at,
  last_live_at, last_error)`
- `recording(id, streamer_id FK, path, filename, started_at, ended_at, duration_s,
  score, trigger JSON, status, size_bytes, thumbnail_path, error, created_at)`
- `setting(key PRIMARY KEY, value JSON, updated_at)`
- `signal_sample(id, streamer_id, ts, score, components JSON)`
- `log_entry(id, ts, level, streamer_login, message)`
- Defaults (Setting, im UI änderbar): `storage_root=/data`, `pre_roll_s=240`,
  `post_roll_s=60`, `threshold=0.60`, `cooldown_s=180`, `quality=720p60,best`,
  `retention_days=14`, `max_gb_per_streamer=50`, `weights={...}`, `audio_enabled=false`.

## 8. Oberfläche (deutsch, server-gerendert)

- **/** Dashboard: Karten je Streamer (LIVE/OFFLINE, Zuschauer, Uptime, Puffer-Sekunden,
  letzter Score, Sparkline 15 min), letzte Aufnahmen, Disk-Status, Fehlerhinweise.
- **/streamers**: hinzufügen per Login (wird gegen Twitch aufgelöst, Fehler bei
  unbekanntem Namen), aktivieren/deaktivieren, löschen (mit Bestätigung; Aufnahmen bleiben).
- **/recordings**: Liste + Filter, Inline-Player, Download, löschen, Sidecar einsehen.
- **/settings**: Speicherort (mit Button „Pfad prüfen“, zeigt Schreibbarkeit und freien
  Platz), Schwellen, Vor-/Nachlauf, Cooldown, Qualität, Retention. Speichern validiert.
- **/logs**: letzte 500 Zeilen, Auto-Refresh.
- **/api/***: REST + OpenAPI unter `/docs`. Health: `GET /api/health` → `{"status":"ok"}`.

UI ohne externe CDNs (offline-fähig), responsiv, Dark/ Light über
`prefers-color-scheme`, keine Tracker.

## 9. Betrieb

- `docker compose up -d --build`; Volume `./clips:/data` (Host-Pfad frei wählbar).
- ENV: `STORAGE_ROOT`, `TZ`, `LOG_LEVEL`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`
  (optional), `PORT`, `QUALITY`.
- Healthcheck im Compose (`/api/health`), `restart: unless-stopped`.
- Container läuft als non-root (`app`), `/data` beschreibbar.
- README (deutsch): Schnellstart, Host-Ordner wählen, Streamer hinzufügen, wie Erkennung
  funktioniert, Grenzen (Twitch-ToS, Werbeunterbrechungen, Kein DRM, Qualität), Troubleshooting.

## 10. Abnahmekriterien (binär)

1. `docker compose build` läuft ohne Fehler durch; `docker compose up -d` startet,
   Container wird binnen 60 s `healthy`.
2. `GET /api/health` → 200 `{"status":"ok"}`; `GET /` → 200 mit UI.
3. Streamer anlegen über UI/API ist nach Container-Neustart noch vorhanden.
4. Bei einem live Streamer: binnen 60 s Status LIVE, Segmentdateien wachsen im Puffer.
5. `python -m app.tools.simulate_trigger --login <x>` (oder API-Endpunkt) erzeugt aus dem
   Puffer eine abspielbare MP4 mit 300 s ± 10 s, `ffprobe` bestätigt Dauer und Video-Stream.
6. Zur Aufnahme existieren JSON-Sidecar (Score, Komponenten, Fenster, Trigger-Grund) und
   JPEG-Thumbnail.
7. Änderung des Speicherorts im UI wird validiert (nicht beschreibbar ⇒ Fehlermeldung,
   kein Speichern) und von der nächsten Aufnahme verwendet.
8. `pytest -q` grün; Tests decken ab: Score-Fusion/Hysterese/Cooldown, Fenster- und
   Segmentwahl, Pfadvalidierung, Retention, GQL-Parsing (gemockt).
9. Ohne Twitch-Zugangsdaten lauffähig (anonymer Modus); mit gesetzten Credentials
   Helix-Pfad aktiv.
10. README erklärt Installation, Speicherort-Wahl, ToS-Hinweis.

## 11. Nicht im Umfang

- Eigentlicher Clipschnitt/Hochladen (nur Sequenzen liefern).
- Multi-User-Auth/Accounts (Einzelbetreiber, Zugriffsschutz über Reverse-Proxy möglich).
- Aufnahme von VODs, YouTube, Kick.
- ML-Modell für „virality“ (Heuristik ist bewusst erklärbar; Erweiterungspunkt dokumentiert).
