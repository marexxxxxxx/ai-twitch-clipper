# AI Twitch Clipper

Selbst gehosteter Dienst, der eine Liste von Twitch-Streamern dauerhaft überwacht,
wahrscheinlich virale Momente heuristisch erkennt und daraus automatisch
**Sequenzen von fünf Minuten** aus einem Ringpuffer auf die Festplatte schreibt.
Die eigentlichen Clips schneidet ein separates, nachgelagertes Modul.

Enthalten sind: FastAPI-Backend, Weboberfläche (deutsch, ohne Build-Schritt),
SQLite-Persistenz, Segmentrekorder (streamlink + ffmpeg), Chat-/Clip-/Viewer-Signale,
Detektor mit Hysterese und Cooldown, Zusammenbau inkl. JSON-Sidecar und JPEG-Thumbnail,
Retention, Dockerfile und docker-compose.

---

## Schnellstart

Voraussetzungen: Docker mit Compose-Plugin, ca. 2 GB freier Speicher für den Puffer
(bei 720p60 und einem Streamer), Netzwerkzugriff auf Twitch.

```bash
# Host-Ordner für Aufnahmen anlegen (wird als /data in den Container gemountet)
mkdir -p clips

# Bauen und starten
docker compose up -d --build

# Health-Check
curl http://localhost:8000/api/health
# {"status":"ok"}
```

Danach die Oberfläche öffnen: **http://localhost:8000**

| Seite | Zweck |
|---|---|
| `/` | Dashboard: Live-Status, Zuschauer, Puffer, Score-Sparkline, letzte Aufnahmen |
| `/streamers` | Streamer einzeln oder per Massen-Import hinzufügen, pausieren, löschen |
| `/recordings` | Aufnahmen ansehen, herunterladen, Sidecar einsehen, löschen |
| `/settings` | Speicherort, Schwellen, Vor-/Nachlauf, Cooldown, Qualität, Retention |
| `/logs` | letzte 500 Logzeilen mit Auto-Refresh |
| `/docs` | OpenAPI-Dokumentation |

Der Container läuft nach dem Entrypoint als non-root-Benutzer `app` (UID 1000).
Der Entrypoint passt lediglich die Eigentümerschaft des gemounteten `/data` an und
gibt dann die Rechte ab.

### Host-Ordner frei wählen

Standardmäßig liegt der Host-Ordner unter `./clips`. Über eine `.env` oder
Umgebungsvariable lässt sich ein beliebiger Pfad verwenden:

```bash
# .env
HOST_CLIPS_DIR=/srv/twitch-aufnahmen
PORT=8000
TZ=Europe/Berlin
```

```bash
docker compose up -d
```

Der Container sieht den Ordner immer als `/data`. Wenn im UI ein anderer
Speicherort eingetragen wird, muss dieser Pfad im Container existieren und
beschreibbar sein – bei einem Host-Pfad also zusätzlich als Volume mounten:

```yaml
volumes:
  - ./clips:/data
  - /srv/zweite-platte:/mnt/clips2
```

Im UI dann `/mnt/clips2` eintragen und „Pfad prüfen“ nutzen. Nicht beschreibbare
Pfade werden abgelehnt und nicht gespeichert.

---

## Streamer hinzufügen

Über die Oberfläche unter `/streamers` oder per API:

```bash
curl -X POST http://localhost:8000/api/streamers \
  -H 'Content-Type: application/json' \
  -d '{"login":"caedrel"}'
```

Der Login wird anonym über Twitch GraphQL aufgelöst. Unbekannte Namen liefern
HTTP 404. Der Kanal muss zum Anlegen nicht live sein. Die Überwachung startet
automatisch; ein Offline-Kanal wird alle 30 Sekunden geprüft. Nach dem Erkennen
eines Live-Streams beginnt die Aufnahme in den Ringpuffer (2-Sekunden-Segmente,
maximal 360 Sekunden).

