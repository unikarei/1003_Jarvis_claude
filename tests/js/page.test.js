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
  // 考えている間の割り込みも、検出と停止の時間を出す
  assert.strictEqual(P.bargeNote(t, true), "割り込み: 話し始めから停止まで約 182 ms（検出 130・停止 52）");
  assert.strictEqual(P.bargeNote(t, false), "割り込み（考えている間）: 話し始めから停止まで約 182 ms（検出 130・停止 52）");
});

test("POST の応答: 想定内の失敗（古い番号など）と、会話を終える想定外の失敗を分ける", () => {
  for (const kind of ["played", "interrupt", "calibrate", "utterance"]) {
    assert.strictEqual(P.postOutcome(kind, 200), "ok", kind);
    assert.strictEqual(P.postOutcome(kind, 500), "lost", kind);
    assert.strictEqual(P.postOutcome(kind, 502), "lost", kind);
    assert.strictEqual(P.postOutcome(kind, 404, "no_session"), "lost", kind);
  }
  assert.strictEqual(P.postOutcome("utterance", 202), "ok");
  assert.strictEqual(P.postOutcome("played", 409, "stale_turn"), "expected");
  assert.strictEqual(P.postOutcome("played", 404, "not_found"), "expected");
  assert.strictEqual(P.postOutcome("played", 400, "bad_request"), "lost");
  assert.strictEqual(P.postOutcome("interrupt", 409, "stale_turn"), "expected");
  assert.strictEqual(P.postOutcome("interrupt", 400, "bad_request"), "lost");
  assert.strictEqual(P.postOutcome("calibrate", 409, "calibrated"), "expected");
  assert.strictEqual(P.postOutcome("utterance", 400, "bad_audio"), "expected");
  assert.strictEqual(P.postOutcome("utterance", 413, "too_large"), "expected");
  assert.strictEqual(P.postOutcome("utterance", 409, "ended"), "lost");
});

test("番号の分からないまま割り込んだ発言: 番号が分かるまで新しい番号のイベントを保持し、分かったら渡し直す", () => {
  const h = P.createCutHold();
  assert.strictEqual(h.holding, false);
  h.cut(1);                                       // 発言 1（応答の前）に割り込んだ
  assert.strictEqual(h.holding, true);
  h.hold({ turn: 2, type: "recognized" });        // 次の発言のイベントが応答より先に届いた
  h.hold({ turn: 2, type: "audio" });
  // 割り込んでいない発言の番号が分かっても、割り込んだ発言が残る間は保持を続ける
  assert.deepStrictEqual(h.known(9), { cut: false, release: [] });
  assert.strictEqual(h.holding, true);
  const r = h.known(1);
  assert.strictEqual(r.cut, true);
  assert.deepStrictEqual(r.release.map(e => e.type), ["recognized", "audio"]);   // 失わない
  assert.strictEqual(h.holding, false);
  // 2 つ割り込んだら、両方の番号が分かるまで渡さない
  h.cut(3); h.cut(4); h.hold({ turn: 5 });
  assert.deepStrictEqual(h.known(3), { cut: true, release: [] });
  assert.deepStrictEqual(h.known(4), { cut: true, release: [{ turn: 5 }] });
  h.cut(6); h.hold({ turn: 7 }); h.clear();
  assert.strictEqual(h.holding, false);
  assert.deepStrictEqual(h.known(6), { cut: false, release: [] });
});

