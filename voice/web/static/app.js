// ブラウザでの声の会話のページ（T-1501・T-1502）。話すと自動で区切って送り、認識した文と回答を文字で出し、
// 回答を文ごとに受け取って最初の文から順に読み上げる。読み上げている間は発話の検出を止める（割り込みは T-1503）。
// 音声・本文はブラウザの保存領域に置かない。画面での承認（T-1504）はまだない。
(function () {
  "use strict";
  const V = window.JarvisVad;
  const H = { "X-Jarvis-Voice": "1" };
  const $ = id => document.getElementById(id);
  const CALIBRATE_FRAMES = Math.round(1 / V.FRAME_SECONDS);   // 最初の 1 秒で背景の音を測る
  const AFTER_SPEECH_MS = 300;              // 最後の文を読み終えてから聞き始めるまで（自分の声の残りを拾わない）

  let config = null, session = null, events = null, stream = null, audio = null, node = null;
  let detector = null, paused = false, background = [], ring = [], current = null, answerEl = null;
  // 読み上げ: turn は今の発言の番号。items は順番 → {id, buffer, state: "loading"|"ready"|"error"}
  let turn = 0, items = new Map(), next = 0, source = null, muted = false, lastEnd = 0, unmuteTimer = null;
  let playedQueue = Promise.resolve();      // /api/played を送る順番（start → end）を守る

  function status(text, kind) {
    $("status").textContent = text;
    $("status").className = "status " + (kind || "");
  }

  function note(text) { $("note").textContent = text || ""; }

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

  async function post(path, body, headers) {
    return fetch(path, { method: "POST", headers: Object.assign({}, H, headers || {}), body: body || "",
                         credentials: "same-origin" });
  }

  // ------------------------------------------------------------ 会話の開始と終了

  async function start() {
    $("start").disabled = true;
    note("");
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
      if (e && (e.name === "NotAllowedError" || e.name === "SecurityError")) {
        status("マイクの使用が許可されていません。ブラウザの設定でマイクを許可してから、もう一度始めてください", "error");
      } else if (e && e.name === "NotFoundError") {
        status("マイクが見つかりません。マイクをつないでから、もう一度始めてください", "error");
      } else {
        status(String(e && e.message || e), "error");
      }
      await end(true);                      // 作った会話をサーバーでも終える（残すと再試行が 409 busy になる）
    }
  }

  function openEvents() {
    events = new EventSource("/api/events?session=" + encodeURIComponent(session));
    events.onmessage = m => onEvent(JSON.parse(m.data));
    events.onerror = () => {
      if (!session) return;
      status("サーバーとの接続が切れました。会話は終わりました（もう一度「会話を始める」を押してください）", "error");
      end(true);                            // サーバーが切断に気づく前でも会話を終え、直後の再試行を 409 busy にしない
    };
  }

  async function openMic() {
    const sid = session;
    const s = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true } });
    if (session !== sid) { s.getTracks().forEach(t => t.stop()); return; }   // 許可を待つ間に会話が終わった
    stream = s;
    const ctx = new AudioContext();
    audio = ctx;
    await ctx.audioWorklet.addModule("/static/vad-worklet.js");
    if (session !== sid) { s.getTracks().forEach(t => t.stop()); ctx.close().catch(() => {}); return; }
    const source = audio.createMediaStreamSource(stream);
    node = new AudioWorkletNode(audio, "vad-frames", { numberOfOutputs: 1 });
    node.port.onmessage = m => onFrame(m.data.rms, m.data.samples);
    source.connect(node);
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

  function cleanup() {
    session = null;
    if (events) { events.close(); events = null; }
    if (node) { node.port.onmessage = null; node.disconnect(); node = null; }
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
    if (audio) { audio.close().catch(() => {}); audio = null; }
    detector = null; current = null; ring = []; background = []; paused = false;
    resetPlayback(0);
    muted = false; lastEnd = 0;
    $("start").disabled = false;
    $("pause").disabled = true;
    $("pause").textContent = "マイクを一時停止";
    $("end").disabled = true;
  }

  // ------------------------------------------------------------ 発話の検出

  function onFrame(rms, samples) {
    if (paused || !session || muted) return;  // 読み上げている間は聞かない（割り込みは T-1503）
    if (!detector) {
      background.push(rms);
      if (background.length < CALIBRATE_FRAMES) return;
      const threshold = V.thresholdFrom(background, config.threshold_factor, config.min_threshold);
      detector = V.createDetector({ threshold, silenceSeconds: config.silence_seconds,
                                    maxSeconds: config.max_seconds, prerollSeconds: config.preroll_seconds });
      status("聞いています", "listening");
      return;
    }
    ring.push(samples);
    if (ring.length > detector.backFrames) ring.shift();
    if (current) current.push(samples);
    const ev = detector.push(rms);
    if (!ev) return;
    if (ev.type === "start") {
      current = ring.slice();               // 判定の窓と、その前の 0.3 秒を含める
      return;
    }
    const frames = current;
    current = null;
    ring = [];
    if (ev.reason === "max") note("長いので区切りました（" + config.max_seconds + " 秒）");
    if (!ev.keep) return;                   // 短すぎる音（咳・物音）は送らない
    send(frames);
  }

  async function send(frames) {
    const total = frames.reduce((n, f) => n + f.length, 0);
    const pcm = new Float32Array(total);
    let at = 0;
    for (const f of frames) { pcm.set(f, at); at += f.length; }
    const rate = audio.sampleRate;          // 16 kHz を仮定しない（サーバーで変換する）
    const sid = session;
    status("考え中", "busy");
    try {
      const r = await post("/api/utterance?session=" + encodeURIComponent(sid) + "&rate=" + rate,
                           V.encodeWav(pcm, rate), { "Content-Type": "audio/wav" });
      if (r.status === 409) {
        note("前の発言を処理しているため、今の発言は送りませんでした");
      } else if (r.ok) {
        const body = await r.json().catch(() => ({}));
        if (session === sid && Number.isInteger(body.turn) && body.turn > turn) resetPlayback(body.turn);
      } else {
        const body = await r.json().catch(() => ({}));
        status("音声を送れませんでした（" + r.status + " " + (body.message || body.error || "") + "）", "error");
      }
    } catch (e) {
      status("音声を送れませんでした（通信の失敗）", "error");
    }
  }

  // ------------------------------------------------------------ 読み上げ

  // 新しい発言を始める（前の発言の音を捨てる。取った WAV の参照も外す）
  function resetPlayback(t) {
    if (source) { source.onended = null; try { source.stop(); } catch (e) { /* 止まっていれば何もしない */ } }
    source = null;
    items = new Map();
    next = 0;
    if (unmuteTimer) { clearTimeout(unmuteTimer); unmuteTimer = null; }
    if (t > turn && muted) {                  // サーバーは前の発言を終えてから次を受け付ける（止めたままにしない）
      muted = false;
      if (detector) detector.reset();
      current = null; ring = [];
    }
    turn = t;
  }

  // 知らせは 1 つずつ順に送る（短い文で end が start より先にサーバーへ届くと、start が 404 になり
  // 最初の再生の時刻を記録できない）
  function played(item, t, event) {
    if (!session) return;
    const path = "/api/played?session=" + encodeURIComponent(session);
    const body = JSON.stringify({ turn: t, id: item.id, event });
    playedQueue = playedQueue
      .then(() => post(path, body, { "Content-Type": "application/json" }))
      .catch(() => { /* 届かなければサーバーが待つのをやめる */ });
  }

  async function onAudio(ev) {
    if (ev.turn !== turn || !audio) return;     // 今の発言の番号と違う音は取りに行かない
    const item = { id: ev.id, buffer: null, state: "loading" };
    items.set(ev.index, item);
    const t = turn, sid = session;
    try {
      const r = await fetch("/api/audio/" + encodeURIComponent(ev.id) + "?session=" + encodeURIComponent(sid),
                            { credentials: "same-origin", cache: "no-store" });
      if (!r.ok) throw new Error("音声を取れません（" + r.status + "）");
      const data = await r.arrayBuffer();
      if (t !== turn || session !== sid || !audio) return;
      item.buffer = await audio.decodeAudioData(data);
      item.state = "ready";
    } catch (e) {
      item.state = "error";
    }
    if (t === turn && session === sid) pump();
  }

  // 順番（index）どおりに 1 文ずつ続けて再生する（全文を待たない）
  function pump() {
    while (!source && audio && items.has(next)) {
      const item = items.get(next), t = turn;
      if (item.state === "loading") return;
      if (item.state === "error") {
        items.delete(next);
        next += 1;
        played(item, t, "error");
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
          played(item, t, "end");
          if (t === turn) pump();
        };
        startSpeaking();
        s.start();
        source = s;
        items.delete(next);
        next += 1;
        played(item, t, "start");
      } catch (e) {
        item.buffer = null;
        items.delete(next);
        next += 1;
        played(item, t, "error");
      }
    }
  }

  function startSpeaking() {
    if (unmuteTimer) { clearTimeout(unmuteTimer); unmuteTimer = null; }
    if (!muted) {
      muted = true;                           // 最初の文の開始から、発話の検出を止める
      if (detector) detector.reset();
      current = null; ring = [];
    }
    if (!paused) status("話しています", "speaking");
  }

  // 発言の処理の終わり（最後の文の終了の 0.3 秒後から聞く）
  function onTurnDone(t) {
    if (t !== turn) return;                   // 新しい番号は onEvent で反映済み。古い番号の知らせは無視する
    const wait = muted ? Math.max(0, lastEnd + AFTER_SPEECH_MS - performance.now()) : 0;
    if (unmuteTimer) clearTimeout(unmuteTimer);
    unmuteTimer = setTimeout(() => {
      unmuteTimer = null;
      if (t !== turn || !session) return;
      muted = false;
      if (detector) detector.reset();
      current = null; ring = [];
      if ($("status").classList.contains("error")) return;   // 認識・回答の失敗の表示は残す（次に話せば消える）
      status(paused ? "マイクを一時停止しています" : (detector ? "聞いています" : "背景の音を測っています"),
             paused ? "" : "listening");
    }, wait);
  }

  // ------------------------------------------------------------ イベント

  function onEvent(ev) {
    if (!session || ev.session !== session) return;
    // /api/utterance の応答より先に、その発言のイベント（recognized・ignored・turn_done など）が届くことがある。
    // 新しい番号はここで反映し、応答との到着順にかかわらず受付状態へ戻れるようにする
    if (ev.type !== "ended" && Number.isInteger(ev.turn) && ev.turn > turn) resetPlayback(ev.turn);
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
      case "notice":
        note(ev.message);
        break;
      case "turn_done":
        onTurnDone(ev.turn);
        break;
      case "answer_chunk":
        if (!answerEl) answerEl = line("ai", "JARVIS", "");
        answerEl.textContent += ev.text;
        break;
      case "answer_done":
        if (!answerEl) answerEl = line("ai", "JARVIS", "");
        answerEl.textContent = ev.text;      // 逐次表示しなかった回答もここで全文を出す
        answerEl = null;
        break;                                // 読み上げが終わるまで聞かない（turn_done で戻る）
      case "error":
        if (ev.kind === "playback") { note("読み上げの音を 1 文再生できませんでした（次の文に進みます）"); break; }
        line("error", "エラー", ev.message || ev.kind);
        status(ev.kind === "stt" ? "認識できませんでした。もう一度話してください" :
               ev.kind === "answer" ? "答えを作れませんでした。もう一度話してください" :
               "エラーがありました。もう一度話してください", "error");
        break;
      case "ended":
        line("system", "", "会話を終えました");
        status("会話を終えました", "");
        cleanup();
        break;
    }
  }

  // ------------------------------------------------------------ ボタン

  $("start").onclick = start;
  $("end").onclick = () => { status("会話を終えました", ""); end(true); };
  $("pause").onclick = () => {
    paused = !paused;
    if (detector) detector.reset();
    current = null; ring = [];
    if (stream) stream.getAudioTracks().forEach(t => { t.enabled = !paused; });
    $("pause").textContent = paused ? "マイクを再開" : "マイクを一時停止";
    status(paused ? "マイクを一時停止しています" : muted ? "話しています" :
           (detector ? "聞いています" : "背景の音を測っています"), paused ? "" : muted ? "speaking" : "listening");
  };
})();