### Mehrere Kanäle auf einmal

Unter `/streamers` gibt es dafür das Panel **„Mehrere Kanäle importieren“**.
Pro Zeile (oder durch Komma/Semikolon getrennt) sind erlaubt:

```text
https://www.twitch.tv/shroud
#caedrel
@forsen
twitch.tv/example/videos
```

Bereits vorhandene Kanäle werden übersprungen, unbekannte als „nicht gefunden“
gemeldet. Maximal 200 Kanäle pro Import. Dasselbe per API:

```bash
curl -X POST http://localhost:8000/api/streamers/bulk \
  -H 'Content-Type: application/json' \
  -d '{"input":"https://twitch.tv/shroud\n#caedrel\nforsen"}'

# Antwort:
# {"counts":{"added":3,"duplicate":0,"not_found":0,"invalid":0},
#  "items":[{"input":"https://twitch.tv/shroud","login":"shroud",
#            "status":"added","detail":"Hinzugefügt."}, ...]}
```

Ist Twitch nicht erreichbar, antwortet der Endpunkt mit HTTP 502 und legt
**keinen** der Einträge an.

---

## Maßstab: 40 Streams gleichzeitig

Gemessen mit einem 720p60-Stream (streamlink + ffmpeg, Ringpuffer 360 s):

| Messgröße | ein Stream | 40 Streams (Hochrechnung) |
|---|---|---|
| RAM (RSS) | ~117,5 MiB | ~4,7 GiB |
| Download | ~3,4 Mbit/s | ~136 Mbit/s |
| Schreiblast Ringpuffer | ~0,43 MB/s | ~17–18 MB/s |
| Ringpuffer (360 s) | ~160 MB | ~6,2 GiB |

Eine fertige 300-s-Sequenz belegt ca. 130 MB.

**Empfehlung für 40 × 720p60:** 4 CPU-Kerne, 8 GB RAM, 200 Mbit/s Netzwerk,
SSD mit mindestens 100 GB freiem Speicher. Bei 480p halbieren sich Bandbreite
und Schreiblast. Der hier vermessene Host (2 Kerne, 4,1 GiB verfügbar, 58 GB
frei) reicht für 40 × 720p60 **nicht**; auf diesem Host sind ~10–12 Streams
realistisch.

`max_concurrent_streams` (Standard 40, 1–200) begrenzt, wie viele Live-Kanäle
tatsächlich gleichzeitig aufnehmen. Weitere Live-Kanäle bleiben in der
Warteschlange; die Streamer-Seite zeigt sie als `WARTESCHLANGE` mit „Platz n“,
das Dashboard zeigt aktive Streams, Warteschlange, Prozesse, Speicher und die
Twitch-Anfragen je Zyklus. Bei 40 überwachten Kanälen sind das 3
Batch-Anfragen pro Zyklus (15 + 15 + 10); Clips laufen in einem separaten,
kleineren Poll. Die Einstellung liegt unter `/settings` im Panel „Kapazität“.
Ohne cgroup-Limit greift der Speicher-Guard auf `MemAvailable` zurück und
startet bei weniger als 1 GB frei keine neuen Aufnahmen.

---

## Wie die Erkennung funktioniert

Alle Signale werden pro Aufnahme-Session in **5-Sekunden-Buckets** geführt, mit
15 Minuten Historie. Jeder Tick (alle 5 s) erzeugt einen Score und wird als
`SignalSample` gespeichert (Grundlage für Sparkline und Sidecar).

