#!/usr/bin/env bash
# Write a verified git bundle (all branches and tags) to a directory outside the repo.
# Usage: scripts/backup.sh [backup-dir]      (default: $PLR_BACKUP_DIR, else ~/plr-backups)
# Restore: git clone <bundle-file> patsquire-plr
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
backup_dir="${1:-${PLR_BACKUP_DIR:-$HOME/plr-backups}}"

case "$(realpath -m "$backup_dir")/" in
  "$repo_root"/*) echo "error: backup dir must be outside the repository ($repo_root)" >&2; exit 1 ;;
esac

if ! git -C "$repo_root" rev-parse --verify --quiet HEAD >/dev/null; then
  echo "error: nothing to back up yet (no commits)" >&2
  exit 1
fi

if [[ -n "$(git -C "$repo_root" status --porcelain)" ]]; then
  echo "warning: uncommitted changes are NOT included in the bundle; commit them first" >&2
fi

mkdir -p "$backup_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
head="$(git -C "$repo_root" rev-parse --short HEAD)"
bundle="$backup_dir/patsquire-plr-$stamp-$head.bundle"

git -C "$repo_root" bundle create "$bundle" --all
git -C "$repo_root" bundle verify "$bundle" >/dev/null
echo "backup written and verified: $bundle"
