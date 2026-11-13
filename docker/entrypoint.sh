#!/usr/bin/env bash
# DocTheCode container entrypoint.
#
# Unless DATABASE_URL points to an external Postgres, this runs a private PostgreSQL inside the
# container: created on first start in $DOCTHECODE_DATA_DIR/postgres, reachable only through a Unix
# socket inside the container, started before DocTheCode and stopped cleanly after it.
set -euo pipefail

DATA="${DOCTHECODE_DATA_DIR:-/data}"
mkdir -p "$DATA"

as_pg() {  # Postgres refuses to run as root; use the postgres user when we are root.
  if [[ "$(id -u)" == "0" ]]; then runuser -u postgres -- "$@"; else "$@"; fi
}

stop_pg() { :; }

if [[ -z "${DATABASE_URL:-}" ]]; then
  PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -n1)}"
  if [[ ! -x "$PG_BIN/postgres" ]]; then
    echo "DocTheCode: no PostgreSQL found in the image and DATABASE_URL is not set." >&2
    exit 1
  fi
  export PATH="$PG_BIN:$PATH"
  PGDATA="$DATA/postgres"
  SOCK="${DOCTHECODE_PG_SOCKET_DIR:-/run/docthecode-postgres}"
  mkdir -p "$PGDATA" "$SOCK"
  if [[ "$(id -u)" == "0" ]]; then
    chown postgres:postgres "$PGDATA" "$SOCK"
  fi
  chmod 700 "$PGDATA"

  BIN_MAJOR="$("$PG_BIN/postgres" --version | sed -E 's/.* ([0-9]+)(\.[0-9]+)*.*/\1/')"
  if [[ -s "$PGDATA/PG_VERSION" && "$(cat "$PGDATA/PG_VERSION")" != "$BIN_MAJOR" ]]; then
    echo "DocTheCode: the database in $PGDATA was created by PostgreSQL $(cat "$PGDATA/PG_VERSION"), but this image" >&2
    echo "has PostgreSQL $BIN_MAJOR. Use an image with the matching version, or upgrade the data with pg_upgrade." >&2
    exit 1
  fi

  if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
    echo "DocTheCode: creating the built-in PostgreSQL $BIN_MAJOR database in $PGDATA"
    as_pg "$PG_BIN/initdb" -D "$PGDATA" -U docthecode --encoding=UTF8 --no-locale \
      --auth-local=trust --auth-host=reject >/dev/null
  fi

  echo "DocTheCode: starting the built-in PostgreSQL"
  as_pg "$PG_BIN/pg_ctl" -D "$PGDATA" -w -t 120 -l "$PGDATA/server.log" start \
    -o "-c listen_addresses='' -c unix_socket_directories='$SOCK' -c max_connections=40 -c shared_buffers=64MB" \
    >/dev/null || { echo "DocTheCode: PostgreSQL failed to start:" >&2; tail -n 30 "$PGDATA/server.log" >&2; exit 1; }

  if ! as_pg psql -h "$SOCK" -U docthecode -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'docthecode'" | grep -q 1; then
    as_pg createdb -h "$SOCK" -U docthecode docthecode
  fi

  export DATABASE_URL="postgresql://docthecode@/docthecode?host=$SOCK"
  export DOCTHECODE_EMBEDDED_DB=1
  stop_pg() {
    echo "DocTheCode: stopping the built-in PostgreSQL"
    as_pg "$PG_BIN/pg_ctl" -D "$PGDATA" -m fast -w -t 20 stop >/dev/null 2>&1 || true
  }
fi

# Run DocTheCode; forward stop signals to it, then stop Postgres once DocTheCode has saved its state.
"$@" &
APP=$!
trap 'kill -TERM "$APP" 2>/dev/null || true' TERM INT
set +e
wait "$APP"
CODE=$?
while kill -0 "$APP" 2>/dev/null; do
  wait "$APP"
  CODE=$?
done
stop_pg
exit "$CODE"
