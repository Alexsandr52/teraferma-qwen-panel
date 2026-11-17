#!/usr/bin/env bash
# Put the exact GGUF into Teraferma's saveable /workspace on a PyTorch instance.
set -Eeuo pipefail

ROOT=/workspace/qwen-panel
MODEL="$ROOT/Huihui-Qwen3.8-27B-abliterated-Q6_K_L.gguf"
MODEL_SHA=20b1214a23f0f5f401b6117cbd63336cfff6c07be4df93bd6e374d8fd8d12938
MODEL_SIZE=24945124640
MODEL_URL=https://huggingface.co/huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF/resolve/main/Huihui-Qwen3.8-27B-abliterated-Q6_K_L.gguf

mkdir -p "$ROOT"
trap 'echo FAILED > "$ROOT/storage_status"' ERR
echo DOWNLOADING > "$ROOT/storage_status"
if ! command -v curl >/dev/null; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates
fi
if ! echo "$MODEL_SHA  $MODEL" | sha256sum -c --status; then
  if [ -f "$MODEL" ] && [ "$(stat -c %s "$MODEL")" -ge "$MODEL_SIZE" ]; then
    rm -f "$MODEL"
  fi
  curl -fL --retry 8 --retry-delay 10 --connect-timeout 30 \
    --speed-limit 1024 --speed-time 120 --continue-at - \
    --output "$MODEL" "$MODEL_URL"
  echo "$MODEL_SHA  $MODEL" | sha256sum -c
fi
printf '%s\n' "$MODEL_SHA" > "$ROOT/model.sha256"
echo READY > "$ROOT/storage_status"
echo 'Проверенная модель готова к сохранению'
