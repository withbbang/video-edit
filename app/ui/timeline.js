/* 타임라인 보기 범위 계산 (순수 함수 — 화면 app.js 와 node 테스트가 함께 쓴다).
   보기 범위 view = {t0: 왼쪽 끝 원본 초, spc: 칸 하나의 초}. 칸 너비 cw(px), 줄 너비 w(px). */

// 영상 전체가 줄 너비에 꼭 맞는 보기 (마지막 칸은 잘릴 수 있다)
function fitView(d, w, cw) {
  return { t0: 0, spc: (d * cw) / Math.max(cw, w) };
}

// 칸 하나는 1프레임보다 작게, 전체보다 크게 하지 않고, 왼쪽 끝은 영상 안에서
function clampView(v, d, w, cw, fps) {
  const fit = fitView(d, w, cw).spc;
  const spc = Math.min(Math.max(v.spc, 1 / fps), fit);
  const t0 = Math.min(Math.max(0, v.t0), Math.max(0, d - (spc * w) / cw));
  return { t0, spc };
}

const tToX = (v, t, cw) => ((t - v.t0) / v.spc) * cw;
const xToT = (v, x, cw) => v.t0 + (x / cw) * v.spc;

// factor 배 확대 (1보다 작으면 축소). anchorT 는 화면에서 같은 자리에 남는다
function zoomAt(v, factor, anchorT, d, w, cw, fps) {
  const x = tToX(v, anchorT, cw);
  const spc = clampView({ t0: 0, spc: v.spc / factor }, d, w, cw, fps).spc;   // 한계에서 멈춘 배율로 자리를 잡는다
  return clampView({ t0: anchorT - (x / cw) * spc, spc }, d, w, cw, fps);
}

// t 가 보이면 그대로, 아니면 t 가 왼쪽에서 10% 자리에 오게 넘긴다
function followT(v, t, d, w, cw, fps) {
  const span = (v.spc * w) / cw;
  if (t >= v.t0 && t < v.t0 + span) return v;
  return clampView({ t0: t - 0.1 * span, spc: v.spc }, d, w, cw, fps);
}

// 재생 중 따라가기: 재생 위치가 가운데를 넘거나 보기 밖이면 가운데에 두고 보기가 같이 흐른다 (쪽 넘김 없이)
function playFollow(v, t, d, w, cw, fps) {
  const span = (v.spc * w) / cw;
  if (t >= v.t0 && t <= v.t0 + span / 2) return v;
  return clampView({ t0: t - span / 2, spc: v.spc }, d, w, cw, fps);
}

// 재생 중 칸 하나의 초: 줄이 초당 pxPerSec 보다 빨리 흐르지 않게 (1칸 = 1프레임이면 너무 빨라 알아볼 수 없다)
function playSpc(spc, cw, pxPerSec) {
  return Math.max(spc, cw / pxPerSec);
}

// 보이는 칸들: 칸 시작 시간 t (spc 격자), 화면 x
function cells(v, d, w, cw) {
  const out = [];
  const n0 = Math.floor(v.t0 / v.spc + 1e-9);
  for (let k = 0; ; k++) {
    const t = (n0 + k) * v.spc;
    const x = tToX(v, t, cw);
    if (x >= w || t >= d) break;
    out.push({ i: n0 + k, t, x });
  }
  return out;
}

// 시간 t 의 썸네일이 시트 몇 번의 몇째 칸인지 (get_thumbs 의 배치)
function sheetCell(t, meta) {
  const n = Math.floor(t / meta.step + 1e-9);
  if (n < 0 || n >= meta.count) return null;
  const k = n % meta.per_sheet;
  return { sheet: Math.floor(n / meta.per_sheet), col: k % meta.cols, row: Math.floor(k / meta.cols) };
}

// [a, b) 구간 파형 값(0~255)의 최대. 한 값보다 좁으면 그 자리 값
function waveMax(bytes, hop, a, b) {
  const i0 = Math.max(0, Math.floor(a / hop + 1e-9));
  const i1 = Math.min(bytes.length, Math.max(i0 + 1, Math.ceil(b / hop - 1e-9)));
  let m = 0;
  for (let i = i0; i < i1; i++) if (bytes[i] > m) m = bytes[i];
  return m;
}

