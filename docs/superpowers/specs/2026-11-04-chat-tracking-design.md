# Chat-Tracking & Trigger-Tuning (Paket A)

- **Datum:** 2026-11-04
- **Status:** Entwurf zur Freigabe
- **Repo:** `ai-twitch-clipper`
- **Umfang:** Paket A aus der Diskussion vom 2026-11-04

## 1. Kontext

Der Clipper läuft, nimmt drei Streamer auf und hält den Ringpuffer gefüllt. Zunächst
entstanden keine Aufnahmen. Die Auswertung der gespeicherten `signal_sample`-Ticks
zeigt:

| Streamer | Max. Score | Bester Moment | Komponenten |
|---|---:|---|---|
| nooreax | 0,50 | 19:32:18 | Chat 1,0; Emotes 1,0; Clip 0; Zuschauer 0 |
| letshugotv | 0,42 | 19:37:42 | Chat 0,75; Emotes 1,0; Clip 0; Zuschauer 0 |
| trymacs | 0,36 | 19:30:41 | Chat 1,0; Emotes 0; Clip 0; Zuschauer 0 |

> **Stand der laufenden Installation (2026-11-04, nach der ersten Analyse):**
> In der DB stehen inzwischen `threshold=0.40` und eigene Gewichte
> (`chat_rate=0.53`, `emote_burst=0.51`, `clip_created=0.30`, `viewers=0.15`,
> `clip_burst=0.10`). Um 19:53 wurde bereits eine Aufnahme mit Score 0.428
> erzeugt. Diese Werte werden bei diesem Schnitt **nicht überschrieben**; die
> Analyse unten bezieht sich auf den ursprünglichen Stand mit 0,60 und den
> Default-Gewichten.

Die Erkennung ist nicht defekt. Sie ist für diese Streams zu konservativ:

- Chat und Emotes allein erreichen mit den aktuellen Gewichten maximal 0,50.
- Die Schwelle liegt bei 0,60; zusätzlich müssen **zwei aufeinanderfolgende**
  5-Sekunden-Ticks über der Schwelle liegen.
- Neue Twitch-Clips gab es seit Aufnahmestart nicht; die Zuschauerzahlen waren stabil.
- Kurze, starke Chat-/Emote-Spikes fallen dadurch durch.

Ziel dieses Schnitts ist deshalb nicht „mehr Signale um jeden Preis“, sondern
**breitere, robustere Chat-Signale plus eine besser konfigurierbare Trigger-Logik**.
Die neuen Signale werden zunächst nur im Shadow-Mode gemessen, damit wir sie vor
dem Scharfschalten an echten Daten bewerten können.

## 2. Ziele

- Starke Chat-/Emote-Momente auch ohne neuen Twitch-Clip oder Zuschauer-Sprung erkennen.
- Chat-Menge durch aussagekräftigere Signale ergänzen:
  - verschiedene Chatter statt nur Nachrichten,
  - Hype-/Lacher-Sprache,
  - Copypasta/Wiederholungen,
  - explizite Clip-Kommandos,
  - Clip-Kommandos von Streamer/Moderatoren.
- Trigger-Logik konfigurierbar machen (`required_ticks`, Schwelle, Stundenlimit).
- Neue Signale gefahrlos einführen: Shadow-Mode, Speicherung in `SignalSample`,
  Auswertungsskript, danach bewusstes Aktivieren.
- Bestehende Aufnahme-, Assembler-, Watcher- und Cleanup-Pipeline unverändert lassen.
- Keine neue externe Abhängigkeit und keine Twitch-OAuth-Einrichtung.

## 3. Nicht-Ziele

- **Keine** Bits-, Subs-, Geschenk-Sub- oder Raid-Erkennung. Das ist Paket B.
- **Keine** Audio-/Video-/Facecam-Analyse. Das ist Paket B/C.
- **Kein** Twitch EventSub/Helix für Hype Train, Predictions oder Channel Points.
- **Keine** per-Streamer-Profile in diesem Schnitt. Das bleibt ein späterer Ausbau.
- **Keine** Änderung am Bridge-/Watcher-/Cleanup-Verhalten.