| Signal | Quelle | Score |
|---|---|---|
| `chat_rate` | anonymer IRC-WebSocket | Nachrichten/5 s gegen Median der letzten 15 min (ohne letzte 60 s): `(rate − 2·median) / (4·median + 4)`, auf 0…1 begrenzt; Ausreißer auf 99. Perzentil gekappt; erst ab 5 min Historie |
| `emote_burst` | Chat | reine Emote-Nachrichten + Hype-Emotes (`KEKW, LUL, OMEGALUL, Pog, PogChamp, monkaS, PepeHands, catJAM, Clap, ICANT, HUH`) je 5 s gegen eigenen Median, gleiche Formel |
| `clip_created` | separater GraphQL-Clip-Poll alle 30 s | neuer Clip mit `createdAt` in den letzten 180 s ⇒ 1.0; `clip_burst = min(1, n/3)` als Verstärker |
| `viewers` | zentraler GraphQL-Poll alle 30 s | relative Änderung über 120 s: `clamp(delta / 0.10, 0, 1)`, nur positiv |
| `audio` | optional | in dieser Version **nicht aktiv** (siehe Abweichungen) |

**Chat-Modi (`ROOMSTATE`):** Der Chat-Reader wertet die `ROOMSTATE`-Events von
Twitch aus (`subs-only`, `emote-only`, `followers-only`, `slow`). Bei einem
Moduswechsel werden die bisherigen 5-s-Buckets verworfen und Chat- sowie
Emote-Signal für **120 s** ausgesetzt (Settle), damit der Übergang keinen
Trigger auslöst. Danach wird der Median ausschließlich aus Buckets der neuen
Phase gebildet; fehlende Buckets zählen erst ab 12 vergangenen Buckets als
Stille. Der aktive Modus erscheint im Dashboard und auf der Streamer-Seite
(z. B. „Chat: verbunden · Sub-only“). Ehrlich eingeordnet: In Sub-only ist die
absolute Chatmenge kleiner, die Erkennung bleibt relativ zum Kanal-Median. Es
gibt **keine automatische Schwellenanpassung** – die Schwelle kann unter
`/settings` manuell gesenkt werden.

**Ein zentraler Poll:** Live-Status und Viewer kommen aus demselben Batch-Poll
alle 30 s (`live_poll_s`). Der frühere separate 20-s-Viewer-Poll entfiel, weil
Twitch pro Anfrage maximal 15 Root-Field-Aliase erlaubt und ein zweiter Poll
das Request-Budget verdoppeln würde. Clips werden davon entkoppelt in
kleineren Batches (`CLIP_BATCH_SIZE = 5`) ebenfalls alle 30 s geholt.

**Fusion:** `score = Σ wᵢ·sᵢ / Σ wᵢ` mit den Defaults
`chat_rate 0.35, emote_burst 0.20, clip_created 0.30, viewers 0.15`
sowie `clip_burst 0.10` als zusätzlichem Gewicht. Die Summe wird normalisiert.

**Trigger:** `score ≥ threshold` (Default 0.60) in **zwei aufeinanderfolgenden
Ticks**, Cooldown (Default 180 s) abgelaufen und der Puffer enthält mindestens
den Vorlauf (Default 240 s). Der Cooldown beginnt mit dem Trigger.

**Fenster:** `[T − Vorlauf, T + Nachlauf]`, Default also 240 s + 60 s = 300 s.
Es wird gewartet, bis der Rekorder das Fensterende erreicht hat (Timeout 5 min).
Randsegmente werden ganz genommen, dadurch ergibt sich eine Länge von
300 s ± Segmentlänge. Endet der Stream vorher, wird das vorhandene Material
verwendet und die Aufnahme als `truncated` markiert.

---

## Aufnahme, Dateien und Sidecar

Während einer Session:

```
{STORAGE_ROOT}/_buffer/{login}/{stream_id}/seg_00000.ts …
{STORAGE_ROOT}/_buffer/{login}/{stream_id}/index.csv
```

Nach einem Trigger:

```
{STORAGE_ROOT}/{login}/{YYYY-MM-DD}/{HHMMSS}_{score100}_{recording_id}.mp4
{STORAGE_ROOT}/{login}/{YYYY-MM-DD}/{HHMMSS}_{score100}_{recording_id}.json
{STORAGE_ROOT}/{login}/{YYYY-MM-DD}/{HHMMSS}_{score100}_{recording_id}.jpg
```

