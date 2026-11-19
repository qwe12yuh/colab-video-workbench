#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf -- "$scratch"' EXIT
export VIDEO_TEST_LOG="$scratch/commands"
cat >"$scratch/cli" <<'CLI'
#!/usr/bin/env bash
printf '%s\n' "$1" >>"$VIDEO_TEST_LOG"
case "$1" in
  new|stop) exit 0 ;;
  ssh) echo "NVIDIA RTX PRO 6000 Blackwell Server Edition, 97887" ;;
  upload) exit 42 ;;
  *) exit 1 ;;
esac
CLI
chmod +x "$scratch/cli"
export VIDEO_COLAB_CLI="$scratch/cli"
mkdir "$scratch/project"
cp -R "$root/examples/gpu-smoke/project.json" "$root/examples/gpu-smoke/assets" "$scratch/project/"
if bash "$root/run-colab.sh" "$scratch/project/project.json" s01; then
  printf '%s\n' 'Expected the mocked upload to fail.' >&2
  exit 1
fi
[[ "$(cat "$VIDEO_TEST_LOG")" == "$(printf '%s\n' new ssh upload stop)" ]]
printf '%s\n' 'Verified upload failure still released the dedicated runtime.'
