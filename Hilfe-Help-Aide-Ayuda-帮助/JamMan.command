#!/bin/bash
# Weiterleitung fuer alte Verknuepfungen.
cd "$(dirname "$0")" || exit 1
exec "../JamMan-macOS.command" "$@"
