#!/bin/sh
# Container entrypoint: make sure $HOME (on the /data mount) exists, then run
# the service command (hunter loop or supercronic).
mkdir -p "$HOME" 2>/dev/null || true
exec "$@"
