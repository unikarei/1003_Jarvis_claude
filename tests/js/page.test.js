// voice/web/static/page.js（ページの判定）のテスト（T-1503）。tests/test_voice_web.py から node --test で呼ぶ。
"use strict";
const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");
const P = require(path.join(__dirname, "..", "..", "voice", "web", "static", "page.js"));

test("異常の種類ごとに、区別できる文言と次の操作を示す", () => {
  const kinds = ["mic_denied", "mic_missing", "mic_ended", "playback", "playback_repeated", "disconnected", "stt", "tts"];
  const texts = kinds.map(k => P.problem(k).text);
  assert.strictEqual(new Set(texts).size, kinds.length);         // すべて違う文言
  for (const k of kinds) {
    const p = P.problem(k);
    assert.ok(p.message && p.action, k);                         // 何が起きたかと、次の操作
    assert.ok(p.text.includes(p.action), k);
  }
  assert.match(P.problem("mic_denied").text, /ブラウザの設定でマイクを許可/);
  assert.match(P.problem("mic_denied").text, /もう一度「会話を始める」/);
  assert.match(P.problem("mic_missing").text, /マイクをつない/);
  assert.match(P.problem("mic_ended").text, /会話を終えました/);
  assert.match(P.problem("playback").text, /飛ばして続け/);
  assert.match(P.problem("playback_repeated").text, /続けて 3 文/);
  assert.match(P.problem("disconnected").text, /接続が切れました/);
  assert.match(P.problem("disconnected").text, /マイクを止めました/);
  assert.match(P.problem("disconnected").text, /新しい会話/);
  assert.match(P.problem("stt").text, /もう一度話してください/);
  assert.match(P.problem("tts").text, /文字で続け/);
  // 会話を終える異常（マイク・通信）と、続ける異常（再生・認識・音声エンジン）
  assert.deepStrictEqual(kinds.filter(k => P.problem(k).ends), ["mic_denied", "mic_missing", "mic_ended", "disconnected"]);
  assert.strictEqual(P.problem("playback").place, "note");
  assert.strictEqual(P.problem("tts").place, "note");
  assert.strictEqual(P.problem("stt").place, "status");
  assert.ok(P.problem("unknown-kind").text.length > 0);
});

test("マイクの例外を種類に分ける", () => {
  assert.strictEqual(P.micErrorKind({ name: "NotAllowedError" }), "mic_denied");
  assert.strictEqual(P.micErrorKind({ name: "SecurityError" }), "mic_denied");
  assert.strictEqual(P.micErrorKind({ name: "NotFoundError" }), "mic_missing");
  assert.strictEqual(P.micErrorKind({ name: "OverconstrainedError" }), "mic_missing");
  assert.strictEqual(P.micErrorKind(new Error("x")), null);
  assert.strictEqual(P.micErrorKind(null), null);
});

test("seq が前に受けたもの以下のイベントは捨てる（重複・順番の違い）", () => {
  const f = P.createSeqFilter();
  const seqs = [1, 2, 2, 4, 3, 5, 1, 6];
  assert.deepStrictEqual(seqs.map(seq => f.accept({ seq })), [true, true, false, true, false, true, false, true]);
  assert.strictEqual(f.last, 6);
  assert.strictEqual(f.accept({}), false);
  assert.strictEqual(f.accept({ seq: "7" }), false);
  assert.strictEqual(P.createSeqFilter().accept({ seq: 1 }), true);   // 新しい会話は 1 から
});

test("古い発言・割り込んだ発言のイベントは捨てる", () => {
  assert.strictEqual(P.turnDecision({ type: "ready" }, 0, -1), "none");
  assert.strictEqual(P.turnDecision({ turn: 0, calibration: true }, 0, -1), "current");
  assert.strictEqual(P.turnDecision({ turn: 3 }, 3, -1), "current");
  assert.strictEqual(P.turnDecision({ turn: 4 }, 3, -1), "new");
  assert.strictEqual(P.turnDecision({ turn: 2 }, 3, -1), "stale");
  // 発言 3 に割り込んだ: 発言 3 の audio・answer_chunk・turn_done は捨て、発言 5 は受ける
  assert.strictEqual(P.turnDecision({ turn: 3, type: "audio" }, 3, 3), "stale");
  assert.strictEqual(P.turnDecision({ turn: 3, type: "turn_done", interrupted: true }, 3, 3), "stale");
  assert.strictEqual(P.turnDecision({ turn: 5 }, 3, 3), "new");
});

test("再生の失敗を続けて 3 回数えたら知らせる（成功で数え直す）", () => {
  const c = P.createFailCounter();
  assert.deepStrictEqual([c.fail(), c.fail(), c.fail()], [false, false, true]);
  assert.strictEqual(c.count, 0);
  c.fail(); c.fail(); c.ok();
  assert.deepStrictEqual([c.fail(), c.fail(), c.fail()], [false, false, true]);
});

test("割り込みの時間: 話し始め → 検出 → 停止（出力の遅れを足す）", () => {
  const t = P.bargeTiming({ onset: 10.0, detect: 10.13, stopCall: 10.13, outputLatency: 0.042, baseLatency: 0.01 });
  assert.deepStrictEqual(t, { detectMs: 130, stopMs: 52, totalMs: 182 });
  assert.strictEqual(P.bargeNote(t), "割り込み: 話し始めから停止まで約 182 ms（検出 130・停止 52）");
  assert.deepStrictEqual(P.bargeTiming({ onset: 1, detect: 1.2, stopCall: 1.2 }), { detectMs: 200, stopMs: 0, totalMs: 200 });
});

test("切断とみなすまでの時間は ping の間隔（5 秒）より長い", () => {
  assert.strictEqual(P.SILENCE_LIMIT_MS, 15000);
  assert.strictEqual(P.PLAYBACK_FAILS_TO_WARN, 3);
});