`HHMMSS` ist der UTC-Triggerzeitpunkt (nicht der Fensterbeginn), `score100` der
Score als dreistellige Prozentzahl (z. B. `087`). Das Video wird ohne Neukodierung
zusammengebaut (`-c copy -movflags +faststart`), das Thumbnail bei 20 % der
Laufzeit erzeugt.

Das JSON-Sidecar enthält unter anderem:

- `score` und `components` (gewichtete Einzelscores)
- `weights` und `components_detail` (Rohwerte, Mediane, Clip-Anker)
- `trigger`: Art (`auto`/`manual`), Grund, Schwelle, Notizen, Pufferstand
- `window`: Start/Ende in UTC, Vor-/Nachlauf, geplante und tatsächliche Medienzeit
- `segments`: Anzahl und erste/letzte Segmentdatei
- `file`: Dateiname, Dauer, Größe, Thumbnail

### Manuell auslösen

Im Container:

```bash
docker compose exec clipper \
  python -m app.tools.simulate_trigger --login caedrel --wait
```

Oder per API:

```bash
curl -X POST http://localhost:8000/api/trigger \
  -H 'Content-Type: application/json' \
  -d '{"login":"caedrel","reason":"Test"}'
```

Ein manueller Trigger ist nur möglich, wenn eine Aufnahme läuft und der Puffer
mindestens den Vorlauf enthält (Default 240 s). Andernfalls antwortet die API
mit HTTP 409 und einer erklärenden Meldung.

### Aufbau prüfen

```bash
docker compose exec clipper sh -c 'ls -lh /data/caedrel/*/'
docker compose exec clipper sh -c 'ffprobe -v error -show_entries format=duration \
  -show_entries stream=codec_type -of json /data/caedrel/*/*.mp4'
```

---

## Einstellungen

Alle Werte liegen in der Tabelle `setting` und sind im UI unter `/settings`
änderbar. Änderungen greifen für die **nächste** Aufnahme; laufende Puffer
bleiben im bisherigen Verzeichnis.

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `storage_root` | `/data` (ENV `STORAGE_ROOT`) | Wurzel für Puffer und fertige Aufnahmen |
| `pre_roll_s` | 240 | Vorlauf in Sekunden |
| `post_roll_s` | 60 | Nachlauf in Sekunden |
| `threshold` | 0.60 | Trigger-Schwelle |
| `cooldown_s` | 180 | Mindestabstand zwischen Triggern |
| `quality` | `720p60,best` | streamlink-Kandidatenliste |
| `retention_days` | 14 | Aufbewahrung fertiger Aufnahmen |
| `max_gb_per_streamer` | 50 | Quota je Streamer; älteste Aufnahmen zuerst |
| `weights` | siehe oben | Gewichte der Fusion |
| `audio_enabled` | false | reserviert, derzeit ohne Funktion |

**Pfadprüfung:** Beim Speichern wird der Zielordner angelegt (falls nötig) und
mit einer Temp-Datei real beschrieben. Schlägt das fehl, bleibt der alte Wert
erhalten und die API liefert HTTP 400 mit Begründung. Der Button „Pfad prüfen“
zeigt zusätzlich freien und gesamten Platz.

**Retention:** stündlich werden Aufnahmen älter als `retention_days` sowie
Überschreitungen der Quota je Streamer gelöscht (MP4, JSON, JPG und DB-Zeile).
Das gilt auch für `failed`-Aufnahmen; hängengebliebene `pending`/`assembling`
werden nach 24 Stunden entfernt, verwaiste `*.part.mp4` nach 6 Stunden.
Verwaiste Pufferordner werden nach 6 Stunden entfernt, Logs nach 14 Tagen,
Signalproben nach 2 Tagen.

---

## Twitch-Zugangsdaten (optional)

