// 発話の検出（音量による判定、T-1501・BV-03。再生中のしきい値は T-1503・BV-07）。ブラウザでは window.JarvisVad、Node では module.exports。
// 純粋な関数だけにして、Node の node --test で確かめる（tests/js/vad.test.js）。
(function (root) {
  "use strict";

  const FRAME_SECONDS = 0.01;   // vad-worklet.js が 10 ms ごとに RMS を送る
  const WINDOW = 16;            // 直近 16 フレーム（0.16 秒）のうち
  const NEEDED = 12;            // 12 フレームがしきい値を超えたら話し始め
  const RELEASE_RATIO = 0.7;    // 話している途中は、しきい値の 0.7 倍を下回るフレームを無音と数える
  const MIN_SPEECH_SECONDS = 0.3;

  function median(values) {
    if (!values.length) return 0;
    const s = [...values].sort((a, b) => a - b);
    const m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }

  // 背景の音（最初の 1 秒の RMS）からしきい値を決める: max(中央値 × 倍率, 下限)
  function thresholdFrom(rmsValues, factor, minThreshold) {
    return Math.max(median(rmsValues) * factor, minThreshold);
  }

  // p パーセンタイル（最近傍順位法、latency.py と同じ）。空なら 0
  function percentile(values, p) {
    if (!values.length) return 0;
    const s = [...values].sort((a, b) => a - b);
    return s[Math.min(s.length, Math.max(1, Math.ceil(p / 100 * s.length))) - 1];
  }

  // 再生中のしきい値（T-1503・BV-07）: max(通常のしきい値, 回り込み（あいさつの再生中の RMS の p95）× 余裕の倍率)。
  // 測れなかった（空）ときは通常のしきい値
  function playThresholdFrom(rmsDuringPlayback, threshold, margin) {
    return Math.max(threshold, percentile(rmsDuringPlayback, 95) * margin);
  }

  // RMS（-1〜1）→ dBFS（表示用）
  function dbfs(rms) {
    return rms > 0 ? 20 * Math.log10(rms) : -Infinity;
  }

  // フレームの RMS を 1 つずつ push し、{type: "start", onsetFrames} か {type: "end", reason, keep, speechSeconds} を
  // 受け取る。start の時点で、直近 backFrames フレーム（判定の窓＋前の 0.3 秒）を発話に含める。onsetFrames は、
  // 判定の窓の中で最初にしきい値を超えたフレームが、今のフレームの何フレーム前か（割り込みの時間の計測に使う）。
  // setPlaying(true) の間（読み上げの再生中）は、しきい値の代わりに playThreshold（それより小さければ threshold）で判定する。
  function createDetector(opts) {
    const o = Object.assign({ frameSeconds: FRAME_SECONDS, silenceSeconds: 0.8, maxSeconds: 20,
                              prerollSeconds: 0.3, minSpeechSeconds: MIN_SPEECH_SECONDS }, opts);
    const frames = s => Math.round(s / o.frameSeconds);
    const silenceN = Math.max(1, frames(o.silenceSeconds));
    const maxN = Math.max(WINDOW, frames(o.maxSeconds));
    const minN = frames(o.minSpeechSeconds);
    let speaking = false, recent = [], length = 0, voicedEnd = 0, quiet = 0;
    let playing = false, playThreshold = o.playThreshold == null ? o.threshold : o.playThreshold;

    function current() {
      return playing ? Math.max(o.threshold, playThreshold) : o.threshold;
    }

    function reset() {
      speaking = false; recent = []; length = 0; voicedEnd = 0; quiet = 0;
    }

    function finish(reason) {
      const ev = { type: "end", reason, keep: voicedEnd >= minN,
                   speechSeconds: +(voicedEnd * o.frameSeconds).toFixed(3) };
      reset();
      return ev;
    }

    function push(rms) {
      const threshold = current();
      if (!speaking) {
        recent.push(rms >= threshold);
        if (recent.length > WINDOW) recent.shift();
        if (recent.filter(Boolean).length < NEEDED) return null;
        speaking = true;                       // 窓の最初のフレームを話し始めとする
        length = recent.length;
        voicedEnd = recent.lastIndexOf(true) + 1;
        quiet = 0;
        const onsetFrames = recent.length - 1 - recent.indexOf(true);
        recent = [];
        return { type: "start", onsetFrames };
      }
      length += 1;
      if (rms < threshold * RELEASE_RATIO) {
        quiet += 1;
      } else {
        quiet = 0;
        voicedEnd = length;
      }
      if (quiet >= silenceN) return finish("silence");
      if (length >= maxN) return finish("max");
      return null;
    }

    return {
      push, reset, backFrames: WINDOW + frames(o.prerollSeconds),
      setThreshold(value) { if (Number.isFinite(value) && value > 0) o.threshold = value; },
      setPlaying(value) { playing = !!value; },
      setPlayThreshold(value) { playThreshold = value; },
      get hasEnoughSpeech() { return voicedEnd >= minN; },
      get speaking() { return speaking; },
      get threshold() { return o.threshold; },
      get playThreshold() { return Math.max(o.threshold, playThreshold); },
      get currentThreshold() { return current(); },
    };
  }

  // PCM 16 bit・モノラルの WAV（サンプルレートはそのまま）
  function encodeWav(samples, sampleRate) {
    const buf = new ArrayBuffer(44 + samples.length * 2);
    const v = new DataView(buf);
    const text = (at, s) => { for (let i = 0; i < s.length; i++) v.setUint8(at + i, s.charCodeAt(i)); };
    text(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVE");
    text(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, sampleRate, true); v.setUint32(28, sampleRate * 2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    text(36, "data"); v.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const x = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true);
    }
    return buf;
  }

  const api = { FRAME_SECONDS, WINDOW, NEEDED, RELEASE_RATIO, MIN_SPEECH_SECONDS,
                median, thresholdFrom, percentile, playThresholdFrom, dbfs, createDetector, encodeWav };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.JarvisVad = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
