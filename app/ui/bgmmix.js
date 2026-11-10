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
    for (let i = 0; i < n; i++) {
      const db = TLm.dbAt(points, from + i * step);
      a[i] = db <= DB_MIN ? 0 : Math.pow(10, (db + (+gainDb || 0)) / 20);
    }
    return a;
  }

  const api = { bgmPlan, gainCurve };
  if (typeof module !== 'undefined') module.exports = api;
  if (typeof window === 'undefined') return;

  class BgmMixer {
    constructor(video) {
      this.v = video; this.bgms = []; this.total = 0; this.ctx = null;
      this.master = null; this.bufs = new Map(); this.nodes = []; this.gen = 0;
      this.exp = null;   // {t0: 예약 때 video 위치, c0: ctx 시각}
      this.onEv = () => this.resched();
      this.evs = ['play', 'playing', 'pause', 'seeking', 'ratechange', 'volumechange'];
      this.evs.forEach((e) => video.addEventListener(e, this.onEv));
      this.timer = setInterval(() => this.check(), 1000);
    }
    _ctx() {
      if (!this.ctx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return null;
        this.ctx = new AC();
        this.master = this.ctx.createGain();
        this.master.connect(this.ctx.destination);
      }
      return this.ctx;
    }
    set(bgms, total) { this.bgms = bgms || []; this.total = total || 0; this.resched(); }
    _stop() {
      this.gen++;
      for (const n of this.nodes) {
        try { n.src.stop(); } catch (e) { /* 이미 멈춤 */ }
        try { n.src.disconnect(); n.g.disconnect(); } catch (e) { /* */ }
      }
      this.nodes = []; this.exp = null;
    }
    async _buf(url) {
      if (!this.bufs.has(url)) {
        this.bufs.set(url, fetch(url).then((r) => r.arrayBuffer()).then((ab) => this.ctx.decodeAudioData(ab)));
      }
      try { return await this.bufs.get(url); } catch (e) { this.bufs.delete(url); return null; }
    }
    async resched() {
      this._stop();
      const v = this.v, gen = this.gen;
      if (v.paused || v.ended || !this.bgms.length || !this._ctx()) return;
      const ctx = this.ctx, rate = v.playbackRate || 1;
      if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (e) { /* */ } }
      this.master.gain.value = v.muted ? 0 : v.volume;
      const t = v.currentTime;
      const plan = bgmPlan(this.bgms, t, this.total || v.duration || 0);
      const bufs = await Promise.all(plan.map((p) => this._buf(this.bgms.find((b) => b.id === p.id).url)));
      if (gen !== this.gen) return;   // 기다리는 동안 다시 예약됨
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
      if (!this.exp || this.v.paused || !this.ctx) return;
      const want = this.exp.t0 + (this.ctx.currentTime - this.exp.c0) * this.exp.rate;
      if (Math.abs(want - this.v.currentTime) > 0.05) this.resched();
    }
    dispose() {
      clearInterval(this.timer);
      this.evs.forEach((e) => this.v.removeEventListener(e, this.onEv));
      this._stop();
      if (this.ctx) { try { this.ctx.close(); } catch (e) { /* */ } this.ctx = null; }
    }
  }
  window.BgmMixer = BgmMixer;
})();
