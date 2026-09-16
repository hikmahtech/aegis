#!/bin/sh
# hledger — the books (spec 2026-09-05-maou-books-design.md §10).
#
# The version and its checksum live HERE and nowhere else. Core and the worker
# share ONE books checkout, so two images holding two different hledgers would
# run two different binaries against the same journal — which is exactly what
# a version bump applied to one Dockerfile and not the other used to produce.
# Both images COPY and run this script; bumping it moves both.
#
# A static release binary, pinned by version and checksum: an unpinned download
# would make the build's output depend on what GitHub served that day.
set -eu

HLEDGER_VERSION=1.52.3
HLEDGER_SHA256=d14a4fc2ac804b556f481b64e8c54efa380db1ac85b3723c9df7b1eeade74b3a

curl -fsSL -o /tmp/hledger.tgz \
  "https://github.com/simonmichael/hledger/releases/download/${HLEDGER_VERSION}/hledger-linux-x64.tar.gz"
echo "${HLEDGER_SHA256}  /tmp/hledger.tgz" | sha256sum -c -
tar -xzf /tmp/hledger.tgz -C /tmp hledger
install -m 0755 /tmp/hledger /usr/local/bin/hledger
rm -f /tmp/hledger.tgz /tmp/hledger
