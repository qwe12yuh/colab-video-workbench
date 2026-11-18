#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -f "$scratch/cli" "$scratch/commands"; rmdir "$scratch"' EXIT
export VIDEO_TEST_LOG="$scratch/commands"
cat >"$scratch/cli" <<'CLI'
#!/usr/bin/env bash
printf '%s\n' "$1" >>"$VIDEO_TEST_LOG"
case "$1" in new|stop) exit 0 ;; upload) exit 42 ;; *) exit 1 ;; esac
CLI
chmod +x "$scratch/cli"
export VIDEO_COLAB_CLI="$scratch/cli"
if bash "$root/run-colab.sh" "$root/examples/gpu-smoke/project.json" s01; then
  [[ ! -e "$VIDEO_TEST_LOG" ]]
  printf '%s\n' 'Verified existing take did not allocate a runtime.'
else
  [[ "$(cat "$VIDEO_TEST_LOG")" == "$(printf '%s\n' new upload stop)" ]]
  printf '%s\n' 'Verified upload failure still released the dedicated runtime.'
fi