## 4. Datenfluss

```
Twitch IRC
  -> ChatReader (app/twitch/chat.py)
  -> ChatMessage mit user_id / badges / text / emotes
  -> SessionSignals (app/detect/signals.py)
       - bestehende Signale: chat_rate, emote_burst, clip_created, viewers, clip_burst
       - neue Signale: unique_chatters, hype_keywords, copypasta,
                       clip_commands, streamer_command
  -> Detector (app/detect/detector.py)
       - fusionierter Score
       - required_ticks, Cooldown, max_clips_per_hour
       - direkter Trigger bei Streamer-/Mod-Clip-Kommando (nur wenn Shadow-Mode aus)
  -> LiveSession.tick() (app/supervisor.py)
       - speichert alle Komponenten in signal_sample.components
       - speichert aggregierte Rohwerte in signal_sample.detail
       - löst bei Bedarf trigger() aus
```

`signal_sample.components` ist bereits JSON. Für die Auswertung der neuen Signale
kommt zusätzlich eine nullable JSON-Spalte `signal_sample.detail` hinzu. Sie wird
beim Start bei bestehenden SQLite-Datenbanken per `ALTER TABLE` ergänzt; ein
vollständiges Migrationsframework ist nicht nötig. Gespeichert werden nur
aggregierte Rohwerte (Zähler, Mediane, Treffer-Keywords, Anzahl User), keine
Chat-Texte oder User-Namen. Alte Zeilen ohne `detail` bleiben gültig.

## 5. Neue Chat-Signale

Alle neuen Burst-Signale verwenden dieselbe Infrastruktur wie `chat_rate`:

- 5-Sekunden-Buckets (`BUCKET_S = 5`)
- Vergleich gegen den Median der letzten 15 Minuten
- die letzten 60 Sekunden werden aus dem Median ausgeschlossen
- mindestens 5 Minuten Session-Historie (`MIN_HISTORY_S = 300`)
- 99. Perzentil-Kappung der Vergleichswerte
- Formel: `burst_score(metric, median) = clamp((metric - 2*median) / (4*median + 4))`

### 5.1 `unique_chatters`

**Frage:** Wie viele verschiedene Personen schreiben in einem 5-Sekunden-Fenster?
Das ist robuster als die reine Nachrichtenzahl, weil ein einzelner Spammer die
Chat-Rate stark verzerren kann.

- Datenquelle: `ChatMessage.user_id` aus dem IRC-Tag `user-id`.
- Fallback: `ChatMessage.user` (Anzeigename), falls `user-id` fehlt.
- Gemeinsame Identität: `identity(message) = user_id or user`; dieselbe
  Hilfsfunktion wird auch für `copypasta` verwendet.
- Bucket-Feld: `Bucket.users: set[str]`.
- Metrik: Anzahl unterschiedlicher User im letzten abgeschlossenen Bucket.
- Score: `burst_score(metric, median)`.
- Detail: `{value, median, samples}`.
- Kein zusätzlicher Zustand außer dem Set pro Bucket.

### 5.2 `hype_keywords`

**Frage:** Wie viele Nachrichten enthalten typische Hype-/Lacher-Sprache?
Das Signal ist absichtlich konfigurierbar, weil Sprache und Szene stark variieren.

- Datenquelle: `ChatMessage.text`.
- Standardliste (kommagetrennt, case-insensitive):
  `haha, hahaha, lol, lmao, xd, 😂, 🤣, geil, alter, digga, krass, lost, rip, gg, clutch, nein, wtf, omg, unfassbar, krank, böse, aufstehen, wallah, ehre, schwöre`
- Matching:
  - reine Buchstaben-/Zahlen-Keywords mit Wortgrenzen (`(?<!\w)…(?!\w)`),
  - Emojis als Literal.
