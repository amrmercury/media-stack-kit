#!/usr/bin/env bash
# Build the tarball you hand to friends. Refuses to build if the audit finds any real secret.
set -euo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$KIT"
echo "Re-exporting settings from the live stack..."
python3 tools/export_from_live.py >/dev/null
echo "Auditing for secrets..."
python3 tools/audit_seed.py
OUT="${1:-$KIT/../media-stack-kit.tar.gz}"
tar --exclude='./tools' --exclude='./state' --exclude='./tests' --exclude='./.git' --exclude='__pycache__' \
    --transform 's,^\./,media-stack-kit/,' -czf "$OUT" .
echo "Built $OUT ($(du -h "$OUT" | cut -f1)). Friends: tar xzf, cd media-stack-kit, ./install.sh"