Ohne Zugangsdaten läuft alles anonym über die öffentliche Web-GraphQL-Client-ID
`kimne78kx3ncx6brgo4mv6wki5h1ko`. Das ist der Standardmodus.

Mit App-Zugangsdaten wird Helix als primäre Quelle genutzt:

```bash
# .env
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
```

```bash
docker compose up -d
docker compose logs | grep "Twitch-Client-Modus"
# … Twitch-Client-Modus: helix
```

Der Helix-Pfad bezieht ein App-Access-Token über `client_credentials` und nutzt
`/helix/users`, `/helix/streams` und `/helix/clips`. Schlägt der Token-Abruf
fehl, erscheint der Fehler im UI und in den Logs; die Aufnahme startet nicht.

---

## Tests

```bash
# im Container (empfohlen, Python 3.12, ffmpeg vorhanden)
docker compose run --rm --no-deps clipper pytest -q

# lokal (Python ≥ 3.12, Abhängigkeiten aus requirements.txt)
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest -q
```

Abgedeckt: Score-Fusion/Gewichte, Hysterese, Cooldown, Puffervoraussetzung,
Chat-/Emote-/Viewer-/Clip-Signale, Fenster- und Segmentwahl, Pfadvalidierung,
Einstellungsvalidierung, Retention/Quota, GraphQL-Parsing (gemockt) und
Index-Parsing des Rekorders.

---

## Projektstruktur

```
app/
  main.py                 FastAPI, Lifespan (Supervisor + Retention)
  config.py               Boot-Konfiguration (pydantic-settings)
  db.py                   SQLAlchemy 2 async + SQLite
  models.py               Streamer, Recording, Setting, SignalSample, LogEntry
  runtime_settings.py     Einstellungen, Validierung, Cache
  storage.py              Pfadprüfung, freier Platz, atomare Writes
  supervisor.py           Task je Streamer, Session-Lifecycle, Backoff
  retention.py            Aufräumen nach Policy
  dashboard_data.py       gemeinsame Dashboard-Daten
  logging_setup.py        Konsole, UI-Puffer, DB-Persistenz
  twitch/gql.py           anonymer GraphQL-Client
  twitch/helix.py         optionaler Helix-Client
  twitch/chat.py          anonymer IRC-WebSocket
  capture/recorder.py     streamlink | ffmpeg → 2-s-Segmente
  capture/buffer.py       Ringpuffer, Segmentwahl, Disk-Guard
  detect/signals.py       5-s-Buckets, Chat/Emote/Clip/Viewer
  detect/detector.py      Fusion, Hysterese, Cooldown
  clip/assembler.py       Fenster, concat, Thumbnail, Sidecar
  api/                    REST-Endpunkte
  web/routes.py           server-gerenderte Seiten
  templates/, static/     UI (deutsch, ohne CDN)
  tools/simulate_trigger.py
tests/                    pytest
Dockerfile, docker-compose.yml, Makefile, .env.example
```

### Datenbank

SQLite unter `/data/clipper.db` (ENV `DATA_DIR`). WAL-Modus, `foreign_keys=ON`.
Die Datenbank liegt bewusst außerhalb von `storage_root`, damit ein Wechsel des
Aufnahme-Speicherorts die Streamerliste nicht mitnimmt. Beim Löschen eines
Streamers bleiben Aufnahmen erhalten (`recording.streamer_id` wird `NULL`,
`streamer_login` bleibt denormalisiert gespeichert).

---

## Betrieb und Robustheit

- `restart: unless-stopped`, Healthcheck alle 20 s gegen `/api/health`.
- Ein Supervisor-Task je Streamer mit exponentiellem Backoff (5 s … 300 s).
- Der Segmentindex wird inkrementell ab dem letzten Byte-Offset gelesen;
  ffmpeg rotiert `index.csv` über `-segment_list_size` auf 400 Einträge.
  Der Recorder-Zustand folgt dem Pufferfenster, damit weder Speicher noch
  Tick-Laufzeit mit der Sessiondauer wachsen.
