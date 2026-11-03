// ブラウザでの声の会話のページ（T-1501）。話すと自動で区切って送り、認識した文と回答を文字で出す。
// 音声・本文はブラウザの保存領域に置かない。読み上げ（T-1502）・割り込み（T-1503）・画面での承認（T-1504）はまだない。
(function () {
  "use strict";
  const V = window.JarvisVad;
  const H = { "X-Jarvis-Voice": "1" };
  const $ = id => document.getElementById(id);
  const CALIBRATE_FRAMES = Math.round(1 / V.FRAME_SECONDS);   // 最初の 1 秒で背景の音を測る

  let config = null, session = null, events = null, stream = null, audio = null, node = null;
  let detector = null, paused = false, background = [], ring = [], current = null, answerEl = null;

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
      await end(false);
    }
  }

  function openEvents() {
    events = new EventSource("/api/events?session=" + encodeURIComponent(session));
    events.onmessage = m => onEvent(JSON.parse(m.data));
    events.onerror = () => {
      if (!session) return;
      status("サーバーとの接続が切れました。会話は終わりました（もう一度「会話を始める」を押してください）", "error");
      cleanup();
    };
  }

  async function openMic() {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true } });
    audio = new AudioContext();
    await audio.audioWorklet.addModule("/static/vad-worklet.js");
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
      try { await post("/api/end?session=" + encodeURIComponent(sid)); } catch (e) { /* 終わっていれば何もしない */ }
    }
  }

  function cleanup() {
    session = null;
    if (events) { events.close(); events = null; }
    if (node) { node.port.onmessage = null; node.disconnect(); node = null; }
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
    if (audio) { audio.close(); audio = null; }
    detector = null; current = null; ring = []; background = []; paused = false;
    $("start").disabled = false;
    $("pause").disabled = true;
    $("pause").textContent = "マイクを一時停止";
    $("end").disabled = true;
  }

  // ------------------------------------------------------------ 発話の検出

  function onFrame(rms, samples) {
    if (paused || !session) return;
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
        note("前の発言を考えているため、今の発言は送りませんでした");
      } else if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        status("音声を送れませんでした（" + r.status + " " + (body.message || body.error || "") + "）", "error");
      }
    } catch (e) {
      status("音声を送れませんでした（通信の失敗）", "error");
    }
  }

  // ------------------------------------------------------------ イベント

  function onEvent(ev) {
    if (!session || ev.session !== session) return;
    switch (ev.type) {
      case "ready":
        break;
      case "recognized":
        line("owner", "あなた", ev.text);
        answerEl = null;
        status("考え中", "busy");
        break;
      case "ignored":
        if (!paused && detector) status("聞いています", "listening");
        break;
      case "answer_chunk":
        if (!answerEl) answerEl = line("ai", "JARVIS", "");
        answerEl.textContent += ev.text;
        break;
      case "answer_done":
        if (!answerEl) answerEl = line("ai", "JARVIS", "");
        answerEl.textContent = ev.text;      // 逐次表示しなかった回答もここで全文を出す
        answerEl = null;
        if (!paused) status("聞いています", "listening");
        break;
      case "error":
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
    status(paused ? "マイクを一時停止しています" : (detector ? "聞いています" : "背景の音を測っています"),
           paused ? "" : "listening");
  };
})();
