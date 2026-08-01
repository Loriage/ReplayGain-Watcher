#!/bin/sh
set -eu

puid="${PUID:-1000}"
pgid="${PGID:-1000}"

validate_id() {
    name="$1"
    value="$2"

    case "$value" in
        ''|*[!0-9]*)
            echo "$name must be a positive integer" >&2
            exit 2
            ;;
    esac

    if [ "$value" -eq 0 ]; then
        echo "$name must not be 0; the application must not run as root" >&2
        exit 2
    fi
}

validate_id PUID "$puid"
validate_id PGID "$pgid"

exec /usr/bin/setpriv \
    --reuid "$puid" \
    --regid "$pgid" \
    --clear-groups \
    --no-new-privs \
    --inh-caps=-all \
    --ambient-caps=-all \
    --bounding-set=-all \
    -- "$@"
