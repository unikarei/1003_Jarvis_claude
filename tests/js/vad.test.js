// voice/web/static/vad.js の判定のテスト（T-1501）。tests/test_voice_web.py から node --test で呼ぶ。
"use strict";
const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");
const V = require(path.join(__dirname, "..", "..", "voice", "web", "static", "vad.js"));

const T = 0.01;
function run(det, levels) {
  const out = [];
  levels.forEach((rms, i) => { const ev = det.push(rms); if (ev) out.push(Object.assign({ at: i }, ev)); });
  return out;
}
const rep = (v, n) => Array(n).fill(v);

test("しきい値は背景の中央値 × 倍率と下限の大きいほう", () => {
  assert.strictEqual(V.thresholdFrom([0.001, 0.002, 0.5], 4, 0.002), 0.008);   // 一瞬の物音に引きずられない
  assert.strictEqual(V.thresholdFrom([0.0001, 0.0001], 4, 0.002), 0.002);
});

test("16 フレームのうち 12 フレームが超えたら話し始め", () => {
  const det = V.createDetector({ threshold: T });
  assert.deepStrictEqual(run(det, rep(0.1, 11)), []);
  const ev = run(det, [0.1]);
  assert.strictEqual(ev.length, 1);
  assert.strictEqual(ev[0].type, "start");
  // 途中に小さい音が混じっても、窓の中で 12 フレームあれば始める
  const det2 = V.createDetector({ threshold: T });
  const levels = [];
  for (let i = 0; i < 16; i++) levels.push(i % 4 === 0 ? 0 : 0.1);   // 16 中 12
  const ev2 = run(det2, levels);
  assert.strictEqual(ev2.length, 1);
  assert.strictEqual(ev2[0].at, 15);
});

test("一瞬の物音（11 フレーム）では始めない", () => {
  const det = V.createDetector({ threshold: T });
  assert.deepStrictEqual(run(det, [...rep(0.1, 11), ...rep(0, 30), ...rep(0.1, 11)]), []);
});

test("しきい値の 0.7 倍を下回るフレームが無音の長さ続いたら区切る", () => {
  const det = V.createDetector({ threshold: T, silenceSeconds: 0.8 });
  const evs = run(det, [...rep(0.1, 50), ...rep(0.008, 30), ...rep(0.1, 10), ...rep(0, 80)]);
  // 0.008 は 0.7 倍（0.007）以上なので無音と数えない
  assert.deepStrictEqual(evs.map(e => e.type), ["start", "end"]);
  assert.strictEqual(evs[1].reason, "silence");
  assert.strictEqual(evs[1].at, 50 + 30 + 10 + 80 - 1);
  assert.strictEqual(evs[1].keep, true);
  assert.strictEqual(evs[1].speechSeconds, 0.9);
});

test("最長に達したら区切る（黙って捨てない）", () => {
  const det = V.createDetector({ threshold: T, maxSeconds: 20 });
  const evs = run(det, rep(0.1, 2100));
  assert.strictEqual(evs[1].type, "end");
  assert.strictEqual(evs[1].reason, "max");
  assert.strictEqual(evs[1].at, 2000 - 1);         // 話し始め（窓の最初のフレーム）から 20 秒
  assert.strictEqual(evs[1].keep, true);
  assert.strictEqual(evs[2].type, "start");        // 続けて話していれば次の発話になる
});

test("0.3 秒より短い発話は送らない", () => {
  const det = V.createDetector({ threshold: T });
  const evs = run(det, [...rep(0.1, 20), ...rep(0, 80)]);
  assert.deepStrictEqual(evs.map(e => e.type), ["start", "end"]);
  assert.strictEqual(evs[1].keep, false);
  const det2 = V.createDetector({ threshold: T });
  const evs2 = run(det2, [...rep(0.1, 30), ...rep(0, 80)]);
  assert.strictEqual(evs2[1].keep, true);
});

test("前 0.3 秒と判定の窓を含める", () => {
  assert.strictEqual(V.createDetector({ threshold: T, prerollSeconds: 0.3 }).backFrames, 16 + 30);
});

test("WAV は PCM 16 bit・モノラルで、レートはそのまま", () => {
  const buf = V.encodeWav(new Float32Array([0, 1, -1, 2]), 48000);
  const v = new DataView(buf);
  assert.strictEqual(buf.byteLength, 44 + 8);
  assert.strictEqual(v.getUint16(22, true), 1);
  assert.strictEqual(v.getUint32(24, true), 48000);
  assert.strictEqual(v.getUint16(34, true), 16);
  assert.deepStrictEqual([0, 1, 2, 3].map(i => v.getInt16(44 + i * 2, true)), [0, 32767, -32768, 32767]);
});