- Metrik: Anzahl Nachrichten mit mindestens einem Treffer im letzten Bucket
  (nicht Anzahl der Treffer pro Nachricht).
- Score: `burst_score(metric, median)`.
- Detail: `{value, median, samples}`.
- Konfiguration: `hype_keywords` als String; leer = Standardliste.

### 5.3 `copypasta`

**Frage:** Wird derselbe Text von mehreren verschiedenen Leuten gleichzeitig
wiederholt? Das ist ein starkes, schwer zu fälschendes Hype-Signal.

- Normalisierung: Text kleinschreiben, URLs entfernen, Satzzeichen/Whitespace
  normalisieren, Emote-only-Nachrichten und Texte unter 4 Zeichen ignorieren.
- Zustand: `deque[(ts, normalized_text, identity)]`, maximal 2000 Einträge,
  Prune auf die letzten 30 Sekunden. `identity` ist `user_id or user` (wie 5.1).
- Erkennung: Ein Text gilt als Copypasta, wenn er innerhalb von
  `copypasta_window_s` (Standard 10 s) von mindestens
  `copypasta_min_users` (Standard 3) **verschiedenen** Usern gesendet wurde.
- Haltezeit: `copypasta` bleibt 15 Sekunden auf 1,0, danach 0,0.
- Score: binär 1,0/0,0 (kein Burst-Score nötig).
- Detail: `{active, users, window_s}`.
- Wichtig: Wiederholungen desselben Users zählen nicht als mehrere User.

### 5.4 `clip_commands`

**Frage:** Fordern Zuschauer aktiv einen Clip an? Das ist ein direkter Intent-Signal.

- Datenquelle: `ChatMessage.text`.
- Regex (case-insensitive, an Wortgrenzen):
  `(?:^|\s)(?:!clip|clip\s+(?:it|that|das)|clippen|!highlight)(?:\s|$)`
- Treffer werden pro Bucket gezählt.
- Score: `burst_score(metric, median)`.
- Detail: `{value, median, samples}`.
- Bewusst **nicht** enthalten: normales „highlight“ ohne `!`, weil das zu viele
  False Positives erzeugt.

### 5.5 `streamer_command`

**Frage:** Hat der Streamer oder ein Moderator selbst einen Clip angefordert?
Das ist das präziseste neue Signal.

- Datenquelle: `ChatMessage.badges` aus dem IRC-Tag `badges`.
- Erkennung: Badge enthält `broadcaster/1` oder `moderator/1` **und** der Text
  matcht die Clip-Command-Regex aus 5.4.
- Score: 1,0 für 30 Sekunden nach dem Kommando, sonst 0,0.
- Detail: `{hit, until}`.
- Zusätzlich: direkter Trigger (siehe Abschnitt 6), wenn
  `streamer_command_triggers = true` und Shadow-Mode aus ist.
- Kein direkter Trigger im Shadow-Mode, damit sich das Verhalten dort nicht ändert.

## 6. Trigger-Logik

### 6.1 Neue Einstellungen

| Einstellung | Standard | Bereich | Bedeutung |
|---|---:|---:|---|
| `threshold` | 0,40 | 0,05–1,0 | Fusionierter Score für einen Trigger |
| `required_ticks` | 1 | 1–5 | Wie viele aufeinanderfolgende Ticks ≥ Schwelle nötig sind |
| `max_clips_per_hour` | 6 | 1–60 | Obergrenze je Streamer und Stunde |
| `streamer_command_triggers` | true | bool | Streamer-/Mod-Kommando löst direkt aus |
| `shadow_mode` | true | bool | Neue Signale nur messen, nicht fusionieren |

`cooldown_s` bleibt standardmäßig 180 Sekunden. `pre_roll_s`/`post_roll_s`
bleiben unverändert (240/60).

### 6.2 Detector-Änderungen

