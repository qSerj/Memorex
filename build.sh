#!/usr/bin/env bash
# Собирает memory-setup.zip для установки скилла через интерфейс Claude Code.
# Внутри архива один каталог memory-setup/ с SKILL.md в корне: такой формат ждёт установщик скиллов.
set -euo pipefail

cd "$(dirname "$0")"
VERSION=$(tr -d '[:space:]' < VERSION)
OUT="memory-setup.zip"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/memory-setup"
cp SKILL.md VERSION "$STAGE/memory-setup/"
cp -r references migrations "$STAGE/memory-setup/"

rm -f "$OUT"
( cd "$STAGE" && zip -qr "$OLDPWD/$OUT" memory-setup )

echo "$OUT собран, версия $VERSION"
unzip -l "$OUT" | tail -n +4 | head -20
