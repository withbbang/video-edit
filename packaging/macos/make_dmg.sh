#!/usr/bin/env bash
# 사용법: bash packaging/macos/make_dmg.sh dist/youtube-edit.app dist/youtube-edit-mac-arm64.dmg
# .app 과 "Applications" 바로가기, LICENSE·THIRD_PARTY.txt(ffmpeg 라이선스·소스 위치)를 담은 압축 dmg 를 만든다.
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "사용법: $0 <앱.app> <출력.dmg>" >&2
  exit 2
fi
APP="${1%/}"
OUT="$2"
VOLNAME="${VOLNAME:-youtube-edit}"

if [ ! -d "$APP/Contents/MacOS" ]; then
  echo "오류: $APP 은 .app 번들이 아닙니다" >&2
  exit 1
fi

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/youtube-edit-dmg.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

# ditto 는 심볼릭 링크·확장 속성·서명을 그대로 복사한다
ditto "$APP" "$STAGE/$(basename "$APP")"
ln -s /Applications "$STAGE/Applications"
for f in LICENSE THIRD_PARTY.txt; do
  src="$APP/Contents/Resources/$f"
  [ -f "$src" ] || { echo "오류: $src 가 없습니다" >&2; exit 1; }
  cp "$src" "$STAGE/$f"
done

mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
hdiutil create -volname "$VOLNAME" -srcfolder "$STAGE" -fs HFS+ -format UDZO -imagekey zlib-level=9 -ov "$OUT"
hdiutil verify "$OUT" >/dev/null
echo "만듦: $OUT ($(du -h "$OUT" | cut -f1))"
