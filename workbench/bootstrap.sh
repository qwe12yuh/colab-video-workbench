#!/usr/bin/env bash
set -euo pipefail
umask 077
export PATH="$HOME/.local/bin:$PATH"
root="$(cd -- "$(dirname -- "$0")" && pwd)"
state="${COLAB_WORKBENCH_STATE:-$HOME/.local/share/colab-video-workbench}"
profile="$state/gcloud"
key="$state/ssh_ed25519"
missing=()
for tool in ffmpeg ffprobe ssh-keygen; do
  command -v "$tool" >/dev/null || missing+=("$tool")
done
if [ "${#missing[@]}" -gt 0 ]; then
  case "$(uname -s)" in
    Darwin)
      if ! command -v brew >/dev/null; then
        echo "Install Homebrew (https://brew.sh), then run this setup again. Missing: ${missing[*]}" >&2
        exit 1
      fi
      if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then brew install ffmpeg; fi
      if ! command -v ssh-keygen >/dev/null; then brew install openssh; fi
      ;;
    Linux)
      sudo apt-get update
      sudo apt-get install -y ffmpeg openssh-client
      ;;
  esac
fi
if ! command -v uv >/dev/null; then
  echo "Install uv, then run this setup again: https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi
if ! command -v colab >/dev/null; then uv tool install google-colab-cli==0.8.0; fi
mkdir -p "$profile"
if [ ! -f "$key" ]; then ssh-keygen -q -t ed25519 -N "" -f "$key"; fi
if [ "${1:-}" != auth ]; then
  bash "$root/colab.sh" version
  exit 0
fi
if ! command -v gcloud >/dev/null; then
  echo "Install Google Cloud CLI in the terminal running the workbench: https://cloud.google.com/sdk/docs/install-sdk"
  exit 1
fi
if [ -s "$profile/application_default_credentials.json" ]; then
  exec bash "$root/colab.sh" usage
fi
echo "Continue in Google's consent page. The CLI requests Colab, email, and Cloud scopes required by gcloud."
CLOUDSDK_CONFIG="$profile" gcloud auth application-default login --no-launch-browser --disable-quota-project \
  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
bash "$root/colab.sh" usage
