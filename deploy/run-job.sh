#!/bin/sh
# Job wrapper for deploy/crontab (supercronic): lock, prefixed logs, status
# files and ntfy alerts. All output goes to stdout (= TrueNAS app logs).
#
#   run-job.sh <name> <stale-seconds> -- <command...>
#       Run <command> under `flock -n /tmp/<name>.lock` (skip silently if the
#       previous run is still going), prefix its output with [<name>]. Success
#       writes the epoch to $STATUS_DIR/<name>.ok; failure POSTs the last
#       output lines to $NTFY_URL. Exits with the command's exit code.
#   run-job.sh --check-stale
#       For every job that has not succeeded within its <stale-seconds>, POST
#       one alert per stale episode (marker: $STATUS_DIR/<name>.alerted,
#       cleared by the next success).
#   run-job.sh --healthy
#       Docker healthcheck: exit 1 if any job is stale.
#
# Grace: a job is never stale before the container has been up for its
# <stale-seconds> (covers the first run and restarts).
set -u

APP_NAME="${APP_NAME:-flathunter}"
STATUS_DIR="${STATUS_DIR:-/data/.status}"
TAIL_LINES=40

usage() {
    echo "usage: run-job.sh <name> <stale-seconds> -- <command...>" >&2
    echo "       run-job.sh --check-stale | --healthy" >&2
    exit 2
}

# ntfy <title> <body>: never fails, even if ntfy is unreachable.
ntfy() {
    [ -n "${NTFY_URL:-}" ] || return 0
    printf '%s\n' "$2" | curl -fsS -m 10 -H "Title: $1" -H "Tags: warning" \
        --data-binary @- "$NTFY_URL" >/dev/null || true
}

fmt_time() { date -d "@$1" '+%Y-%m-%d %H:%M %Z'; }

# Epoch at which PID 1 of this container started.
container_start() {
    up=$(cut -d' ' -f1 /proc/uptime)
    up=${up%.*}
    ticks=$(sed 's/.*) //' /proc/1/stat | cut -d' ' -f20)
    echo $(( $(date +%s) - up + ticks / $(getconf CLK_TCK) ))
}

# Print "<name> <last-ok-epoch or 0> <reference-epoch>" for every stale job.
stale_jobs() {
    now=$(date +%s)
    started=$(container_start)
    for maxf in "$STATUS_DIR"/*.max; do
        [ -f "$maxf" ] || continue
        name=$(basename "$maxf" .max)
        max=$(cat "$maxf")
        last=0
        if [ -f "$STATUS_DIR/$name.ok" ]; then
            last=$(cat "$STATUS_DIR/$name.ok")
        fi
        ref=${last:-0}
        if [ "$ref" -lt "$started" ]; then
            ref=$started
        fi
        if [ $(( now - ref )) -gt "$max" ]; then
            echo "$name ${last:-0} $ref"
        fi
    done
}

run_job() {
    name=$1
    max=$2
    shift 2
    if [ "${1:-}" = "--" ]; then
        shift
    fi
    [ $# -ge 1 ] || usage

    mkdir -p "$STATUS_DIR"
    echo "$max" >"$STATUS_DIR/$name.max"

    exec 9>"/tmp/$name.lock"
    flock -n 9 || exit 0

    out=$(mktemp)
    rc_file=$(mktemp)
    # fd 9 (the lock) is closed for the command so a stray child can't hold it.
    { "$@" 2>&1 9>&-; echo $? >"$rc_file"; } | tee "$out" |
        while IFS= read -r line || [ -n "$line" ]; do
            printf '[%s] %s\n' "$name" "$line"
        done
    rc=$(cat "$rc_file")
    rc=${rc:-1}

    if [ "$rc" -eq 0 ]; then
        date +%s >"$STATUS_DIR/$name.ok"
        rm -f "$STATUS_DIR/$name.alerted"
    else
        printf '[%s] failed (rc=%s)\n' "$name" "$rc"
        ntfy "$APP_NAME: $name failed (rc=$rc)" "$(tail -n "$TAIL_LINES" "$out")"
    fi
    rm -f "$out" "$rc_file"
    exit "$rc"
}

check_stale() {
    stale_jobs | while read -r name last ref; do
        [ -f "$STATUS_DIR/$name.alerted" ] && continue
        if [ "$last" -gt 0 ]; then
            since=$(fmt_time "$last")
        else
            since="$(fmt_time "$ref") (container start, no success recorded)"
        fi
        echo "[check-stale] $name has not succeeded since $since"
        ntfy "$APP_NAME: $name has not succeeded since $since" \
            "No successful '$name' run since $since (max age $(cat "$STATUS_DIR/$name.max")s). See TrueNAS > Apps > $APP_NAME > Logs."
        touch "$STATUS_DIR/$name.alerted"
    done
}

healthy() {
    stale=$(stale_jobs | cut -d' ' -f1 | tr '\n' ' ')
    if [ -n "$stale" ]; then
        echo "stale jobs: $stale"
        exit 1
    fi
    echo "all jobs fresh"
}

case "${1:-}" in
    --check-stale) check_stale ;;
    --healthy) healthy ;;
    -* | "") usage ;;
    *)
        [ $# -ge 3 ] || usage
        run_job "$@"
        ;;
esac