- `Detector.configure()` bekommt `required_ticks` und `max_clips_per_hour`.
- Das Stundenlimit wird **dauerhaft** aus der `recording`-Tabelle gezählt:
  `count(recording) WHERE streamer_id = ? AND created_at >= now - 1h`.
  Dadurch überlebt es Stream-Reconnects und App-Neustarts.
- Neue Methode `can_trigger(now, buffer_s, recent_triggers=0) -> tuple[bool, list[str]]`
  prüft:
  - Cooldown abgelaufen,
  - Puffer ≥ `pre_roll_s`,
  - `recent_triggers < max_clips_per_hour`.
- `LiveSession` zählt die Recordings vor jedem Tick und übergibt den Wert an
  `detector.update(...)`; `update()` nutzt `can_trigger`.
- `Detector` kann `trigger_times` weiterhin für Tests/Snapshot führen, die DB
  ist aber die maßgebliche Quelle.
- `snapshot()` liefert zusätzlich `required_ticks`, `max_clips_per_hour` und
  `triggers_last_hour`.

### 6.3 Direkter Streamer-/Mod-Trigger

In `LiveSession.tick()`, nach `detector.update(...)`:

1. Wenn `detail["streamer_command"]["hit"]` true ist **und**
   `streamer_command_triggers` true ist **und** Shadow-Mode aus ist:
2. `can_trigger(now, buffer_s, recent_triggers)` prüfen.
3. Wenn erlaubt und nicht bereits durch `decision.trigger` ausgelöst:
   - `detector.mark_triggered(now)` aufrufen,
   - `trigger("auto", decision, manual=False, reason="Streamer/Mod-Clipping")`
     mit den aktuellen Komponenten aufrufen.
4. Fehler werden wie bei anderen Triggern geloggt, nicht nach außen geworfen.

Damit kann ein Moderator/Streamer auch bei ruhigem Chat sofort einen Clip erzeugen.
Cooldown und Stundenlimit gelten trotzdem.

### 6.4 Warum 0,40 und ein Tick?

Mit den alten Gewichten hätte `0,40 + 1 Tick` die beiden beobachteten
Near-Misses erwischt:

- nooreax 0,50 → Trigger,
- letshugotv 0,42 → Trigger,
- trymacs 0,36 → kein Trigger.

Das sind ca. 2 Clips pro 30 Minuten über drei große Streams. Der Cooldown von
180 s und `max_clips_per_hour = 6` verhindern eine Lawine. Wenn es zu viele
Clips werden, kann `threshold` erhöht oder `required_ticks` auf 2 gesetzt werden.

> **Hinweis zu den aktuellen Gewichten:** Die laufende Installation hat inzwischen
> eigene Gewichte (`chat_rate=0.53`, `emote_burst=0.51`, …) und `threshold=0.40`.
> Die Beispielrechnung oben bezieht sich auf die ursprünglichen Default-Gewichte.
> Mit den aktuellen Gewichten wurde um 19:53 bereits eine Aufnahme mit Score
> 0.428 erzeugt. Diese Werte werden beim Rollout nicht überschrieben.

## 7. Shadow-Mode

### 7.1 Verhalten

- `shadow_mode = true` (Standard):
  - Alle neuen Signale werden berechnet und in `signal_sample.components` gespeichert.
  - Ihre Gewichte werden für die Fusion auf 0 gesetzt.
  - Der Score und das Trigger-Verhalten entsprechen exakt dem bisherigen Verhalten
    (mit der neuen Schwelle/`required_ticks`).
  - Der direkte Streamer-/Mod-Trigger ist deaktiviert.
- `shadow_mode = false`:
  - Die konfigurierten Gewichte der neuen Signale zählen.
  - Der direkte Streamer-/Mod-Trigger ist aktiv, wenn `streamer_command_triggers`.

### 7.2 Auswertung