- streamlink und ffmpeg laufen als Kindprozesse; bei SIGTERM werden sie beendet,
  laufende Zusammenbauten markiert, `*.part.mp4` entfernt und der Puffer aufgeräumt.
- Watchdog: Liefert die Aufnahme 90 s keine neuen Segmente, wird die Session
  beendet und neu gestartet (z. B. nach Twitch-Aussetzern).
- Disk-Guard: unter 2 GB frei startet keine neue Aufnahme; unter 1 GB wird die
  laufende Aufnahme sauber beendet. Beides wird geloggt und im UI angezeigt.
- Chat-Reconnect mit Backoff; Fehler einzelner Signale stoppen nie die Aufnahme.
- Interne Zeitrechnung durchgängig UTC; `TZ` wirkt nur auf die Anzeige.

### Logs

```bash
docker compose logs -f --tail=200
```

Die gleichen Zeilen stehen unter `/logs` (max. 500) und in der Tabelle
`log_entry`.

---

## Grenzen und rechtliche Hinweise

- **Twitch-ToS:** Das Projekt lädt Streams herunter. Das ist nur zulässig, wenn
  du die Rechte hast bzw. Twitch und die Streamer dem zustimmen. Prüfe die
  aktuellen Twitch-Nutzungsbedingungen und die Regeln der jeweiligen Kanäle.
  Verwende die Software nicht, um fremde Inhalte weiterzuverbreiten.
- **Kein DRM:** Geschützte Streams können nicht aufgenommen werden.
- **Werbung:** `--twitch-disable-ads` überspringt erkannte Werbesegmente; das
  kann zu Lücken und kürzeren Sequenzen führen. Twitch kann das Verhalten
  jederzeit ändern.
- **GraphQL ist nicht offiziell:** Der anonyme Modus nutzt eine öffentliche
  Web-Client-ID. Twitch kann Abfragen, Limits oder das Format ändern. Für
  stabilen Betrieb sind Helix-Zugangsdaten empfohlen.
- **Keine VODs, kein YouTube/Kick**, kein Upload, kein Clipschnitt, keine
  ML-Bewertung. Die Heuristik ist bewusst erklärbar; neue Signale lassen sich in
  `detect/signals.py` ergänzen.
- **Ressourcen:** 720p60 erzeugt ca. 130 MB pro 5-Minuten-Sequenz. Der
  Ringpuffer belegt zusätzlich bis zu 360 s pro Streamer. Speicherplatz und
  Schreiblast vor dem Dauerbetrieb prüfen.
- **Keine Authentifizierung:** Der Dienst ist für ein vertrauenswürdiges Netz
  gedacht. Für öffentlichen Zugriff einen Reverse-Proxy mit Auth davorsetzen.

---

## Troubleshooting

**Container wird nicht healthy**
```bash
docker compose ps
docker compose logs --tail=200
curl -v http://localhost:8000/api/health
```
Häufig: Port 8000 belegt (`PORT=8080 docker compose up -d`), fehlende
Schreibrechte auf dem Volume, oder die DB liegt auf einem nicht gemounteten Pfad.

**`permission denied` unter `/data`**
Der Container läuft als UID 1000. Auf dem Host:
```bash
sudo chown -R 1000:1000 clips
```
Der Entrypoint versucht das beim Start automatisch, kann es aber nur, wenn er
als root startet und der Mount es zulässt.

**Streamer bleibt OFFLINE, obwohl er live ist**
- Login prüfen (`/api/twitch/live` zeigt live Kanäle).
- Twitch GraphQL blockiert evtl. die Client-ID oder Rate-Limit; Logs ansehen.
- Systemzeit/Zeitzone prüfen (TLS und `createdAt`-Vergleiche).
- Bei Helix: Client-ID/Secret und App-Token prüfen.

