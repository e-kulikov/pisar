#!/usr/bin/env bash
# Usage: package-release.sh <version> <zipapp> <output-dir>
# Builds pisar_<version>_any.tar.gz deterministically and prints its path.
# The zipapp is architecture independent; it needs Python >= 3.11 on the host.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <version> <zipapp> <output-dir>" >&2
  exit 2
fi
version=$1
zipapp=$2
output=$3

reported=$("$zipapp" --version)
if [[ "$reported" != "pisar $version" ]]; then
  echo "executable reports '$reported', expected 'pisar $version'" >&2
  exit 1
fi

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
name="pisar_${version}_any"
temp=$(mktemp -d)
trap 'rm -rf -- "$temp"' EXIT
stage="$temp/$name"

install -d "$stage"
install -m 0755 "$zipapp" "$stage/pisar"
install -m 0644 "$repo/README.md" "$stage/README.md"

# A stable timestamp keeps the archive reproducible for a given commit.
: "${SOURCE_DATE_EPOCH:=$(git -C "$repo" log -1 --format=%ct)}"
mkdir -p "$output"
archive="$output/$name.tar.gz"
tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --owner=0 --group=0 --numeric-owner \
  --mode='go-w' --format=posix \
  --pax-option='exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime' \
  -C "$temp" -cf - "$name" | gzip -n -9 >"$archive"
printf '%s\n' "$archive"
