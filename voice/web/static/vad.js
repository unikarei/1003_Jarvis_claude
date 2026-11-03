// 発話の検出（音量による判定、T-1501・BV-03）。ブラウザでは window.JarvisVad、Node では module.exports。
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

  // フレームの RMS を 1 つずつ push し、{type: "start"} か {type: "end", reason, keep, speechSeconds} を受け取る。
  // start の時点で、直近 backFrames フレーム（判定の窓＋前の 0.3 秒）を発話に含める。
  function createDetector(opts) {
    const o = Object.assign({ frameSeconds: FRAME_SECONDS, silenceSeconds: 0.8, maxSeconds: 20,
                              prerollSeconds: 0.3, minSpeechSeconds: MIN_SPEECH_SECONDS }, opts);
    const frames = s => Math.round(s / o.frameSeconds);
    const silenceN = Math.max(1, frames(o.silenceSeconds));
    const maxN = Math.max(WINDOW, frames(o.maxSeconds));
    const minN = frames(o.minSpeechSeconds);
    let speaking = false, recent = [], length = 0, voicedEnd = 0, quiet = 0;

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
      if (!speaking) {
        recent.push(rms >= o.threshold);
        if (recent.length > WINDOW) recent.shift();
        if (recent.filter(Boolean).length < NEEDED) return null;
        speaking = true;                       // 窓の最初のフレームを話し始めとする
        length = recent.length;
        voicedEnd = recent.lastIndexOf(true) + 1;
        quiet = 0;
        recent = [];
        return { type: "start" };
      }
      length += 1;
      if (rms < o.threshold * RELEASE_RATIO) {
        quiet += 1;
      } else {
        quiet = 0;
        voicedEnd = length;
      }
      if (quiet >= silenceN) return finish("silence");
      if (length >= maxN) return finish("max");
      return null;
    }

    return { push, reset, backFrames: WINDOW + frames(o.prerollSeconds), get speaking() { return speaking; } };
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
                median, thresholdFrom, createDetector, encodeWav };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.JarvisVad = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
