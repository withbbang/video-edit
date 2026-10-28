/* 영상 위 자막과 글자 꾸밈 계산 (순수 함수 — 화면 app.js 와 node 테스트가 함께 쓴다).
   꾸밈 {font: 글꼴 family, size: 기본 크기 배율, color: "#rrggbb", bold: true}. 유튜브용 자막 새기기(pipeline.build_ass)와
   같은 값·비율을 써서 미리보기가 새긴 결과와 같게 보인다. */

// fonts/ 에 든 글꼴 (style.css 의 @font-face 와 같은 family 이름 — libass 도 이 이름으로 찾는다)
const FONTS = [
  { family: 'NanumGothic', label: '나눔고딕' },
  { family: 'NanumMyeongjo', label: '나눔명조' },
  { family: 'Black Han Sans', label: '검은고딕' },
  { family: 'Jua', label: '주아' },
  { family: 'Do Hyeon', label: '도현' },
];
// pipeline.py 의 BURN_* 와 같은 값
const BASE = { font: 'NanumGothic', size: 0.05, outline: 3 / 1080, marginV: 0.06, marginH: 0.05 };
const SIZES = [0.6, 0.8, 1, 1.25, 1.5, 2, 2.5];
const COLORS = [
  ['#ffffff', '흰색'], ['#ffd60a', '노랑'], ['#ff9f0a', '주황'], ['#ff453a', '빨강'],
  ['#ff6fb5', '분홍'], ['#64d2ff', '하늘'], ['#30d158', '초록'],
];

// object-fit: contain 으로 그린 영상 화면이 요소 안 어디에 있는지
function contentBox(elW, elH, vidW, vidH) {
  if (!(elW > 0 && elH > 0 && vidW > 0 && vidH > 0)) return null;
  const k = Math.min(elW / vidW, elH / vidH);
  const w = vidW * k, h = vidH * k;
  return { x: (elW - w) / 2, y: (elH - h) / 2, w, h };
}

// 자막 자리: 화면 아래 여백·양옆 여백, 기본 글자 크기와 외곽선 (모두 px, 짧은 변 기준)
function overlayLayout(c) {
  const short = Math.min(c.w, c.h);
  return {
    left: c.x + c.w * BASE.marginH,
    width: c.w * (1 - 2 * BASE.marginH),
    bottom: c.y + c.h - c.h * BASE.marginV,   // 영상 요소 위에서부터 자막 아랫변까지
    fs: short * BASE.size,
    outline: Math.max(0.5, short * BASE.outline),
  };
}

// 다음·앞 크기 단계 (사이 값이면 가까운 쪽 단계로)
function stepSize(cur, dir) {
  const v = Number(cur) || 1;
  if (dir > 0) return SIZES.find((s) => s > v + 1e-6) ?? SIZES[SIZES.length - 1];
  return [...SIZES].reverse().find((s) => s < v - 1e-6) ?? SIZES[0];
}

const same = (a, b) => (a || null) === (b || null) ||
  (!!a && !!b && a.font === b.font && a.size === b.size && a.color === b.color && !!a.bold === !!b.bold);

// 글자 목록 + 글자마다 꾸밈 → 같은 꾸밈이 이어지는 묶음 [{style, text, i}] (i = 첫 글자 번호)
function runs(chars, styles) {
  const out = [];
  chars.forEach((ch, i) => {
    const st = (styles && styles[i]) || null;
    const last = out[out.length - 1];
    if (last && same(last.style, st)) last.text += ch;
    else out.push({ style: st, text: ch, i });
  });
  return out;
}

// 고른 글자들의 꾸밈에서 항목마다 모두 같은 값 (다르면 undefined). 꾸밈이 없으면 기본값으로 친다
function common(list) {
  const val = (st, k) => {
    const v = st ? st[k] : undefined;
    if (k === 'font') return v || BASE.font;
    if (k === 'size') return v || 1;
    if (k === 'color') return v || '#ffffff';
    return !!v;
  };
  const out = {};
  for (const k of ['font', 'size', 'color', 'bold']) {
    const vals = list.map((st) => val(st, k));
    out[k] = vals.length && vals.every((v) => v === vals[0]) ? vals[0] : undefined;
  }
  return out;
}

// [[어절 키, 글자 번호], ...] → 어절마다 이어진 구간 [[키, 시작, 끝(빼고)], ...] (나온 순서 유지)
function toRanges(picks) {
  const out = [];
  for (const [key, i] of picks) {
    const last = out[out.length - 1];
    if (last && last[0] === key && last[2] === i) last[2] = i + 1;
    else out.push([key, i, i + 1]);
  }
  return out;
}

const CS = { FONTS, BASE, SIZES, COLORS, contentBox, overlayLayout, stepSize, runs, common, toRanges };
if (typeof window !== 'undefined') window.CS = CS;
if (typeof module !== 'undefined') module.exports = CS;
