#!/usr/bin/env bash
# Usage: validate-release-version.sh <tag> <pyproject.toml>
# Checks that a vX.Y.Z tag matches [project].version and prints the version.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <tag> <pyproject.toml>" >&2
  exit 2
fi
tag=$1
pyproject=$2

if [[ ! $tag =~ ^v([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
  echo "tag '$tag' is not of the form vX.Y.Z" >&2
  exit 1
fi
version=${BASH_REMATCH[1]}

project_version=$(python3 -c '
import sys, tomllib
with open(sys.argv[1], "rb") as stream:
    print(tomllib.load(stream)["project"]["version"])
' "$pyproject")
if [[ "$project_version" != "$version" ]]; then
  echo "tag $tag does not match the project version '$project_version' in $pyproject" >&2
  exit 1
fi
printf '%s\n' "$version"
