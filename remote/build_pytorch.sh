#!/usr/bin/env bash
# First-run build for Teraferma's saveable Ubuntu 22.04 PyTorch image.
set -Eeuo pipefail

ROOT=/workspace/qwen-panel
TAG=b12431
mkdir -p "$ROOT/runtime"

if [ -x "$ROOT/runtime/llama-server" ]; then
  echo 'Сохранённый llama-server уже готов'
  exit 0
fi

echo BUILDING > "$ROOT/build_status"
trap 'echo FAILED > "$ROOT/build_status"' ERR
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq build-essential curl ca-certificates pkg-config

if ! command -v nvcc >/dev/null 2>&1; then
  curl -fLsS https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb -o /tmp/cuda-keyring.deb
  dpkg -i /tmp/cuda-keyring.deb
  apt-get update -qq
  apt-get install -y -qq cuda-nvcc-12-8 cuda-cudart-dev-12-8 libcublas-dev-12-8
fi
export PATH="/usr/local/cuda-12.8/bin:$PATH"

curl -fL --retry 5 --retry-delay 5 \
  "https://github.com/ggml-org/llama.cpp/archive/refs/tags/$TAG.tar.gz" \
  -o "$ROOT/llama-src.tar.gz"
mkdir -p "$ROOT/llama.cpp"
tar -xzf "$ROOT/llama-src.tar.gz" -C "$ROOT/llama.cpp" --strip-components=1

cmake -S "$ROOT/llama.cpp" -B "$ROOT/llama.cpp/build" \
  -DGGML_CUDA=ON -DGGML_NATIVE=OFF \
  -DCMAKE_CUDA_ARCHITECTURES=86 \
  -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build "$ROOT/llama.cpp/build" --target llama-server -j 16

cp -P "$ROOT/llama.cpp/build/bin/llama-server" "$ROOT/runtime/"
find "$ROOT/llama.cpp/build/bin" -maxdepth 1 -name '*.so*' -exec cp -P {} "$ROOT/runtime/" \;
# Keep the runtime self-contained: a fresh machine has no build-essential.
gomp=$(ldd "$ROOT/runtime/llama-server" | awk '/libgomp\.so/ {print $3; exit}')
[ -n "$gomp" ] && [ -f "$gomp" ] && cp -L "$gomp" "$ROOT/runtime/libgomp.so.1"
chmod 700 "$ROOT/runtime/llama-server"
ldconfig -n "$ROOT/runtime" 2>/dev/null || true
LD_LIBRARY_PATH="$ROOT/runtime:${LD_LIBRARY_PATH:-}" "$ROOT/runtime/llama-server" --version

rm -rf "$ROOT/llama.cpp" "$ROOT/llama-src.tar.gz"
echo READY > "$ROOT/build_status"
echo 'llama-server собран и сохранён в /workspace'
