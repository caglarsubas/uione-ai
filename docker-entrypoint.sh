#!/bin/sh
# Pick up whatever the provisioner left behind, then start the app.
#
# This exists because compose resolves `env_file` on the host before anything
# runs, while the estate's tokens are created by a container a moment earlier.
# Sourcing here is the only point at which those two facts can be reconciled.
#
# **Already-set variables win.** The generated file is a set of defaults
# discovered at provision time; anything explicitly configured in compose is a
# decision someone made. It matters concretely: the provisioner records the
# mock vendors as 127.0.0.1:9102, which is correct when the app runs on the
# host and points the container at *itself* when it does not. Compose sets
# those to `mocks:9102`, and that must survive.
#
# A missing file is normal, not an error. The app runs against fixtures and the
# mocked vendors perfectly well, and refusing to start because an optional demo
# estate was not provisioned would be the wrong trade.
set -e

ESTATE_ENV="${ESTATE_ENV:-/run/estate/generated.env}"

if [ -f "$ESTATE_ENV" ]; then
    loaded=0
    kept=0
    while IFS= read -r line; do
        case "$line" in
            ''|\#*) continue ;;
        esac
        key=${line%%=*}
        value=${line#*=}
        [ "$key" = "$line" ] && continue

        # `eval` rather than a lookup table because sh has no indirect
        # expansion; the key comes from a file this image wrote itself.
        current=$(eval "printf '%s' \"\${$key-}\"")
        if [ -n "$current" ]; then
            kept=$((kept + 1))
            continue
        fi
        export "$key=$value"
        loaded=$((loaded + 1))
    done < "$ESTATE_ENV"
    echo "uione: estate settings loaded=$loaded, kept-from-environment=$kept"
else
    echo "uione: no provisioned estate at $ESTATE_ENV; using fixtures and mocks"
fi

# The share lives on a volume, so it cannot be created at image build time —
# the volume is mounted over whatever the image had there. Without this the
# file connector fails on every refresh cycle with "root not found", which
# reads as a broken connector rather than an empty directory.
if [ -n "${UIONE_FILES_ROOT:-}" ] && [ ! -d "$UIONE_FILES_ROOT" ]; then
    mkdir -p "$UIONE_FILES_ROOT"
    echo "uione: created file share root at $UIONE_FILES_ROOT"
fi

exec "$@"
