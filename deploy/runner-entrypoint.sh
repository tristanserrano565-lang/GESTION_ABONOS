#!/bin/sh
set -eu

token_file="${N8N_RUNNERS_AUTH_TOKEN_FILE:-}"
if [ -z "$token_file" ] || [ ! -s "$token_file" ]; then
    echo "Falta el secreto de autenticacion del task runner." >&2
    exit 1
fi
export N8N_RUNNERS_AUTH_TOKEN="$(tr -d '\r\n' < "$token_file")"
unset N8N_RUNNERS_AUTH_TOKEN_FILE

exec /usr/local/bin/task-runner-launcher "$@"