- `app/tools/shadow_report.py` liest die SQLite-DB und zeigt je Streamer:
  - Anzahl der Ticks,
  - p50/p95/max für jedes neue Signal,
  - die Top-Momente je Signal mit Zeitstempel, Legacy-Score und Komponenten,
  - Anzahl der Ticks ≥ 0,7 pro Signal,
  - die gespeicherten Rohwerte aus `signal_sample.detail` (Zähler, Mediane,
    Treffer-Keywords, Anzahl User).
- Optional simuliert der Report das Chat-Profil: hypothetischer Score mit
  `CHAT_PROFILE_WEIGHTS`, danach dieselbe Trigger-Pipeline mit Schwelle,
  `required_ticks`, Cooldown und Stundenlimit. Ausgabe: „would-trigger“-Momente
  und erwartete Clip-Rate. Aufruf mit `--simulate-profile`.
- Aufruf im Container:
  `python -m app.tools.shadow_report --db /data/clipper.db --hours 48`
- Optionaler Filter: `--streamer nooreax`.
- Zusätzlich loggt `LiveSession` bei einem neuen Signal ≥ 0,7 eine `[shadow]`-Zeile,
  höchstens einmal pro Streamer und 30 Sekunden.
- Nach 1–2 Tagen wird anhand des Reports entschieden, ob das Chat-Profil
  scharfgeschaltet wird.

### 7.3 Aktivierung

Die UI bekommt einen Button **„Chat-Profil anwenden“**. Er setzt:

- `threshold = 0.40`
- `required_ticks = 1`
- `max_clips_per_hour = 6`
- `shadow_mode = false`
- `streamer_command_triggers = true`
- `weights = CHAT_PROFILE_WEIGHTS` (siehe 8.2)

Vor dem Speichern wird per `confirm()` nachgefragt. Einzelne Werte können danach
weiter angepasst werden.

## 8. Gewichte und Einstellungen

### 8.1 Bestehende Gewichte (Shadow-Phase)

`DEFAULT_WEIGHTS` bleibt für die alten fünf Signale unverändert und bekommt die
neuen fünf Schlüssel mit Gewicht 0:

```python
DEFAULT_WEIGHTS = {
    "chat_rate": 0.35,
    "emote_burst": 0.20,
    "clip_created": 0.30,
    "viewers": 0.15,
    "clip_burst": 0.10,
    "unique_chatters": 0.0,
    "hype_keywords": 0.0,
    "copypasta": 0.0,
    "clip_commands": 0.0,
    "streamer_command": 0.0,
}
```

Damit bleibt der Score in der Shadow-Phase identisch. `sanitize_weights()` füllt
fehlende neue Schlüssel mit 0.

> Für die bestehende Installation werden beim Rollout die **aktuell gespeicherten**
> Gewichte gelesen und nur um die neuen Schlüssel mit 0 ergänzt. `DEFAULT_WEIGHTS`
> gilt für Neuinstallationen; es werden keine vorhandenen Gewichte überschrieben.

### 8.2 Chat-Profil (nach der Auswertung)

```python
CHAT_PROFILE_WEIGHTS = {
    "chat_rate": 0.20,
    "unique_chatters": 0.15,
    "emote_burst": 0.15,
    "hype_keywords": 0.10,
    "copypasta": 0.10,
    "clip_commands": 0.05,
    "streamer_command": 0.05,
    "clip_created": 0.10,
    "viewers": 0.05,
    "clip_burst": 0.05,
}
```

Summe = 1,00. Das Profil ist ein Startwert, kein Dogma. Nach dem Shadow-Report
kann es angepasst werden.

### 8.3 Neue Settings-Schlüssel

| Schlüssel | Typ | Standard | Validierung |
|---|---|---:|---|
| `required_ticks` | int | 1 | 1–5 |
| `max_clips_per_hour` | int | 6 | 1–60 |
| `shadow_mode` | bool | true | – |
| `streamer_command_triggers` | bool | true | – |
| `hype_keywords` | str | Standardliste | max. 500 Zeichen |
| `copypasta_min_users` | int | 3 | 2–10 |
| `copypasta_window_s` | int | 10 | 5–30 |