test("画面での承認: 今の発言の要求だけを出し、結果と残りの秒数を示す（T-1504）", () => {
  const ev = { turn: 3, id: "abc", text: "「x」を実行します", expires_in: 60 };
  assert.strictEqual(P.approvalShown(ev, 3, -1), true);
  assert.strictEqual(P.approvalShown(ev, 4, -1), false);          // 今の発言の番号と違う
  assert.strictEqual(P.approvalShown(ev, 2, -1), false);
  assert.strictEqual(P.approvalShown(ev, 3, 3), false);           // 割り込んだ発言
  assert.strictEqual(P.approvalShown({ turn: 3, text: "x" }, 3, -1), false);   // ID がない
  assert.strictEqual(P.approvalShown({ turn: 3, id: "", text: "x" }, 3, -1), false);
  assert.strictEqual(P.approvalShown(null, 3, -1), false);
  // 結果（approval_done）は、出している要求か割り込みで消した要求と、ID・番号の両方が一致するときだけ出す
  assert.strictEqual(P.approvalMatches({ turn: 3, id: "abc" }, { turn: 3, id: "abc" }), true);
  assert.strictEqual(P.approvalMatches({ turn: 4, id: "abc" }, { turn: 3, id: "abc" }), false);
  assert.strictEqual(P.approvalMatches({ turn: 3, id: "abd" }, { turn: 3, id: "abc" }), false);
  assert.strictEqual(P.approvalMatches({ turn: 3, id: "abc" }, null), false);
  assert.strictEqual(P.secondsLeft(60, 1000, 1000), 60);
  assert.strictEqual(P.secondsLeft(60, 1000, 1500), 60);          // 切り上げ
  assert.strictEqual(P.secondsLeft(60, 1000, 60000), 1);
  assert.strictEqual(P.secondsLeft(60, 1000, 70000), 0);          // 0 未満にしない
  const texts = ["run", "declined", "expired", "cancelled"].map(P.approvalResultText);
  assert.strictEqual(new Set(texts).size, 4);
  assert.match(P.approvalResultText("run"), /実行しました/);
  for (const r of ["declined", "expired", "cancelled", "unknown"]) assert.match(P.approvalResultText(r), /実行しませんでした/);
  assert.strictEqual(P.postOutcome("approve", 200), "ok");
  assert.strictEqual(P.postOutcome("approve", 409, "stale_approval"), "expected");   // 期限切れ・使用済み
  assert.strictEqual(P.postOutcome("approve", 403), "lost");
  assert.strictEqual(P.postOutcome("approve", 400), "lost");
});

test("切断とみなすまでの時間は ping の間隔（5 秒）より長い", () => {
  assert.strictEqual(P.SILENCE_LIMIT_MS, 15000);
  assert.strictEqual(P.PLAYBACK_FAILS_TO_WARN, 3);
});

test("確認のカードを出している間は、発話の検出で割り込みも送信もしない（T-2503、ADR-0040）", () => {
  for (const type of ["start", "end"]) {
    for (const awaiting of [true, false]) {
      assert.strictEqual(P.micAction(type, awaiting, true), "ignore");   // 声・物音・読み上げの回り込みで取り消さない
    }
  }
  assert.strictEqual(P.micAction("start", true, false), "barge");        // カードがなければ今までどおり割り込む
  assert.strictEqual(P.micAction("start", false, false), "start");
  assert.strictEqual(P.micAction("end", false, false), "send");
  assert.strictEqual(P.micAction("end", true, false), "send");
});

test("「直して保存」で送る題名・本文は空白を除き、空・長すぎるなら送らない（T-2900）", () => {
  assert.deepStrictEqual(P.editPayload("  題名 ", " 本文\n"), { title: "題名", body: "本文" });
  assert.strictEqual(P.editPayload("", "本文"), null);
  assert.strictEqual(P.editPayload("題名", "   "), null);
  assert.strictEqual(P.editPayload("x".repeat(81), "本文"), null);
  assert.strictEqual(P.editPayload("題名", "y".repeat(2001)), null);
  assert.ok(P.editPayload("x".repeat(80), "y".repeat(2000)));
  assert.match(P.approvalResultText("edited"), /直した内容で保存/);
});

test("会話の記録は一番下を見ているときだけ新しい行で送る（T-2902）", () => {
  assert.strictEqual(P.atBottom(600, 400, 1000), true);     // ちょうど一番下
  assert.strictEqual(P.atBottom(570, 400, 1000), true);     // 少しの差（32 px まで）は一番下とみなす
  assert.strictEqual(P.atBottom(500, 400, 1000), false);    // 上を読んでいる間は送らない
  assert.strictEqual(P.atBottom(0, 400, 300), true);        // 記録が短く、スクロールしない
});
