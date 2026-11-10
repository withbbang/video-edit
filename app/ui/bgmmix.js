// BGM 미리보기 섞기: 재생기 <video>(결과 시간)에 맞춰 Web Audio 로 BGM 을 같이 들려준다.
// 순수 함수(bgmPlan·gainCurve)는 node 테스트용으로 module.exports, 클래스는 window 에만.
(function () {
  const TLm = (typeof module !== 'undefined' && typeof require === 'function') ? require('./timeline.js') : window.TL;
  const DB_MIN = -40;   // 파이프라인 BGM_FLOOR_DB 와 같다: 곡선 dB 가 이 이하면 무음

  // 재생 위치 t 에서 아직 끝나지 않은 BGM 마다 {id, wait, offset, dur}
  function bgmPlan(bgms, t, total) {
    const out = [];
    for (const b of bgms || []) {
      const wait = Math.max(0, b.at - t);
      const offset = b.in + Math.max(0, t - b.at);
      const dur = Math.min(b.out - offset, total - Math.max(t, b.at));
      if (dur > 1e-9) out.push({ id: b.id, wait, offset, dur });
    }
    return out;
  }

  // 곡 초 from 부터 step 간격 배율. 곡선 dB <= DB_MIN 이면 0 (gain 을 더하기 전에 판정)
  function gainCurve(points, gainDb, from, dur, step = 0.05) {
    const n = Math.max(2, Math.floor(dur / step + 1e-9) + 1);
    const a = new Float32Array(n);
    // 점은 한 번만 정렬 (dbAt 은 부를 때마다 정렬함)
    const pts = (points || []).map(([x, y]) => [+x, +y]).sort((p, q) => p[0] - q[0]);
    for (let i = 0; i < n; i++) {
      const db = TLm.dbAtSorted(pts, from + i * step);
      a[i] = db <= DB_MIN ? 0 : Math.pow(10, (db + (+gainDb || 0)) / 20);
    }
    return a;
  }

  const api = { bgmPlan, gainCurve };
  if (typeof module !== 'undefined') module.exports = api;
  if (typeof window === 'undefined') return;

  const DRIFT = 0.1;   // video 와 소리가 이만큼(초) 어긋나면 다시 예약
  class BgmMixer {
    constructor(video) {
      this.v = video; this.bgms = []; this.total = 0; this.ctx = null;
      this.master = null; this.bufs = new Map(); this.nodes = []; this.gen = 0;
      this.exp = null;   // {t0: 예약 때 video 위치, c0: ctx 시각, rate}
      this.dead = false; this.warned = false; this.failed = new Set();
      this.onEv = () => this.resched();
      this.onVol = () => this._applyVolume();
      this.evs = ['play', 'playing', 'pause', 'seeking', 'ratechange'];
      this.evs.forEach((e) => video.addEventListener(e, this.onEv));
      video.addEventListener('volumechange', this.onVol);
      // 브라우저는 사용자 입력 전엔 소리를 막는다: 첫 누름·키 입력에 풀고 다시 예약
      this.onUnlock = () => {
        document.removeEventListener('pointerdown', this.onUnlock, true);
        document.removeEventListener('keydown', this.onUnlock, true);
        if (this.dead) return;
        this._ctx();
        this.resched();
      };
      document.addEventListener('pointerdown', this.onUnlock, true);
      document.addEventListener('keydown', this.onUnlock, true);
      this.timer = setInterval(() => this.check(), 1000);
    }
    _ctx() {
      if (this.dead) return null;
      if (!this.ctx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return null;
        this.ctx = new AC();
        this.master = this.ctx.createGain();
        this.master.connect(this.ctx.destination);
        this.ctx.onstatechange = () => { if (this.ctx && this.ctx.state === 'running') this.resched(); };
      }
      return this.ctx;
    }
    _applyVolume() {
      if (!this.ctx || !this.master) return;
      this.master.gain.setTargetAtTime(this.v.muted ? 0 : this.v.volume, this.ctx.currentTime, 0.01);
    }
    set(bgms, total) {
      if (this.dead) return;
      this.bgms = bgms || []; this.total = total || 0;
      const urls = new Set(this.bgms.map((b) => b.url));
      for (const u of [...this.bufs.keys()]) if (!urls.has(u)) this.bufs.delete(u);
      for (const u of [...this.failed]) if (!urls.has(u)) this.failed.delete(u);
      this.resched();
    }
    _stop() {
      this.gen++;
      for (const n of this.nodes) {
        try { n.src.stop(); } catch (e) { /* 이미 멈춤 */ }
        try { n.src.disconnect(); n.g.disconnect(); } catch (e) { /* */ }
      }
      this.nodes = []; this.exp = null;
    }
    async _buf(url) {
      if (this.failed.has(url)) return null;
      if (!this.bufs.has(url)) {
        this.bufs.set(url, fetch(url)
          .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.arrayBuffer(); })
          .then((ab) => this.ctx.decodeAudioData(ab)));
      }
      try { return await this.bufs.get(url); } catch (e) {
        this.bufs.delete(url); this.failed.add(url);   // 같은 주소를 계속 다시 받지 않음
        console.warn('BGM 불러오기 실패:', url, e);
        return null;
      }
    }
    async resched() {
      if (this.dead) return;
      this._stop();
      const v = this.v, gen = this.gen;
      if (v.paused || v.ended || !this.bgms.length || !this._ctx()) return;
      const ctx = this.ctx, rate = v.playbackRate || 1;
      if (ctx.state !== 'running') { try { await ctx.resume(); } catch (e) { /* */ } }
      if (this.dead || gen !== this.gen) return;
      if (ctx.state !== 'running') {   // 사용자 입력 전: 풀리면 onstatechange/onUnlock 이 다시 예약
        if (!this.warned) { this.warned = true; console.warn('BGM: 소리가 아직 막혀 있음 (화면을 한 번 누르면 켜짐)'); }
        return;
      }
      this._applyVolume();
      this.master.gain.value = v.muted ? 0 : v.volume;
      const t = v.currentTime;
      const plan = bgmPlan(this.bgms, t, this.total || v.duration || 0);
      const bufs = await Promise.all(plan.map((p) => this._buf(this.bgms.find((b) => b.id === p.id).url)));
      if (this.dead || gen !== this.gen) return;   // 기다리는 동안 다시 예약됨
      const now = ctx.currentTime + 0.02;
      const lag = this.v.currentTime - t;   // 디코딩 동안 흐른 시간
      plan.forEach((p, i) => {
        const buf = bufs[i], b = this.bgms.find((x) => x.id === p.id);
        if (!buf) return;
        let wait = p.wait / rate - lag / rate, offset = p.offset, dur = p.dur;
        if (wait < 0) { offset += -wait * rate; dur += wait * rate; wait = 0; }
        if (dur <= 0 || offset >= buf.duration) return;
        const src = ctx.createBufferSource(), g = ctx.createGain();
        src.buffer = buf; src.playbackRate.value = rate;
        const at = now + wait;
        const curve = gainCurve(b.points, b.gain, offset, dur, 0.05);
        g.gain.setValueCurveAtTime(curve, at, Math.max(0.05, (curve.length - 1) * 0.05 / rate));
        src.connect(g); g.connect(this.master);
        src.start(at, offset, dur);
        this.nodes.push({ src, g });
      });
      this.exp = { t0: this.v.currentTime, c0: ctx.currentTime, rate };
    }
    check() {
      if (this.dead || !this.exp || this.v.paused || !this.ctx) return;
      const want = this.exp.t0 + (this.ctx.currentTime - this.exp.c0) * this.exp.rate;
      if (Math.abs(want - this.v.currentTime) > DRIFT) this.resched();
    }
    dispose() {
      this.dead = true;
      clearInterval(this.timer);
      this.evs.forEach((e) => this.v.removeEventListener(e, this.onEv));
      this.v.removeEventListener('volumechange', this.onVol);
      document.removeEventListener('pointerdown', this.onUnlock, true);
      document.removeEventListener('keydown', this.onUnlock, true);
      this._stop();
      if (this.ctx) {
        this.ctx.onstatechange = null;
        try { this.ctx.close().catch(() => {}); } catch (e) { /* */ }
        this.ctx = null;
      }
    }
  }
  window.BgmMixer = BgmMixer;
})();