`threshold` bleibt ein bestehender Schlüssel; der Standard für Neuinstallationen
wird auf 0,40 gesetzt. `seed_defaults()` erkennt eine bestehende Installation
(nicht-leere `setting`-Tabelle) und legt die neuen Schlüssel rückwärtskompatibel
an: `required_ticks=2`, `max_clips_per_hour=60`, `shadow_mode=true`,
`streamer_command_triggers=true`, Standard-Keywords, `copypasta_min_users=3`,
`copypasta_window_s=10`. Vorhandene Werte (insbesondere `threshold` und
`weights`) bleiben unverändert. Das Chat-Profil wird nach dem Deployment
einmalig explizit per API/UI gesetzt (Abschnitt 11).

## 9. UI-Änderungen

### 9.1 Einstellungen

Neue Felder im Abschnitt „Erkennung“:

- `required_ticks` („Benötigte Ticks“)
- `max_clips_per_hour` („Max. Clips pro Stunde“)
- `shadow_mode` („Neue Chat-Signale nur messen“)
- `streamer_command_triggers` („Streamer/Mod-Kommando löst sofort aus“)

Neuer Abschnitt „Chat-Signale“:

- `hype_keywords` als Textarea (kommagetrennt)
- `copypasta_min_users`
- `copypasta_window_s`

Der Gewichte-Block zeigt alle zehn Signale. Die fünf neuen Felder werden mit
„(Schatten)“ markiert, solange `shadow_mode` aktiv ist. Die Hilfe erklärt, dass
Schatten-Gewichte den Score nicht beeinflussen.

Buttons:

- „Speichern“ (bestehend)
- „Chat-Profil anwenden“ (setzt die Werte aus 7.3)

### 9.2 Dashboard

- Die Komponentenliste der Streamer-Karten bekommt Labels für die neuen Signale.
- Neue Signale werden auch im Shadow-Mode angezeigt (Balken), aber mit einem
  Hinweis/Tooltip „Schatten-Signal – zählt nicht in den Score“.
- Das Stundenlimit und `required_ticks` können in den Runtime-Details angezeigt
  werden, sind aber nicht zwingend.

### 9.3 Streamer-Seite

- Zusätzlicher Button **„Jetzt clippen“** pro Streamer-Zeile.
- Ruft `POST /api/trigger` mit `{login, reason: "Manuell über UI"}` auf.
- Bei Erfolg: Toast „Clip wird erzeugt“; bei 409: Fehlermeldung anzeigen.
- Der Button umgeht Cooldown und Stundenlimit bewusst, damit er als
  Sicherheitsnetz funktioniert. Die erzeugte Aufnahme zählt aber als Recording
  und damit für das Stundenlimit der automatischen Trigger.
- Das ist ein Sicherheitsnetz während der Tuning-Phase und unabhängig von den
  neuen Signalen.

## 10. Datenmodell und Kompatibilität

- `SignalSample.components` bleibt JSON; neue Schlüssel werden einfach ergänzt.
- `SignalSample.detail` ist neu (nullable JSON). Die Spalte wird beim Start bei
  bestehenden SQLite-DBs per `ALTER TABLE signal_sample ADD COLUMN detail JSON`
  ergänzt. Gespeichert werden nur aggregierte Rohwerte, keine Chat-Texte.
- `signal_sample`-Zeilen aus der Zeit vor diesem Schnitt haben die neuen
  Schlüssel und `detail` nicht. UI und Report müssen fehlende Werte als 0
  behandeln.
- `setting`-Tabelle: neue Schlüssel werden beim Start durch `seed_defaults()`
  angelegt. Bestehende Werte werden nicht überschrieben.
