#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -lt 2 ]; then
  echo "Usage: bash run-colab.sh project.json shot-id [shot-id ...]" >&2
  exit 2
fi
tool_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
cli="${VIDEO_COLAB_CLI:-$tool_dir/workbench/colab.sh}"
state="${COLAB_WORKBENCH_STATE:-$HOME/.local/share/colab-video-workbench}"
identity=()
if [ -n "${VIDEO_COLAB_IDENTITY:-}" ]; then
  identity=(--identity "$VIDEO_COLAB_IDENTITY")
elif [ -z "${VIDEO_COLAB_CLI:-}" ]; then
  identity=(--identity "$state/ssh_ed25519")
fi
session="video-$(date -u +%Y%m%d-%H%M%S)-$$"
pending="$(python3 "$tool_dir/studio.py" "$1" pending "${@:2}")"
if [ -z "$pending" ]; then
  echo "All selected takes already verified locally."
  exit 0
fi
cleanup() {
  status=$?
  trap - EXIT
  if ! "$cli" stop -s "$session"; then
    echo "Could not confirm release of $session. Check Colab sessions." >&2
    if [ "$status" -eq 0 ]; then status=1; fi
  fi
  exit "$status"
}
"$cli" new -s "$session" --gpu G4
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
python3 "$tool_dir/cloud.py" "$@" --session "$session" --cli "$cli" "${identity[@]}" \
  --timeout-minutes "${VIDEO_COLAB_TIMEOUT_MINUTES:-90}"
