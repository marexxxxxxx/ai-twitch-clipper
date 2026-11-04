"""CLI: Trigger manuell auslösen und optional auf das Ergebnis warten.

Beispiel (im Container):
    python -m app.tools.simulate_trigger --login shroud
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8000"


def _request(base: str, path: str, payload: dict | None = None, method: str = "GET") -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("detail") or body
        except ValueError:
            detail = body
        raise SystemExit(f"Fehler (HTTP {exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"API nicht erreichbar unter {base}: {exc.reason}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Löst manuell eine 5-Minuten-Aufnahme aus dem Ringpuffer aus.")
    parser.add_argument("--login", required=True, help="Twitch-Login des laufenden Streamers")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help=f"API-Basis-URL (Standard: {DEFAULT_BASE})")
    parser.add_argument("--reason", default="Manuell ausgelöst (simulate_trigger)", help="Grund im Sidecar")
    parser.add_argument("--wait", action="store_true", help="Warten, bis die Aufnahme fertig ist")
    parser.add_argument("--timeout", type=int, default=600, help="Maximale Wartezeit in Sekunden")
    parser.add_argument("--json", action="store_true", help="Nur JSON ausgeben")
    args = parser.parse_args(argv)

    result = _request(
        args.base_url,
        "/api/trigger",
        {"login": args.login.strip().lower(), "reason": args.reason},
        method="POST",
    )
    recording_id = result.get("recording_id")
    if not args.json:
        print(f"Trigger ausgelöst: Aufnahme #{recording_id}")
    if not args.wait:
        print(json.dumps(result, ensure_ascii=False))
        return 0

    deadline = time.monotonic() + args.timeout
    last_status = None
    while time.monotonic() < deadline:
        recording = _request(args.base_url, f"/api/recordings/{recording_id}")
        status = recording.get("status")
        if status != last_status and not args.json:
            print(f"Status: {status}")
            last_status = status
        if status in ("ready", "truncated", "failed"):
            if args.json:
                print(json.dumps(recording, ensure_ascii=False, indent=2))
            else:
                print(
                    f"Fertig: {recording.get('filename')} · {status} · "
                    f"{recording.get('duration_s')} s · Score {recording.get('score')}"
                )
                if recording.get("error"):
                    print(f"Fehler: {recording['error']}")
            return 0 if status in ("ready", "truncated") else 1
        time.sleep(3)

    print("Timeout beim Warten auf die Aufnahme.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