- `Streamer`-Tabelle bleibt unverändert.
- `ChatMessage` bekommt zwei neue Felder mit Defaults, damit bestehende Tests und
  Aufrufe weiter funktionieren:
  - `user_id: str = ""`
  - `badges: str = ""`
- `parse_privmsg()` liest zusätzlich die Tags `user-id` und `badges`.
- `SessionSignals` bekommt die Chat-Konfiguration (Keyword-Liste, Copypasta-
  Parameter) beim Start der Session. Änderungen an diesen Feldern gelten – wie
  bei den übrigen Erkennungswerten – für die nächste Aufnahme.

## 11. Rollout

1. Code, Tests und UI implementieren.
2. Testsuite ausführen; bestehende Tests müssen grün bleiben.
3. Image neu bauen und `ai-twitch-clipper` neu starten; Forwarder/Watcher
   brauchen keine Änderung.
4. Für die **bestehende Installation** einmalig per `PUT /api/settings` setzen:
   - `threshold = 0.40`
   - `required_ticks = 1`
   - `max_clips_per_hour = 6`
   - `shadow_mode = true`
   - `streamer_command_triggers = true`
   - `weights` = **aktuell gespeicherte** Gewichte, ergänzt um die neuen
     Schlüssel mit 0 (nicht auf `DEFAULT_WEIGHTS` zurücksetzen). Aktuell:
     `chat_rate=0.53`, `emote_burst=0.51`, `clip_created=0.30`,
     `viewers=0.15`, `clip_burst=0.10`.
5. 24–48 Stunden beobachten:
   - Entstehen jetzt Clips? (Erwartung: ca. 2 pro 30 Minuten bei drei großen Streams.)
   - `[shadow]`-Logzeilen und `shadow_report.py` auswerten.
6. Wenn die neuen Signale plausibel sind: „Chat-Profil anwenden“ in der UI
   (oder äquivalentes `PUT`) und weitere 24 Stunden beobachten.
7. Bei zu vielen Clips: `threshold` erhöhen, `required_ticks` auf 2 setzen oder
   `max_clips_per_hour` senken.

## 12. Tests

### 12.1 Parser

- `parse_privmsg()` liest `user-id` und `badges`.
- Broadcaster-/Mod-Badge wird korrekt erkannt.
- Bestehende Emote-/Hype-Tests bleiben grün.

### 12.2 Signale

- `unique_chatters`: zwei Nachrichten desselben Users zählen nur einmal;
  verschiedene User zählen mehrfach.
- `hype_keywords`: deutsche/englische Keywords, Groß-/Kleinschreibung,
  Wortgrenzen; Emoji-Treffer.
- `copypasta`: 3 verschiedene User mit gleichem Text lösen aus; 3 Nachrichten
  desselben Users nicht; Ablauf nach der Haltezeit.
- `clip_commands`: `!clip`, „clip it“, „clip das“, „clippen“ treffen;
  normales „highlight“ nicht.
- `streamer_command`: Broadcaster/Mod + Clip-Command setzt `hit`; normaler User
  nicht.
- `copypasta` nutzt denselben `user_id`-oder-Anzeigename-Fallback wie
  `unique_chatters`; ohne `user-id` wird ein fehlendes Tag korrekt behandelt.

### 12.3 Detector

- `required_ticks=1` triggert beim ersten Tick über der Schwelle.
- `required_ticks=2` verhält sich wie bisher.
- `max_clips_per_hour` blockiert weitere Trigger und erlaubt sie nach Ablauf
  des Fensters wieder.
- `can_trigger()` berücksichtigt Cooldown, Puffer und Stundenlimit.
- Das Stundenlimit zählt Recordings aus der DB und bleibt nach einem
  Session-Neustart erhalten.
- Manuelle Trigger umgehen das Stundenlimit, werden aber als Recording gezählt.
- Shadow-Gewichte (0) verändern den Legacy-Score nicht.

### 12.4 Settings/UI

