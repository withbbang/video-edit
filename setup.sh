#!/usr/bin/env bash
# youtube-edit 환경 구성 (macOS / Linux)
#   ./setup.sh                 ffmpeg 확인 + .venv 생성 + 파이썬 의존성 설치
#   ./setup.sh --download-model   위 작업 + Whisper 모델(large-v3-turbo, ~1.6GB) 미리 받기
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/3 ffmpeg"
if ! command -v ffmpeg >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    brew install ffmpeg
  else
    echo "ffmpeg 가 없습니다. macOS: brew install ffmpeg  /  Ubuntu: sudo apt install ffmpeg" >&2
    exit 1
  fi
fi
ffmpeg -version | head -1
if ! ffmpeg -hide_banner -filters 2>/dev/null | grep -q arnndn; then
  echo "경고: 이 ffmpeg 에는 arnndn 필터가 없어 노이즈 제거가 afftdn 으로 대체됩니다 (--denoise afftdn)"
fi

echo "== 2/3 Python 가상환경 (.venv)"
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv .venv --python 3.11 || uv venv .venv
  else
    python3 -m venv .venv
  fi
fi
if command -v uv >/dev/null 2>&1; then
  uv pip install --python .venv/bin/python -r requirements.txt
else
  .venv/bin/python -m pip install -q -U pip
  .venv/bin/python -m pip install -q -r requirements.txt
fi
.venv/bin/python -c "import faster_whisper, numpy; print('faster-whisper', faster_whisper.__version__, '/ numpy', numpy.__version__)"

echo "== 3/3 노이즈 모델"
if [ -f models/sh.rnnn ]; then
  echo "models/sh.rnnn OK"
else
  echo "models/sh.rnnn 이 없습니다. https://github.com/GregorR/rnnoise-models 의 somnolent-hogwash/sh.rnnn 을 models/ 에 넣으세요"
fi

if [ "${1:-}" = "--download-model" ]; then
  echo "== Whisper 모델 다운로드 (large-v3-turbo)"
  .venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8'); print('OK')"
fi

mkdir -p input output work
rm -f .claude/_probe 2>/dev/null || true
echo
echo "준비 완료. 영상을 input/ 에 넣고:"
echo "  .venv/bin/python scripts/pipeline.py input/영상.mp4"
echo "또는 Claude Code 에서:  /youtube-clean input/영상.mp4"
