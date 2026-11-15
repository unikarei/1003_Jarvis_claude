// ブラウザでの声の会話のページ（T-1501〜T-1503）。話すと自動で区切って送り、認識した文と回答を文字で出し、
// 回答を文ごとに受け取って最初の文から順に読み上げる。考えている間・話している間も聞き続け、Owner が話し始めたら
// サーバーの応答を待たずに読み上げを止めて新しい発言を聞く（割り込み、ADR-0028）。再生中だけしきい値を上げる
// （会話の開始時のあいさつで回り込みを測る、BV-07）。異常は種類ごとに表示し、切断したら会話を終える（BV-10・BV-11）。
// 音声・本文はブラウザの保存領域に置かない。確認の要るスキルは、画面の「実行する」「実行しない」で答える（T-1504、
// BV-09）。声の「はい」は承認にならない（話せば割り込みになり、サーバーが要求を取り消す）。
(function () {
  "use strict";
  const V = window.JarvisVad;
  const P = window.JarvisPage;
  const H = { "X-Jarvis-Voice": "1" };
  const $ = id => document.getElementById(id);
  const CALIBRATE_FRAMES = Math.round(1 / V.FRAME_SECONDS);   // 最初の 1 秒で背景の音を測る
  const AFTER_SPEECH_MS = 300;              // 最後の文の終了から 0.3 秒は、再生中のしきい値のまま（回り込みの残り）
  const CALIBRATION_PLAY_MS = 15000;        // あいさつの音を受けてから再生し終えるまでの上限

  let config = null, session = null, events = null, stream = null, audio = null, node = null;
  let detector = null, paused = false, background = [], ring = [], current = null, answerEl = null;
  // 読み上げ: turn は今の発言の番号、cut は割り込んだ発言の番号の最大。items は順番 → {id, buffer, state, ctrl}
  let turn = 0, cut = -1, items = new Map(), next = 0, source = null, lastEnd = -Infinity;
  let turnDone = false;                     // 今の発言の turn_done が届いた（その後の同じ発言の音は再生しない）
  let awaiting = false;                     // 送った発言の turn_done を待っている（考えている間・話している間）
  let sendId = 0, pendingSend = 0;          // 送った発言の通し番号。pendingSend は応答（番号）を待っている発言
  let pendingBase = 0;                      // pendingSend を送ったときの turn（SSE で番号が先に分かったかの判定）
  let cutHold = P.createCutHold();          // 番号の分からないまま割り込んだ発言と、その間に届いた新しい番号のイベント
  let playedQueue = Promise.resolve();      // /api/played を送る順番（start → end）を守る
  let seqFilter = P.createSeqFilter(), fails = P.createFailCounter();
  let calibration = null;                   // 回り込みの測定 {rms, playing, source, ctrl, timer}
  let watchdog = null;
  let approval = null;                      // 画面に出している承認の要求 {turn, id, timer}
  let withdrawn = null;                     // 割り込みで画面から消した承認の要求 {turn, id}（結果は approval_done で出す）

  function status(text, kind) {
    $("status").textContent = text;
    $("status").className = "status " + (kind || "");
  }

  function note(text) { $("note").textContent = text || ""; }

  // 文を表示し、https の URL を新しいタブで開くリンクにする（管理画面の URL など。innerHTML は使わない）
  function setLinked(el, text) {
    el.textContent = "";
    const re = /https:\/\/[^\s<>"'）」]+/g;
    let last = 0, m;
    while ((m = re.exec(text)) !== null) {
      el.append(text.slice(last, m.index));
      const a = document.createElement("a");
      a.href = m[0];
      a.textContent = m[0];
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      el.append(a);
      last = m.index + m[0].length;
    }
    el.append(text.slice(last));
  }

  function line(cls, label, text) {
    const div = document.createElement("div");
    div.className = "line " + cls;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = label;
    const body = document.createElement("span");
    body.className = "text";
    body.textContent = text;
    div.append(who, body);
    $("log").append(div);
    div.scrollIntoView({ block: "end" });
    return body;
  }

  // 異常を表示する（種類ごとの文言と次の操作は page.js）。会話を終える異常は呼び出し側で end する
  function showProblem(kind) {
    const p = P.problem(kind);
    if (p.place === "note") note(p.text);
    else status(p.text, "error");
    return p;
  }

  async function post(path, body, headers) {
    return fetch(path, { method: "POST", headers: Object.assign({}, H, headers || {}), body: body || "",
                         credentials: "same-origin" });
  }

  // POST の応答を分類する（page.js の postOutcome）。想定外の失敗なら会話を終える。
  // 返り値は {outcome, body}（body は応答の JSON。読めなければ {}）
  async function checked(kind, r, sid) {
    const body = await r.json().catch(() => ({}));
    const outcome = P.postOutcome(kind, r.status, body && body.error);
    if (outcome === "lost") lost(sid);
    return { outcome, body: body || {} };
  }

  function q(path) { return path + (path.includes("?") ? "&" : "?") + "session=" + encodeURIComponent(session); }

  // 通信の切断（SSE の切断・POST の失敗）: 会話を終え、マイクを解放する。自動で再開しない・古い発言を送り直さない
  function lost(sid) {
    if (!session || (sid !== undefined && sid !== session)) return;
    showProblem("disconnected");
    end(true);
  }

  // ------------------------------------------------------------ 会話の開始と終了

  async function start() {
    $("start").disabled = true;
    note("");
    seqFilter = P.createSeqFilter();
    fails = P.createFailCounter();
    turn = 0; cut = -1; sendId = 0; pendingSend = 0; cutHold.clear();
    try {
      if (!config) config = await (await fetch("/api/config", { credentials: "same-origin" })).json();
      const r = await post("/api/session");
      if (r.status === 409) {
        status("別の会話が動いています（今の会話は奪いません）", "error");
        $("start").disabled = false;
        return;
      }
      if (!r.ok) throw new Error("会話を始められません（" + r.status + "）");
      session = (await r.json()).session;
      openEvents();
      await openMic();
    } catch (e) {
      const kind = P.micErrorKind(e);
      if (kind) showProblem(kind);
      else status(String(e && e.message || e), "error");
      await end(true);                      // 作った会話をサーバーでも終える（残すと再試行が 409 busy になる）
    }
  }

  // イベントも ping も SILENCE_LIMIT_MS 来なければ切断として扱う
  function kick() {
    if (watchdog) clearTimeout(watchdog);
    const sid = session;
    watchdog = setTimeout(() => { watchdog = null; lost(sid); }, P.SILENCE_LIMIT_MS);
  }

  function openEvents() {
    events = new EventSource("/api/events?session=" + encodeURIComponent(session));
    events.onopen = kick;
    events.onmessage = m => { kick(); onEvent(JSON.parse(m.data)); };
    events.addEventListener("ping", kick);
    events.onerror = () => lost();          // EventSource の自動の再接続はしない（cleanup で閉じる）
    kick();
  }

  async function openMic() {
    const sid = session;
    const s = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true } });
    if (session !== sid) { s.getTracks().forEach(t => t.stop()); return; }   // 許可を待つ間に会話が終わった
    stream = s;
    stream.getAudioTracks().forEach(t => {
      t.onended = () => {                   // マイクが抜けた
        if (session !== sid) return;
        showProblem("mic_ended");
        end(true);
      };
    });
    const ctx = new AudioContext();
    audio = ctx;
    await ctx.audioWorklet.addModule("/static/vad-worklet.js");
    if (session !== sid) { s.getTracks().forEach(t => t.stop()); ctx.close().catch(() => {}); return; }
    const src = audio.createMediaStreamSource(stream);
    node = new AudioWorkletNode(audio, "vad-frames", { numberOfOutputs: 1 });
    node.port.onmessage = m => onFrame(m.data.rms, m.data.samples, m.data.t);
    src.connect(node);
    node.connect(audio.destination);        // 出力は無音（処理を動かすためだけにつなぐ）
    background = [];
    detector = null;
    ring = [];
    status("背景の音を測っています（1 秒ほど黙っていてください）", "busy");
    $("pause").disabled = false;
    $("end").disabled = false;
  }

  async function end(notify) {
    const sid = session;
    cleanup();
    if (sid && notify !== false) {
      $("start").disabled = true;           // サーバーが会話を終えるまで次の会話を始めない
      try { await post("/api/end?session=" + encodeURIComponent(sid)); } catch (e) { /* 終わっていれば何もしない */ }
      $("start").disabled = false;
    }
  }

  // 録音・再生・待ちの音・取りに行く途中の要求をすべて止め、マイクを解放する
  function cleanup() {
    session = null;
    if (watchdog) { clearTimeout(watchdog); watchdog = null; }
    if (events) { events.close(); events = null; }
    stopCalibration();
    clearApproval();
    withdrawn = null;
    if (node) { node.port.onmessage = null; node.disconnect(); node = null; }
    if (stream) { stream.getTracks().forEach(t => { t.onended = null; t.stop(); }); stream = null; }
    resetPlayback(0);
    if (audio) { audio.close().catch(() => {}); audio = null; }
    detector = null; current = null; ring = []; background = []; paused = false;
    awaiting = false; lastEnd = -Infinity; cut = -1; cutHold.clear(); pendingSend = 0;
    $("start").disabled = false;
    $("pause").disabled = true;
    $("pause").textContent = "マイクを一時停止";
    $("end").disabled = true;
  }

  // ------------------------------------------------------------ 回り込みの測定（会話の開始時に 1 回）

  async function startCalibration() {
    const sid = session;
    calibration = { rms: [], playing: false, source: null, ctrl: null, timer: null };
    status("回り込みを測っています（あいさつが流れます。黙っていてください）", "busy");
    let ok = false;
    try {
      const r = await post(q("/api/calibrate"));
      if (session !== sid) return;
      const { outcome, body } = await checked("calibrate", r, sid);
      if (outcome === "lost" || session !== sid) return;
      ok = outcome === "ok" && body.audio === true;   // 409（測定済み）は回り込み 0 で続ける
    } catch (e) {
      return lost(sid);
    }
    if (!calibration) return;               // 応答より先に、あいさつを再生し終えた
    if (!ok) return finishCalibration(null, "あいさつを合成できませんでした");
    if (!calibration.timer) {
      calibration.timer = setTimeout(() => finishCalibration(null, "あいさつを再生できませんでした"),
                                     CALIBRATION_PLAY_MS);
    }
  }

  async function onCalibrationAudio(ev) {
    const c = calibration;
    if (!c || c.source || c.ctrl || !audio) return;
    const sid = session;
    if (c.timer) clearTimeout(c.timer);
    c.timer = setTimeout(() => finishCalibration(null, "あいさつを再生できませんでした"), CALIBRATION_PLAY_MS);
    const item = { id: ev.id };
    try {
      c.ctrl = new AbortController();
      const r = await fetch("/api/audio/" + encodeURIComponent(ev.id) + "?session=" + encodeURIComponent(sid),
                            { credentials: "same-origin", cache: "no-store", signal: c.ctrl.signal });
      if (!r.ok) throw new Error("音声を取れません（" + r.status + "）");
      const buffer = await audio.decodeAudioData(await r.arrayBuffer());
      if (calibration !== c || session !== sid) return;
      const s = audio.createBufferSource();
      s.buffer = buffer;
      s.connect(audio.destination);
      s.onended = () => {
        s.onended = null;
        s.disconnect();
        c.source = null;
        c.playing = false;
        lastEnd = performance.now();
        played(item, ev.turn, "end");
        finishCalibration(c.rms);
      };
      s.start();
      c.source = s;
      c.playing = true;                     // start から end までのマイクの RMS を回り込みとして集める
      played(item, ev.turn, "start");
    } catch (e) {
      if (calibration !== c || session !== sid) return;
      played(item, ev.turn, "error");
      finishCalibration(null, "あいさつを再生できませんでした");
    }
  }

  function stopCalibration() {
    const c = calibration;
    calibration = null;
    if (!c) return;
    if (c.timer) clearTimeout(c.timer);
    if (c.ctrl) c.ctrl.abort();
    if (c.source) {
      c.source.onended = null;
      try { c.source.stop(); } catch (e) { /* 止まっていれば何もしない */ }
      try { c.source.disconnect(); } catch (e) { /* 外れていれば何もしない */ }
    }
  }

  // 再生中のしきい値 = max(通常のしきい値, 回り込みの p95 × barge_margin)。測れなければ回り込み 0（通常のしきい値）
  function finishCalibration(rms, reason) {
    if (!calibration || !detector) return;
    stopCalibration();
    const measured = rms && rms.length ? rms : [];
    const playTh = V.playThresholdFrom(measured, detector.threshold, config.barge_margin);
    detector.setPlayThreshold(playTh);
    detector.reset();
    current = null; ring = [];
    const db = x => Math.round(V.dbfs(x));
    if (measured.length) {
      note("回り込み " + db(V.percentile(measured, 95)) + " dBFS・再生中のしきい値 " + db(playTh) +
           " dBFS（通常 " + db(detector.threshold) + " dBFS）");
    } else {
      note((reason || "回り込みを測れませんでした") + "。再生中も通常のしきい値で聞きます");
    }
    status(paused ? "マイクを一時停止しています" : "聞いています", paused ? "" : "listening");
  }

  // ------------------------------------------------------------ 発話の検出

  // 再生している間（文の start から最後の文の終了の 0.3 秒後まで）は再生中のしきい値
  function playingNow() {
    return source !== null || performance.now() - lastEnd < AFTER_SPEECH_MS;
  }

  function onFrame(rms, samples, t) {
    if (paused || !session) return;
    if (!detector) {
      background.push(rms);
      if (background.length < CALIBRATE_FRAMES) return;
      const threshold = V.thresholdFrom(background, config.threshold_factor, config.min_threshold);
      detector = V.createDetector({ threshold, silenceSeconds: config.silence_seconds,
                                    maxSeconds: config.max_seconds, prerollSeconds: config.preroll_seconds });
      startCalibration();
      return;
    }
    if (calibration) {                      // 測っている間は聞かない（あいさつの再生中の音量を集めるだけ）
      if (calibration.playing) calibration.rms.push(rms);
      return;
    }
    detector.setPlaying(playingNow());
    ring.push(samples);
    if (ring.length > detector.backFrames) ring.shift();
    if (current) current.push(samples);
    const ev = detector.push(rms);
    if (!ev) return;
    const action = P.micAction(ev.type, awaiting, approval !== null);
    if (action === "ignore") {              // 確認のカードを出している間はボタンだけで決める（T-2503）
      current = null;
      ring = [];
      if (ev.type === "start") note("確認を待っている間は声を受け付けません。画面のボタンで選んでください");
      return;
    }
    if (ev.type === "start") {
      current = ring.slice();               // 判定の窓と、その前の 0.3 秒を含める
      if (action === "barge") barge(t, ev.onsetFrames);   // 考えている間・話している間なら割り込み
      return;
    }
    const frames = current;
    current = null;
    ring = [];
    if (ev.reason === "max") note("長いので区切りました（" + config.max_seconds + " 秒）");
    if (!ev.keep) return;                   // 短すぎる音（咳・物音）は送らない
    send(frames);
  }

  // 割り込み: サーバーの応答を待たずに再生を止め、待ちの音を捨て、まだ取りに行っていない音を取りに行かない。
  // そのうえで /api/interrupt を送り、そのまま発話を録り続ける
  function barge(t, onsetFrames) {
    const detect = audio.currentTime;
    const frameEnd = Number.isFinite(t) ? t : detect;
    const onset = frameEnd - (onsetFrames + 1) * V.FRAME_SECONDS;   // 最初に超えたフレームの始まり
    const wasPlaying = source !== null;
    const stopCall = audio.currentTime;
    discardPlayback();                      // AudioBufferSourceNode.stop
    withdrawApproval();                     // 待っている承認はサーバーが割り込みで取り消す（声は承認にならない）
    turnDone = true;                        // 割り込んだ発言の音は再生しない
    awaiting = false;
    answerEl = null;
    if (pendingSend) {
      cutHold.cut(pendingSend);             // 番号の分からない発言（応答がまだ）。番号が分かったら割り込みを送る
      if (turn > pendingBase) {             // 応答より先に SSE で番号が分かっている: すぐ割り込み、以後の古いイベントを捨てる
        cut = Math.max(cut, turn);          // （応答の後の割り込みの再送は、サーバーが 409 で何もしない）
        interrupt(turn);
      }
    } else {
      cut = Math.max(cut, turn);
      interrupt(turn);
    }
    status("聞いています（割り込み）", "listening");
    // 考えている間の割り込みも記録する（止める音はなく、停止は待ちの音を捨てた時刻）
    note(P.bargeNote(P.bargeTiming({ onset, detect, stopCall, outputLatency: audio.outputLatency || 0,
                                     baseLatency: audio.baseLatency || 0 }), wasPlaying));
  }

  function interrupt(t) {
    const sid = session;
    post(q("/api/interrupt"), JSON.stringify({ turn: t }), { "Content-Type": "application/json" })
      .then(r => checked("interrupt", r, sid))   // 409（古い番号）は何もしない。それ以外の失敗は会話を終える
      .catch(() => lost(sid));
  }

  async function send(frames) {
    const total = frames.reduce((n, f) => n + f.length, 0);
    const pcm = new Float32Array(total);
    let at = 0;
    for (const f of frames) { pcm.set(f, at); at += f.length; }
    const rate = audio.sampleRate;          // 16 kHz を仮定しない（サーバーで変換する）
    const sid = session, id = ++sendId;
    pendingSend = id;
    pendingBase = turn;
    awaiting = true;
    status("考え中", "busy");
    try {
      const r = await post("/api/utterance?session=" + encodeURIComponent(sid) + "&rate=" + rate,
                           V.encodeWav(pcm, rate), { "Content-Type": "audio/wav" });
      if (session !== sid) return;
      const { outcome, body } = await checked("utterance", r, sid);
      if (session !== sid) return;
      // 番号の確定（onTurnKnown）と同時に外す。本文を読み終えるまでに話し始めたら、その発言の割り込みとして保持する
      if (pendingSend === id) pendingSend = 0;
      if (outcome === "ok") {
        onTurnKnown(id, Number.isInteger(body.turn) ? body.turn : null);
      } else {                              // 受け付けない音声（400・413）。この発言は捨てて聞き続ける
        onTurnKnown(id, null);
        if (sendId === id) awaiting = false;
        status("音声を送れませんでした（" + r.status + " " + (body.message || body.error || "") + "）", "error");
      }
    } catch (e) {
      lost(sid);
    }
  }

  // 送った発言の番号が分かった（t は受け付けられなければ null）。応答の前に割り込んだ発言なら割り込みを送り、
  // 番号が分かるまで保持していたイベントを渡し直す（割り込んだ発言のものは捨て、次の発言のものは使う）
  function onTurnKnown(id, t) {
    const { cut: wasCut, release } = cutHold.known(id);
    if (wasCut && t !== null) {
      cut = Math.max(cut, t);
      interrupt(t);
    } else if (!wasCut && t !== null && t > turn) {
      resetPlayback(t);
    }
    for (const ev of release) {
      if (!session) return;
      dispatch(ev);
    }
  }

  // ------------------------------------------------------------ 読み上げ

  // 新しい発言を始める（前の発言の音を捨てる。取った WAV の参照も外す）
  function resetPlayback(t) {
    discardPlayback();
    if (approval && approval.turn !== t) withdrawApproval();   // 前の発言の承認は出さない（結果は出す）
    next = 0;
    turnDone = false;
    turn = t;
  }

  // 再生中・取得中・再生待ちの音を捨てる（取りに行く途中の要求も止め、取った WAV の参照も外す）
  function discardPlayback() {
    if (source) {
      source.onended = null;
      try { source.stop(); } catch (e) { /* 止まっていれば何もしない */ }
      try { source.disconnect(); } catch (e) { /* 外れていれば何もしない */ }
      lastEnd = performance.now();
    }
    source = null;
    for (const item of items.values()) {
      item.buffer = null;
      if (item.ctrl) item.ctrl.abort();
    }
    items = new Map();
  }

  // 知らせは 1 つずつ順に送る（短い文で end が start より先にサーバーへ届くと、start が 404 になり
  // 最初の再生の時刻を記録できない）
  function played(item, t, event) {
    if (!session) return;
    const sid = session;
    const path = "/api/played?session=" + encodeURIComponent(sid);
    const body = JSON.stringify({ turn: t, id: item.id, event });
    playedQueue = playedQueue
      .then(() => post(path, body, { "Content-Type": "application/json" }))
      .then(r => checked("played", r, sid))   // 古い番号（409）・保持していない音（404 not_found）は想定内
      .catch(() => lost(sid));
  }

  async function onAudio(ev) {
    // 今の発言の番号と違う音、割り込んだ発言の音、turn_done の後に届いた音は取りに行かない
    if (ev.turn !== turn || ev.turn <= cut || turnDone || !audio) return;
    const item = { id: ev.id, buffer: null, state: "loading", ctrl: new AbortController() };
    items.set(ev.index, item);
    const t = turn, sid = session;
    const stale = () => t !== turn || t <= cut || turnDone || session !== sid || !audio || items.get(ev.index) !== item;
    try {
      const r = await fetch("/api/audio/" + encodeURIComponent(ev.id) + "?session=" + encodeURIComponent(sid),
                            { credentials: "same-origin", cache: "no-store", signal: item.ctrl.signal });
      if (!r.ok) throw new Error("音声を取れません（" + r.status + "）");
      const data = await r.arrayBuffer();
      if (stale()) return;
      const buffer = await audio.decodeAudioData(data);
      if (stale()) return;                    // 復号の間に turn_done・割り込み・次の発言が来たら捨てる
      item.buffer = buffer;
      item.state = "ready";
    } catch (e) {
      if (stale()) return;
      item.state = "error";
    }
    item.ctrl = null;
    pump();
  }

  function playbackFailed() {
    showProblem(fails.fail() ? "playback_repeated" : "playback");
  }

  // 順番（index）どおりに 1 文ずつ続けて再生する（全文を待たない）。再生できない文は飛ばして続ける
  function pump() {
    while (!source && audio && !turnDone && items.has(next)) {
      const item = items.get(next), t = turn;
      if (item.state === "loading") return;
      if (item.state === "error") {
        items.delete(next);
        next += 1;
        played(item, t, "error");
        playbackFailed();
        continue;
      }
      try {
        const s = audio.createBufferSource();
        s.buffer = item.buffer;
        s.connect(audio.destination);
        s.onended = () => {
          s.onended = null;
          s.disconnect();
          if (source === s) source = null;
          item.buffer = null;                   // 取った WAV の参照を外す
          lastEnd = performance.now();
          fails.ok();
          played(item, t, "end");
          if (t === turn) pump();
        };
        s.start();
        source = s;
        items.delete(next);
        next += 1;
        played(item, t, "start");
        if (!paused) status("話しています", "speaking");
      } catch (e) {
        item.buffer = null;
        items.delete(next);
        next += 1;
        played(item, t, "error");
        playbackFailed();
      }
    }
  }

  // 発言の処理の終わり（サーバーは最後の文の再生の知らせを受けた、または待つのをやめた）
  function onTurnDone(t) {
    if (t !== turn) return;
    // この発言の取得中・再生待ち・再生中の音を捨て、後から届く同じ発言の音も再生しない
    turnDone = true;
    awaiting = false;
    discardPlayback();
    if ($("status").classList.contains("error")) return;   // 認識・回答の失敗の表示は残す（次に話せば消える）
    status(paused ? "マイクを一時停止しています" : "聞いています", paused ? "" : "listening");
  }

  // ------------------------------------------------------------ 画面での承認（T-1504）

  // 確認の文・「実行する」「実行しない」・残りの秒数を出す。今の発言の番号と違う要求は出さない
  function showApproval(ev) {
    if (!P.approvalShown(ev, turn, cut) || turnDone) return;
    clearApproval();
    const started = performance.now();
    const a = { turn: ev.turn, id: ev.id, timer: null, input: ev.input === true, review: ev.choices === "review" };
    approval = a;
    $("approval-text").textContent = ev.text;
    // 2 段目の確認（T-1702）: 「実行する」の代わりに入力欄と「送る」を出す。語の照合はサーバーが行う
    $("approve-run").hidden = a.input;
    $("approve-word").hidden = !a.input;
    $("approve-send").hidden = !a.input;
    $("approve-word").value = "";
    $("approve-run").disabled = a.input;
    $("approve-word").disabled = !a.input;
    $("approve-send").disabled = !a.input;
    $("approve-decline").disabled = false;
    // 知識の候補の確認（T-1802）: 「保存する」「保存しない」「あとで」
    $("approve-run").textContent = a.review ? "保存する" : "実行する";
    $("approve-decline").textContent = a.review ? "保存しない" : "実行しない";
    $("approve-later").hidden = !a.review;
    $("approve-later").disabled = !a.review;
    if (a.input) $("approve-word").focus();
    const tick = () => {
      $("approval-left").textContent = "残り " + P.secondsLeft(ev.expires_in, started, performance.now()) + " 秒";
    };
    tick();
    a.timer = setInterval(tick, 250);
    $("approval").hidden = false;
    status(a.input ? "確認を待っています（入力欄に入力して送ってください）" : "確認を待っています（画面のボタンで答えてください）",
           "busy");
  }

  // 押したら 1 回だけ送り、ボタンを無効にする（結果は approval_done で出す）
  function answerApproval(decision) {
    const a = approval;
    if (!a || !session) return;
    $("approve-run").disabled = true;
    $("approve-decline").disabled = true;
    $("approve-send").disabled = true;
    $("approve-word").disabled = true;
    $("approve-later").disabled = true;
    const body = { turn: a.turn, id: a.id, decision };
    if (a.input && decision === "run") body.text = $("approve-word").value;   // 入力した語は送るだけ（残さない）
    $("approve-word").value = "";
    const sid = session;
    post(q("/api/approve"), JSON.stringify(body), { "Content-Type": "application/json" })
      .then(r => checked("approve", r, sid))   // 409（期限切れ・取り消し・使用済み）は何も実行しない
      .catch(() => lost(sid));
  }

  function onApprovalDone(ev) {
    if (!P.approvalMatches(ev, approval)) return;
    clearApproval();
    line("system", "", "確認: " + P.approvalResultText(ev.result));
  }

  // 割り込みでボタンを消す。割り込んだ発言のイベントは捨てるが、その要求の結果（approval_done）だけは出す
  function withdrawApproval() {
    if (approval) withdrawn = { turn: approval.turn, id: approval.id };
    clearApproval();
  }

  function clearApproval() {
    if (approval && approval.timer) clearInterval(approval.timer);
    approval = null;
    $("approval").hidden = true;
    $("approval-text").textContent = "";
    $("approval-left").textContent = "";
    $("approve-run").disabled = true;
    $("approve-decline").disabled = true;
    $("approve-run").hidden = false;
    $("approve-word").hidden = true;
    $("approve-word").value = "";
    $("approve-word").disabled = true;
    $("approve-send").hidden = true;
    $("approve-send").disabled = true;
    $("approve-later").hidden = true;
    $("approve-later").disabled = true;
    $("approve-run").textContent = "実行する";
    $("approve-decline").textContent = "実行しない";
  }

  // ------------------------------------------------------------ イベント

  function onEvent(ev) {
    if (!session || ev.session !== session) return;
    if (!seqFilter.accept(ev)) return;        // 重複・順番の違うイベントは捨てる
    if (ev.type === "ended") {
      line("system", "", "会話を終えました");
      status("会話を終えました", "");
      cleanup();
      return;
    }
    if (ev.type === "audio" && ev.calibration) { onCalibrationAudio(ev); return; }
    if (ev.type === "notice") { note(ev.message); return; }
    if (ev.type === "run_result") {           // 承認して実行した後に割り込まれた発言の結果（読み上げはしない）
      setLinked(line("ai", "JARVIS（実行の結果）", ""), ev.text);
      return;
    }
    if (ev.type === "approval_done" && P.approvalMatches(ev, withdrawn)) {   // 割り込んだ発言の確認の結果
      withdrawn = null;
      line("system", "", "確認: " + P.approvalResultText(ev.result));
      return;
    }
    dispatch(ev);
  }

  function dispatch(ev) {
    const d = P.turnDecision(ev, turn, cut);
    if (d === "stale") return;                // 割り込んだ・古い発言のイベントは捨てる
    if (d === "new") {
      // 番号の分からないまま割り込んだ発言があれば、番号が分かるまで保持する（次の発言のイベントを失わない）
      if (cutHold.holding) { cutHold.hold(ev); return; }
      // /api/utterance の応答より先に、その発言のイベントが届くことがある。新しい番号はここで反映する
      resetPlayback(ev.turn);
    }
    switch (ev.type) {
      case "ready":
        break;
      case "recognized":
        line("owner", "あなた", ev.text);
        answerEl = null;
        status("考え中", "busy");
        break;
      case "ignored":
        break;                                // 次の受け付けは turn_done で
      case "audio":
        onAudio(ev);
        break;
      case "turn_done":
        onTurnDone(ev.turn);
        break;
      case "approval":
        showApproval(ev);
        break;
      case "approval_done":
        onApprovalDone(ev);
        break;
      case "answer_chunk":
        if (!answerEl) answerEl = line("ai", "JARVIS", "");
        answerEl.textContent += ev.text;
        break;
      case "answer_done":
        if (!answerEl) answerEl = line("ai", "JARVIS", "");
        setLinked(answerEl, ev.text);        // 逐次表示しなかった回答もここで全文を出す（URL はリンクに）
        answerEl = null;
        break;
      case "error":
        if (ev.kind === "playback") break;    // ページで数えて表示済み
        if (ev.kind === "tts") { showProblem("tts"); break; }   // 文字の回答は続ける
        line("error", "エラー", ev.message || ev.kind);
        showProblem(ev.kind === "stt" || ev.kind === "answer" ? ev.kind : "other");
        break;
    }
  }

  // ------------------------------------------------------------ ボタン

  $("start").onclick = start;
  $("approve-run").onclick = () => answerApproval("run");
  $("approve-decline").onclick = () => answerApproval("decline");
  $("approve-send").onclick = () => answerApproval("run");
  $("approve-later").onclick = () => answerApproval("later");
  $("approve-word").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); answerApproval("run"); } });
  $("end").onclick = () => { status("会話を終えました", ""); end(true); };
  $("pause").onclick = () => {
    paused = !paused;
    if (detector) detector.reset();
    current = null; ring = [];
    if (stream) stream.getAudioTracks().forEach(t => { t.enabled = !paused; });
    $("pause").textContent = paused ? "マイクを再開" : "マイクを一時停止";
    status(paused ? "マイクを一時停止しています" : source ? "話しています" :
           (detector ? "聞いています" : "背景の音を測っています"), paused ? "" : source ? "speaking" : "listening");
  };
})();
