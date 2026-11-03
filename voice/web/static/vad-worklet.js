// マイクの音を 10 ms ごとのフレームにして、RMS と音そのものをページへ送る（T-1501）。音は保存しない。
class VadFrames extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = Math.max(1, Math.round(sampleRate * 0.01));
    this.buf = new Float32Array(this.size);
    this.n = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.buf[this.n++] = ch[i];
      if (this.n === this.size) {
        let sum = 0;
        for (let j = 0; j < this.size; j++) sum += this.buf[j] * this.buf[j];
        const frame = this.buf;
        this.port.postMessage({ rms: Math.sqrt(sum / this.size), samples: frame }, [frame.buffer]);
        this.buf = new Float32Array(this.size);
        this.n = 0;
      }
    }
    return true;
  }
}

registerProcessor("vad-frames", VadFrames);
