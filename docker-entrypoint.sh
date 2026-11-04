#!/bin/sh
# Sorgt dafür, dass ein frisch gemountetes Host-Verzeichnis für den
# non-root-Benutzer app beschreibbar ist, und gibt dann Rechte ab.
set -eu

if [ "$(id -u)" = "0" ]; then
    mkdir -p "${STORAGE_ROOT:-/data}" "${DATA_DIR:-/data}"
    chown app:app "${STORAGE_ROOT:-/data}" "${DATA_DIR:-/data}" 2>/dev/null || true
    exec gosu app "$@"
fi

exec "$@"
