// voice/web/static/vad.js の判定のテスト（T-1501・T-1503）。tests/test_voice_web.py から node --test で呼ぶ。
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

// ---------------------------------------------------------------- 再生中のしきい値（T-1503・BV-07）

const fromDb = db => Math.pow(10, db / 20);

test("再生中のしきい値は max(通常のしきい値, 回り込みの p95 × 余裕)", () => {
  assert.strictEqual(V.percentile([], 95), 0);
  assert.strictEqual(V.percentile([5, 1, 3, 2, 4], 50), 3);
  const values = Array.from({ length: 100 }, (_, i) => (i + 1) / 1000);   // 0.001〜0.1
  assert.strictEqual(V.percentile(values, 95), 0.095);                    // 最近傍順位法（上位 5% の手前）
  assert.ok(Math.abs(V.playThresholdFrom(values, 0.004, 5) - 0.475) < 1e-12);
  assert.strictEqual(V.playThresholdFrom([0.0001, 0.0002], 0.004, 5), 0.004);   // 回り込みが小さければ通常のしきい値
  assert.strictEqual(V.playThresholdFrom([], 0.004, 5), 0.004);           // 測れなかった（回り込み 0）
});

test("再生中と再生していないときでしきい値を切り替える", () => {
  const det = V.createDetector({ threshold: 0.01, playThreshold: 0.05 });
  assert.strictEqual(det.currentThreshold, 0.01);
  det.setPlaying(true);
  assert.strictEqual(det.currentThreshold, 0.05);
  assert.deepStrictEqual(run(det, rep(0.03, 40)), []);         // 再生中は 0.03 では始めない
  det.setPlaying(false);
  const ev = run(det, rep(0.03, 12));                          // 再生していなければ同じ音で始める
  assert.strictEqual(ev.length, 1);
  assert.strictEqual(ev[0].type, "start");
  // 再生中のしきい値が通常より小さくなることはない
  const det2 = V.createDetector({ threshold: 0.01 });
  det2.setPlayThreshold(0.001);
  det2.setPlaying(true);
  assert.strictEqual(det2.currentThreshold, 0.01);
  assert.strictEqual(det2.playThreshold, 0.01);
});

test("試作の値: 回り込みでは検出せず、話し声では検出する（直近 0.16 秒のうち 0.12 秒）", () => {
  // 背景 -60 dBFS（通常のしきい値 = 背景 × 4）、エコー除去の後の回り込み -50 dBFS（ときどき -40 dBFS まで）、
  // 話し声 -12 dBFS
  const background = rep(fromDb(-60), 100);
  const threshold = V.thresholdFrom(background, 4, 0.002);
  const echo = [];
  for (let i = 0; i < 300; i++) echo.push(i % 25 === 0 ? fromDb(-40) : fromDb(-50) * (0.8 + 0.2 * ((i * 7) % 10) / 10));
  const playTh = V.playThresholdFrom(echo, threshold, 5);
  assert.ok(V.dbfs(playTh) > -40 && V.dbfs(playTh) < -30, String(V.dbfs(playTh)));

  const det = V.createDetector({ threshold, playThreshold: playTh });
  det.setPlaying(true);
  const playback = [];                                         // 再生中の回り込み（測ったときより 6 dB 大きい山も含む）
  for (let i = 0; i < 600; i++) playback.push(i % 50 < 15 ? fromDb(-44) : fromDb(-50));
  assert.deepStrictEqual(run(det, playback), []);
  // 同じ回り込みを通常のしきい値で判定すると誤検出する（再生中だけ上げる意味）
  const plain = V.createDetector({ threshold });
  assert.ok(run(plain, playback).some(e => e.type === "start"));

  // 再生中に話しかけた: 12 フレーム（0.12 秒）で検出する。話し始めは窓の最初に超えたフレーム
  const talk = [...rep(fromDb(-50), 20), ...rep(fromDb(-12), 30)];
  const evs = run(det, talk);
  assert.strictEqual(evs[0].type, "start");
  assert.strictEqual(evs[0].at, 20 + 12 - 1);
  assert.strictEqual(evs[0].onsetFrames, 11);
});

test("話し始めの位置（onsetFrames）は窓の中で最初に超えたフレームから", () => {
  const det = V.createDetector({ threshold: T });
  const levels = [0, 0.1, 0, ...rep(0.1, 11)];                  // 14 フレーム中 12 が超えた
  const evs = run(det, levels);
  assert.strictEqual(evs.length, 1);
  assert.strictEqual(evs[0].onsetFrames, levels.length - 1 - 1);
});

test("dBFS", () => {
  assert.strictEqual(V.dbfs(1), 0);
  assert.ok(Math.abs(V.dbfs(0.001) + 60) < 1e-9);
  assert.strictEqual(V.dbfs(0), -Infinity);
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
