#!/usr/bin/env bash
# Start the saved GGUF on Teraferma's PyTorch image after its async restore.
set -Eeuo pipefail

ROOT=/workspace/qwen-panel
STATUS="$ROOT/status"
MODEL="$ROOT/Huihui-Qwen3.8-27B-abliterated-Q6_K_L.gguf"
MODEL_SHA=20b1214a23f0f5f401b6117cbd63336cfff6c07be4df93bd6e374d8fd8d12938
MODEL_SIZE=24945124640
MODEL_URL=https://huggingface.co/huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF/resolve/main/Huihui-Qwen3.8-27B-abliterated-Q6_K_L.gguf

mkdir -p "$ROOT"
trap 'echo FAILED > "$STATUS"' ERR
fail() { echo "$1" >&2; echo FAILED > "$STATUS"; exit 1; }

valid_model() {
  [ -f "$MODEL" ] && [ "$(stat -c %s "$MODEL")" -eq "$MODEL_SIZE" ] \
    && [ -f "$ROOT/model.sha256" ] \
    && [ "$(cat "$ROOT/model.sha256")" = "$MODEL_SHA" ]
}

# Teraferma exposes SSH before rclone has finished restoring /workspace.
# A restored GGUF first appears as a *.partial file. Do not start a second
# 25 GB download while that transfer is in progress.
echo RESTORING > "$STATUS"
absent_checks=0
for _ in $(seq 1 240); do
  if valid_model; then break; fi
  if [ -f "$ROOT/model.sha256" ] || compgen -G "$MODEL.*.partial" >/dev/null; then
    sleep 5
  else
    if [ "${QWEN_EXPECT_SAVED:-0}" = 1 ]; then
      sleep 5
    else
      absent_checks=$((absent_checks + 1))
      [ "$absent_checks" -ge 12 ] && break
      sleep 5
    fi
  fi
done

if ! valid_model; then
  if [ "${QWEN_EXPECT_SAVED:-0}" = 1 ] || [ -f "$ROOT/model.sha256" ] || compgen -G "$MODEL.*.partial" >/dev/null; then
    fail 'Teraferma не закончила восстановление сохранённой модели за 20 минут'
  fi
  echo DOWNLOADING > "$STATUS"
  echo 'На этом аккаунте нет сохранённой модели; первая загрузка Q6_K_L'
  curl -fL --retry 8 --retry-delay 10 --connect-timeout 30 \
    --speed-limit 1024 --speed-time 120 --continue-at - \
    --output "$MODEL" "$MODEL_URL"
  echo "$MODEL_SHA  $MODEL" | sha256sum -c \
    || fail 'Контрольная сумма скачанной модели не совпала'
  printf '%s\n' "$MODEL_SHA" > "$ROOT/model.sha256"
fi

echo BUILDING > "$STATUS"
# Restoring /workspace can strip executable bits and shared library links.
if [ -f "$ROOT/runtime/llama-server" ]; then
  chmod 700 "$ROOT/runtime/llama-server"
fi
if [ -d "$ROOT/runtime" ]; then
  find "$ROOT/runtime" -maxdepth 1 -type f -name '*.so*' -exec chmod 644 {} +
  ldconfig -n "$ROOT/runtime" 2>/dev/null || true
fi
if [ ! -f "$ROOT/runtime/llama-server" ]; then
  bash "$ROOT/build_pytorch.sh" || fail 'Не удалось подготовить CUDA-сборку llama.cpp'
fi

echo LOADING > "$STATUS"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
export LD_LIBRARY_PATH="$ROOT/runtime:/opt/conda/lib/python3.11/site-packages/nvidia/cublas/lib:/opt/conda/lib/python3.11/site-packages/nvidia/cuda_runtime/lib:${LD_LIBRARY_PATH:-}"
nohup "$ROOT/runtime/llama-server" \
  --model "$MODEL" --alias qwen38-27b \
  --host 127.0.0.1 --port 17860 \
  --n-gpu-layers 999 --ctx-size 32768 --parallel 1 \
  --flash-attn on \
  > "$ROOT/runtime.log" 2>&1 < /dev/null &
echo $! > "$ROOT/llama.pid"

for _ in $(seq 1 180); do
  if curl -fsS --max-time 3 http://127.0.0.1:17860/health 2>/dev/null | grep -q '"ok"'; then
    echo READY > "$STATUS"
    echo 'Модель готова'
    exit 0
  fi
  if ! kill -0 "$(cat "$ROOT/llama.pid")" 2>/dev/null; then
    tail -n 40 "$ROOT/runtime.log" >&2
    fail 'llama-server завершился при загрузке модели'
  fi
  sleep 2
done
fail 'Модель не стала готова за 6 минут'