- `validate_updates()` akzeptiert und validiert die neuen Schlüssel.
- `seed_defaults()` legt bei bestehender Installation `required_ticks=2` und
  `max_clips_per_hour=60` an, ohne `threshold`/`weights` zu ändern.
- `SignalSample.detail` wird bei einer bestehenden SQLite-DB nachgezogen und
  vom Report gelesen.
- `hype_keywords` wird getrimmt und auf max. 500 Zeichen geprüft.
- `GET/PUT /api/settings` roundtrip.
- Settings-Seite rendert die neuen Felder und den Profil-Button.
- Streamer-Seite rendert den „Jetzt clippen“-Button.
- `shadow_report.py` läuft gegen eine temporäre SQLite-DB.

### 12.5 Manuell

- Fake-Chat-Nachrichten über Unit-Tests; kein Live-Chat nötig.
- Nach dem Deployment: `signal_sample` prüfen, ob die neuen Schlüssel auftauchen.
- `shadow_report.py --simulate-profile` gegen eine temporäre DB prüfen.
- Einen manuellen Trigger über den neuen Button auslösen und den Durchlauf bis
  `output/` prüfen.

## 13. Risiken und Gegenmaßnahmen

| Risiko | Gegenmaßnahme |
|---|---|
| Keyword-Liste passt nicht zur Szene/Sprache | Konfigurierbar; Startliste bewusst klein |
| Copypasta erkennt Spam/Beleidigungen | Mind. 3 verschiedene User, Haltezeit, moderates Gewicht |
| Unique Chatters durch IRC-Tags nicht verfügbar | Fallback auf Anzeigename; sonst Signal 0 |
| Einzel-Tick löst zu oft aus | Schwelle 0,40, Cooldown 180 s, max. 6 Clips/Stunde |
| Streamer/Mod-Kommando wird als normaler Chat erkannt | Nur Broadcaster/Mod-Badge + Clip-Regex |
| Shadow-Mode wird vergessen | UI-Hinweis, `[shadow]`-Logs, Report, Profil-Button |
| Bestehende Gewichte werden versehentlich verwässert | Shadow-Phase nutzt alte Gewichte; Profil setzt alle zehn Werte gemeinsam |
| Zu viele Clips belasten GPU/Disk | Stundenlimit, Cooldown, Disk-Guard, Watcher bleibt sequenziell |

## 14. Spätere Pakete

- **Paket A2:** Per-Streamer-Profile (eigene Schwelle, Cooldown, Gewichte).
- **Paket B:** Bits/Cheers, Subs/Geschenk-Subs, Raids über IRC; optionale
  Audio-Lautstärke/Schreien.
- **Paket C:** Twitch EventSub/Helix (Hype Train, Predictions, Channel Points),
  Video-/Facecam-Analyse, Sentiment-Modell.

## 15. Entscheidungen nach dem Builder-Check

1. **Stundenlimit:** globaler Wert, pro Streamer angewendet; per-Streamer-
   Konfiguration kommt später (A2).
2. **Streamer-/Mod-Trigger im Shadow-Mode:** nein; er wird erst mit dem
   Chat-Profil aktiviert.
3. **Hype-Keywords:** Startliste wie oben; szenenspezifische Ergänzungen erst
   nach dem Shadow-Report.
4. **Manueller Trigger:** umgeht Cooldown und Stundenlimit, zählt aber als
   Recording für das Limit der automatischen Trigger.
5. **Rohdaten:** `SignalSample.detail` wird eingeführt; keine Chat-Texte, nur
   aggregierte Rohwerte.
6. **Stundenlimit-Persistenz:** DB-basiert über die `recording`-Tabelle, nicht
   nur im Detector-Speicher.
7. **Copypasta-Identität:** derselbe `user_id`-oder-Anzeigename-Fallback wie
   `unique_chatters`.
8. **Bestehende Gewichte:** aktuelle DB-Gewichte bleiben erhalten und werden
   nur um neue Schlüssel mit 0 ergänzt.
