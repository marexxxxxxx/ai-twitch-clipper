# AI Twitch Clipper — Docker

Betrieb des Clippers als Container. Die vollständige Funktionsbeschreibung steht
in [`README.md`](README.md).

## Bauen und starten

```bash
mkdir -p clips
docker compose up -d --build
curl http://localhost:8000/api/health
# {"status":"ok"}
```

Die Oberfläche läuft unter **http://localhost:8000**, standardmäßig mit dem
Host-Ordner `./clips` als `/data`.

| Dienst | Zweck | Adresse |
| --- | --- | --- |
| `clipper` | Recorder, Detektor und Web-UI | `http://<host>:8000` |

Der Container läuft als non-root-Benutzer `app` (UID 1000). Der Entrypoint passt
beim Start die Eigentümerschaft des gemounteten `/data` an, sofern er als root
starten darf.

## Konfiguration

`.env` oder Umgebung:

| Variable | Default | Bedeutung |
| --- | --- | --- |
| `PORT` | `8000` | Host-Port der UI; der Container lauscht intern immer auf 8000 |
| `HOST_CLIPS_DIR` | `./clips` | Aufnahmeordner auf dem Host, gemountet als `/data` |
| `TZ` | `Europe/Berlin` | Zeitzone für die Anzeige; intern wird UTC gerechnet |
| `QUALITY` | `720p60,best` | streamlink-Qualitätskandidaten |
| `TWITCH_CLIENT_ID` | – | optionaler Helix-Modus |
| `TWITCH_CLIENT_SECRET` | – | optionaler Helix-Modus |

Die Datenbank liegt unter `/data/clipper.db` (WAL-Modus) und bleibt bei einem
Wechsel des Aufnahmeordners erhalten, solange `/data` gemountet ist.

## Einstellungen im UI

Alle Laufzeitwerte stehen unter `/settings` und greifen für die nächste
Aufnahme:

| Einstellung | Default (Neuinstallation) | Bedeutung |
| --- | ---: | --- |
| `threshold` | 0,40 | Trigger-Schwelle |
| `required_ticks` | 1 | Aufeinanderfolgende Ticks über der Schwelle (1–5) |
| `max_clips_per_hour` | 6 | Automatische Clips je Streamer und Stunde (1–60) |
| `cooldown_s` | 180 | Mindestabstand zwischen Triggern |
| `pre_roll_s` / `post_roll_s` | 240 / 60 | Vor- und Nachlauf in Sekunden |
| `shadow_mode` | true | Neue Chat-Signale nur messen, nicht fusionieren |
| `streamer_command_triggers` | true | Streamer-/Mod-Clip-Kommando löst direkt aus |
| `hype_keywords` | Standardliste | Kommagetrennte Hype-/Lacher-Begriffe (max. 500 Zeichen) |
| `copypasta_min_users` | 3 | Mindestzahl verschiedener User für Copypasta (2–10) |
| `copypasta_window_s` | 10 | Zeitfenster für Copypasta (5–30 s) |
| `weights` | siehe `README.md` | Gewichte der zehn Signale |
| `retention_days` | 14 | Aufbewahrung fertiger Aufnahmen |
| `max_gb_per_streamer` | 50 | Quota je Streamer |
| `max_concurrent_streams` | 40 | Gleichzeitig aufgenommene Live-Kanäle |

Bestehende Installationen behalten `threshold` und `weights`. Beim Update werden
nur fehlende Schlüssel ergänzt, mit `required_ticks=2` und
`max_clips_per_hour=60`; der Rollout setzt sie anschließend bewusst auf 1 bzw. 6.

## Chat-Tracking (Paket A)

Der Clipper wertet zusätzlich fünf Chat-Signale aus:

| Signal | Bedeutung |
| --- | --- |
| `unique_chatters` | verschiedene User je 5-Sekunden-Bucket |
| `hype_keywords` | Treffer aus der konfigurierbaren Keyword-Liste |
| `copypasta` | derselbe Text von mindestens 3 verschiedenen Usern |
| `clip_commands` | `!clip`, „clip it/that/das“, „clippen“, `!highlight` |
| `streamer_command` | Clip-Kommando mit Broadcaster-/Mod-Badge |

Alle Komponenten werden pro Tick in `signal_sample.components` gespeichert; die
aggregierten Rohwerte (Zähler, Mediane, Keywords, User-Anzahl) landen in
`signal_sample.detail`.

Im **Shadow-Modus** (`shadow_mode=true`, Standard) zählen die neuen Signale mit
Gewicht 0 nicht in den Score. Der direkte Streamer-/Mod-Trigger ist aus. Damit
lassen sich die Signale gefahrlos an echten Daten bewerten.

### Shadow-Report

```bash
# Letzte 48 Stunden
docker compose exec clipper python -m app.tools.shadow_report \
  --db /data/clipper.db --hours 48

# Zusätzlich Chat-Profil und Trigger-Pipeline simulieren
docker compose exec clipper python -m app.tools.shadow_report \
  --db /data/clipper.db --hours 48 --simulate-profile

# Optional auf einen Streamer begrenzen
docker compose exec clipper python -m app.tools.shadow_report \
  --db /data/clipper.db --hours 48 --streamer caedrel
```

Der Report zeigt Tick-Anzahl, p50/p95/max je Signal, Top-Momente, die
Aggregatwerte aus `detail` und mit `--simulate-profile` die
„Would-trigger“-Momente samt erwarteter Clip-Rate. Die Datenbank wird nur lesend
geöffnet.

### Profil anwenden

Nach 24–48 h Beobachtung setzt der Button **„Chat-Profil anwenden“** unter
`/settings` (mit Rückfrage):

- `threshold = 0.40`
- `required_ticks = 1`
- `max_clips_per_hour = 6`
- `shadow_mode = false`
- `streamer_command_triggers = true`
- `weights = CHAT_PROFILE_WEIGHTS`

Das Chat-Profil ist ein Startwert. Bei zu vielen Clips Schwelle erhöhen,
`required_ticks` auf 2 setzen oder das Stundenlimit senken.

### Manuell clippen

Auf `/streamers` gibt es pro Zeile den Button **„Jetzt clippen“**
(`POST /api/trigger`). Er umgeht Cooldown und Stundenlimit bewusst, zählt aber
als Recording für das Stundenlimit der automatischen Trigger.

## Tests

```bash
docker run --rm --entrypoint python \
  -v "$PWD":/app -w /app ai-twitch-clipper:latest -m pytest -q
```

## Neu bauen und neu starten

```bash
docker compose up -d --build clipper
docker compose ps
docker compose logs -f --tail=200
```

Der Healthcheck fragt alle 20 s `/api/health` ab. Bei einem Neustart laufen
laufende Zusammenbauten bis zu 90 s aus (`stop_grace_period`).
