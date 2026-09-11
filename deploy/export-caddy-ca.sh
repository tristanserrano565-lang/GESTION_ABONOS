#!/bin/sh
set -eu

source_path=/data/caddy/pki/authorities/local/root.crt
attempt=0
while [ ! -s "$source_path" ] && [ "$attempt" -lt 60 ]; do
    attempt=$((attempt + 1))
    sleep 1
done
if [ ! -s "$source_path" ]; then
    echo "Caddy no ha generado su certificado raiz interno." >&2
    exit 1
fi

cp "$source_path" /ca/root.crt
chmod 0444 /ca/root.crt