**Keine Segmente / Aufnahme startet nicht**
```bash
docker compose exec clipper streamlink --stdout twitch.tv/<login> 720p60,best | head -c 100000 > /dev/null
```
- `streamlink` muss eine Playlist finden (Kanal live, keine Geo-Sperre).
- FFmpeg muss im Image vorhanden sein (`ffmpeg -version`).
- Freien Platz prüfen; unter 2 GB startet keine Aufnahme.

**Kein Trigger, obwohl es „viral“ wirkt**
- Chat-Signale brauchen 5 Minuten Historie.
- Schwelle im UI prüfen; Score-Sparkline beobachten.
- Cooldown und Puffervorlauf beachten.
- Clip-Signal greift nur bei Clips, die nach Session-Start erzeugt wurden.

**Aufnahme ist nicht exakt 300 s**
Das ist erwartet: Randsegmente werden ganz genommen (2-s-Raster), daher
300 s ± Segmentlänge; durch MP4-Priming/Audio-Padding können praktisch bis zu
ca. 3 s dazukommen. Die Abnahme toleriert ±10 s.

**Thumbnail fehlt**
Die Datei wird bei 20 % der Laufzeit erzeugt. Bei sehr kurzen/truncated
Aufnahmen wird auf 1 s bzw. 0 s zurückgefallen. Fehlt sie trotzdem, Logs prüfen
und die MP4 mit `ffprobe` validieren.

**Speicher voll**
Retention im UI verkürzen, `max_gb_per_streamer` senken, alte Aufnahmen unter
`/recordings` löschen. Der Disk-Guard verhindert neue Aufnahmen unter 2 GB.

---

## Abweichungen von der Spezifikation

Dokumentiert, weil die Spec Abweichungen nur mit Begründung zulässt:

1. **Audio-Signal** (`audio_enabled`) ist als Einstellung vorhanden, aber nicht
   implementiert. Die Spec führt es selbst als optional und standardmäßig aus
   („nur dokumentieren, nicht im Default-Pfad“).
2. **Zusätzliche Spalte `recording.streamer_login`** und Tabelle `artifact`:
   Aufnahmen bleiben nach dem Löschen eines Streamers lesbar (FK wird `NULL`).
   `artifact` ist für Erweiterungen reserviert und derzeit ungenutzt.
3. **Manueller Trigger** wird abgelehnt, solange der Puffer den Vorlauf nicht
   deckt (HTTP 409). Das verhindert kurze, irreführende „5-Minuten“-Dateien.
4. **Clip-Anker:** Der Anker (`createdAt − duration/2`) wird im Sidecar
   dokumentiert, das Aufnahmefenster bleibt aber um den Triggerzeitpunkt T
   zentriert. Bei Twitch-Clips (max. 60 s) liegt der Anker innerhalb des
   Vorlaufs; so bleibt die Länge garantiert.
5. **Thumbnail** ist Best-Effort: Schlägt die JPEG-Erzeugung fehl, bleibt die
   MP4 nutzbar und der Status ist weiterhin `ready`. In der Praxis wird das
   Thumbnail bei 20 %, 1 s und 0 s versucht.
6. **Container-Entrypoint** startet als root, korrigiert die Rechte des
   Volume-Mounts und wechselt dann per `gosu` zu `app`. Der Anwendungsprozess
   selbst läuft als non-root (UID 1000).
7. **`PORT`** in Compose steuert den Host-Port; der Container lauscht intern
   immer auf 8000, damit der Healthcheck unabhängig von der Konfiguration
   funktioniert.
8. **streamlink-Abhängigkeiten** sind auf `urllib3 < 2.6` und
   `requests < 2.33` begrenzt, weil neuere Versionen die Twitch-Anfragen von
   streamlink 7.x brechen.
9. **Kanal-Auflösung** nutzt im anonymen Modus die öffentliche Web-Client-ID;
   das ist kein offizieller API-Vertrag (siehe Grenzen).