// 확대 슬라이더 0~1000 (0 = 가장 축소 maxSpc, 1000 = 가장 확대 minSpc), 로그 눈금
function spcToSlider(spc, minSpc, maxSpc) {
  if (!(maxSpc > minSpc) || spc >= maxSpc) return 0;
  if (spc <= minSpc * (1 + 1e-9)) return 1000;   // 양 끝은 정확히 (부동소수 오차로 999.99… 가 되지 않게)
  return (1000 * Math.log(maxSpc / spc)) / Math.log(maxSpc / minSpc);
}
function sliderToSpc(s, minSpc, maxSpc) {
  if (!(maxSpc > minSpc)) return maxSpc;
  return maxSpc * Math.pow(minSpc / maxSpc, s / 1000);
}

// ---- 이미지 겹쳐 넣기 ----
// 이미지 o = {x, y, w, h?, aspect}: x·y(왼쪽 위)·w 는 영상 화면 비율, h 는 영상 높이 대비(없으면 그림 비율대로),
// aspect = 그림 너비/높이. box = 화면에 그려진 영상 자리(px)
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), Math.max(lo, hi));
const OV_MIN_W = 0.02;
const ovH = (o, box) => (o.h != null ? o.h : (o.w * box.w) / o.aspect / box.h);   // 높이 (영상 높이 대비)

function ovRect(o, box) {
  return { x: box.x + o.x * box.w, y: box.y + o.y * box.h, w: o.w * box.w, h: ovH(o, box) * box.h };
}
// px 만큼 옮긴 자리 (영상 밖으로 나가지 않게)
function ovMove(o, dx, dy, box) {
  return { x: clamp(o.x + dx / box.w, 0, 1 - o.w), y: clamp(o.y + dy / box.h, 0, 1 - ovH(o, box)) };
}
// 손잡이 끌기: edge 는 'n','s','e','w' 또는 모서리 'ne','nw','se','sw'. 끄는 변만 움직이고 반대쪽은 그대로.
// keep(Shift)이면 모서리는 지금 가로세로 비율을 유지 (가로 기준). 영상 안, 최소 OV_MIN_W. → {x, y, w, h} 비율
function ovResizeEdge(o, edge, dx, dy, box, keep) {
  const minW = OV_MIN_W * box.w, minH = OV_MIN_W * box.h;
  let l = o.x * box.w, t = o.y * box.h, r = l + o.w * box.w, b = t + ovH(o, box) * box.h;
  const ratio = (r - l) / (b - t);
  if (edge.includes('w')) l = clamp(l + dx, 0, r - minW);
  if (edge.includes('e')) r = clamp(r + dx, l + minW, box.w);
  if (edge.includes('n')) t = clamp(t + dy, 0, b - minH);
  if (edge.includes('s')) b = clamp(b + dy, t + minH, box.h);
  if (keep && edge.length === 2) {
    // 가로를 기준으로 세로를 맞추되, 세로가 영상 밖으로 나가면 가로를 줄인다
    const room = edge.includes('n') ? b : box.h - t;
    const w = Math.min(r - l, room * ratio);
    const h = w / ratio;
    if (edge.includes('w')) l = r - w; else r = l + w;
    if (edge.includes('n')) t = b - h; else b = t + h;
  }
  return { x: l / box.w, y: t / box.h, w: (r - l) / box.w, h: (b - t) / box.h };
}
// 타임라인 막대 끌기: kind 'move' | 'start' | 'end', dt 초. 프레임 격자, [0, d] 안, 최소 1프레임
function barDrag(kind, s, e, dt, d, fps) {
  const snap = (t) => Math.round(t * fps) / fps;
  const one = 1 / fps;
  if (kind === 'move') {
    const start = clamp(snap(s + dt), 0, d - (e - s));
    return { start, end: start + (e - s) };
  }
  if (kind === 'start') return { start: clamp(snap(s + dt), 0, e - one), end: e };
  return { start: s, end: clamp(snap(e + dt), s + one, d) };
}

// 이미지 줄 영역 높이 끌기: 시작 높이 h0(px)에서 dy(아래로 +)만큼 끌었을 때 새 높이.
// 한 줄(lane)과 내용 높이(content) 사이, 영상(시작 때 vid0)이 minVid 보다 작아지게 키우지는 않는다
function lanesDrag(h0, dy, content, vid0, minVid, lane) {
  const top = Math.min(content, Math.max(lane, h0 + Math.max(0, vid0 - minVid)));
  return clamp(h0 - dy, Math.min(lane, content), top);
}

const TL = { fitView, clampView, tToX, xToT, zoomAt, followT, playFollow, playSpc, cells, sheetCell, waveMax, spcToSlider, sliderToSpc,
  ovRect, ovMove, ovResizeEdge, barDrag, lanesDrag };
if (typeof window !== 'undefined') window.TL = TL;
if (typeof module !== 'undefined') module.exports = TL;
