// ブラウザでの声の会話のページの判定（T-1503・T-1504、BV-06・BV-09・BV-10・BV-11）。ブラウザでは window.JarvisPage、Node では
// module.exports。DOM を使わない純粋な関数だけにして、Node の node --test で確かめる（tests/js/page.test.js）。
(function (root) {
  "use strict";

  const PLAYBACK_FAILS_TO_WARN = 3;   // 続けてこの数の文を再生できなければ注記で知らせる
  const SILENCE_LIMIT_MS = 15000;     // イベントも ping も来なければ切断として扱う（サーバーの ping は 5 秒ごと）

  // 異常の種類ごとの表示（message）と次の操作（action）。ends は会話を終える異常、place は出す場所
  // （status: 状態の欄、note: 注記の欄）
  const PROBLEMS = {
    mic_denied: { message: "マイクの使用が許可されていません。",
                  action: "ブラウザの設定でマイクを許可してから、もう一度「会話を始める」を押してください",
                  ends: true, place: "status" },
    mic_missing: { message: "マイクが見つかりません。",
                   action: "マイクをつないでから、もう一度「会話を始める」を押してください",
                   ends: true, place: "status" },
    mic_ended: { message: "マイクが外れました。会話を終えました。",
                 action: "マイクをつないでから、もう一度「会話を始める」を押してください",
                 ends: true, place: "status" },
    playback: { message: "読み上げの音を 1 文再生できませんでした。",
                action: "その文を飛ばして続けます",
                ends: false, place: "note" },
    playback_repeated: { message: "読み上げの音を続けて " + PLAYBACK_FAILS_TO_WARN + " 文再生できませんでした。",
                         action: "回答は文字で続けます。ブラウザ・Windows の出力先と音量を確かめてください",
                         ends: false, place: "note" },
    disconnected: { message: "サーバーとの接続が切れました。会話を終え、マイクを止めました。",
                    action: "もう一度「会話を始める」を押すと新しい会話になります（前の発言は送り直しません）",
                    ends: true, place: "status" },
    stt: { message: "認識できませんでした。",
           action: "もう一度話してください",
           ends: false, place: "status" },
    tts: { message: "読み上げの音を作れませんでした。",
           action: "回答は文字で続けます",
           ends: false, place: "note" },
    answer: { message: "答えを作れませんでした。",
              action: "もう一度話してください",
              ends: false, place: "status" },
  };

  function problem(kind) {
    const p = PROBLEMS[kind] || { message: "エラーがありました。", action: "もう一度話してください",
                                  ends: false, place: "status" };
    return { kind, text: p.message + p.action, message: p.message, action: p.action, ends: p.ends, place: p.place };
  }

  // getUserMedia などの例外 → 異常の種類（マイク以外の例外は null）
  function micErrorKind(e) {
    const name = e && e.name;
    if (name === "NotAllowedError" || name === "SecurityError") return "mic_denied";
    if (name === "NotFoundError" || name === "OverconstrainedError") return "mic_missing";
    return null;
  }

  // SSE のイベントの連番の確認。前に受けた seq 以下（重複・順番の違い）は捨てる
  function createSeqFilter() {
    let last = 0;
    return {
      accept(ev) {
        if (!ev || !Number.isInteger(ev.seq) || ev.seq <= last) return false;
        last = ev.seq;
        return true;
      },
      get last() { return last; },
    };
  }

  // 発言の番号の付いたイベントの扱い。turn は今の発言の番号、cut は割り込んだ発言の番号の最大（なければ -1）。
  // "none": 番号がない、"stale": 古い・割り込んだ発言（捨てる）、"new": 新しい発言、"current": 今の発言
  function turnDecision(ev, turn, cut) {
    if (!ev || !Number.isInteger(ev.turn)) return "none";
    if (ev.turn <= cut || ev.turn < turn) return "stale";
    return ev.turn > turn ? "new" : "current";
  }

  // 再生の失敗を続けて数える。fail() は、続けて n 回目になったときだけ true（そこで数え直す）
  function createFailCounter(n) {
    const limit = n || PLAYBACK_FAILS_TO_WARN;
    let count = 0;
    return {
      fail() {
        count += 1;
        if (count < limit) return false;
        count = 0;
        return true;
      },
      ok() { count = 0; },
      get count() { return count; },
    };
  }

  // 割り込みの時間（秒の値を受けてミリ秒にする）。onset: 判定の窓の中で最初にしきい値を超えたフレームの時刻、
  // detect: 検出した時刻、stopCall: stop() を呼んだ時刻（いずれも AudioContext の時計）。停止は stop() の時刻に
  // outputLatency + baseLatency を足した時刻
  function bargeTiming(t) {
    const stopped = t.stopCall + (t.outputLatency || 0) + (t.baseLatency || 0);
    const detectMs = Math.max(0, Math.round((t.detect - t.onset) * 1000));
    const stopMs = Math.max(0, Math.round((stopped - t.detect) * 1000));
    return { detectMs, stopMs, totalMs: detectMs + stopMs };
  }

  // playing: 再生中に割り込んだか（false なら考えている間。止める音はなく、停止は待ちの音を捨てた時刻）
  function bargeNote(timing, playing) {
    return "割り込み" + (playing === false ? "（考えている間）" : "") + ": 話し始めから停止まで約 " +
      timing.totalMs + " ms（検出 " + timing.detectMs + "・停止 " + timing.stopMs + "）";
  }

  // POST の応答の扱い。"ok": 成功、"expected": 想定内の失敗（会話を続ける）、"lost": 想定外（会話を終える）。
  // kind は送り先（played / interrupt / calibrate / approve / utterance）、error は応答の本文の error
  function postOutcome(kind, status, error) {
    if (status >= 200 && status < 300) return "ok";
    if (kind === "played") {
      if (status === 409) return "expected";                          // 古い発言の番号
      if (status === 404 && error === "not_found") return "expected"; // もう保持していない音
      return "lost";
    }
    if (kind === "interrupt" || kind === "calibrate" || kind === "approve") {
      return status === 409 ? "expected" : "lost";   // approve の 409: 期限切れ・取り消し・使用済み（何も実行しない）
    }
    if (kind === "utterance") return status === 400 || status === 413 ? "expected" : "lost";   // 受け付けない音声
    return "lost";
  }

  // 番号の分からないまま（/api/utterance の応答の前に）割り込んだ発言。分かるまで、新しい番号のイベントを保持する
  // （割り込んだ発言のイベントか、その次の発言のイベントかを決められないため）。
  // known(id) は {cut: 割り込んだ発言か, release: 保持をやめて渡し直すイベント}
  function createCutHold() {
    const ids = new Set();
    let held = [];
    return {
      cut(id) { ids.add(id); },
      get holding() { return ids.size > 0; },
      hold(ev) { held.push(ev); },
      known(id) {
        const cut = ids.delete(id);
        if (ids.size > 0) return { cut, release: [] };
        const release = held;
        held = [];
        return { cut, release };
      },
      clear() { ids.clear(); held = []; },
    };
  }

  // 画面での承認（T-1504）。approval のイベントを出すか: 今の発言の番号で、割り込んだ発言でなく、形が揃っていること
  function approvalShown(ev, turn, cut) {
    return !!ev && Number.isInteger(ev.turn) && ev.turn === turn && ev.turn > cut &&
      typeof ev.id === "string" && ev.id !== "" && typeof ev.text === "string";
  }

  // approval_done が要求 a（{turn, id}。出している要求か、割り込みで消した要求）の結果か
  function approvalMatches(ev, a) {
    return !!ev && !!a && ev.id === a.id && ev.turn === a.turn;
  }

  // 残りの秒数（切り上げ、0 未満にしない）。startedMs は approval を受けた時刻
  function secondsLeft(expiresIn, startedMs, nowMs) {
    return Math.max(0, Math.ceil(expiresIn - (nowMs - startedMs) / 1000));
  }

  const APPROVAL_RESULTS = { run: "実行しました", declined: "実行しませんでした",
                             expired: "期限が切れたので実行しませんでした",
                             cancelled: "取り消したので実行しませんでした",
                             later: "あとで（保存待ち）にしました" };

  function approvalResultText(result) {
    return APPROVAL_RESULTS[result] || "実行しませんでした";
  }

  const api = { PLAYBACK_FAILS_TO_WARN, SILENCE_LIMIT_MS, PROBLEMS, problem, micErrorKind, createSeqFilter,
                turnDecision, createFailCounter, bargeTiming, bargeNote, postOutcome, createCutHold,
                approvalShown, approvalMatches, secondsLeft, approvalResultText };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.JarvisPage = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
