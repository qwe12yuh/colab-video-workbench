#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
state="${COLAB_WORKBENCH_STATE:-$HOME/.local/share/colab-video-workbench}"
profile="$state/gcloud"
if ! command -v colab >/dev/null; then
  echo "请先运行 bash workbench/bootstrap.sh 安装 Colab CLI。" >&2
  exit 1
fi
if [ "${1:-}" = version ]; then exec colab version; fi
export CLOUDSDK_CONFIG="$profile"
export GOOGLE_APPLICATION_CREDENTIALS="$profile/application_default_credentials.json"
if [ ! -s "$GOOGLE_APPLICATION_CREDENTIALS" ]; then
  echo "请运行 bash workbench/bootstrap.sh auth 连接 Google Colab。" >&2
  exit 1
fi
exec colab --config "$profile/sessions.json" --auth adc "$@"
