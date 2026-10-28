/** Small streaming audio primitives shared by the call controller and focused tests. */
export class SpeechChunks {
  constructor() { this.text = ''; this.started = false; }
  feed(text) {
    this.text += text;
    const result = [];
    while (this.text.length) {
      const limit = this.started ? 40 : 20;
      let cut = 0;
      for (const match of this.text.matchAll(/[。！？!?，,；;\n]/g)) {
        if (match.index >= 5 || (match.index > 0 && /[。！？!?\n]/.test(match[0]))) {
          cut = match.index + 1; break;
        }
      }
      if ((!cut || cut > limit) && this.text.length >= limit) cut = limit;
      if (!cut) break;
      result.push(this.text.slice(0, cut));
      this.started = true;
      this.text = this.text.slice(cut);
    }
    return result;
  }
  flush() { const text = this.text; this.text = ''; return text.trim() ? [text] : []; }
}
export class PcmDecoder {
  constructor() { this.tail = null; }
  reset() { this.tail = null; }
  feed(chunk) {
    let bytes = chunk;
    if (this.tail !== null) {
      bytes = new Uint8Array(chunk.length + 1);
      bytes[0] = this.tail; bytes.set(chunk, 1);
    }
    this.tail = bytes.length % 2 ? bytes[bytes.length - 1] : null;
    const samples = new Float32Array(Math.floor(bytes.length / 2));
    const view = new DataView(bytes.buffer, bytes.byteOffset, samples.length * 2);
    for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
    return samples;
  }
}
export class PcmPlayer {
  constructor(context) { this.context = context; this.sources = new Set(); this.next = 0; this.decoder = new PcmDecoder(); }
  get playing() { return this.sources.size > 0; }
  push(bytes, rate = 24000) {
    const samples = this.decoder.feed(bytes);
    if (!samples.length) return;
    const buffer = this.context.createBuffer(1, samples.length, rate);
    buffer.copyToChannel(samples, 0);
    const source = this.context.createBufferSource();
    source.buffer = buffer; source.connect(this.context.destination);
    source.onended = () => { this.sources.delete(source); source.disconnect(); };
    this.sources.add(source);
    this.next = Math.max(this.next, this.context.currentTime + 0.06);
    source.start(this.next);
    this.next += samples.length / rate;
  }
  stop() {
    for (const source of this.sources) { try { source.stop(); source.disconnect(); } catch {} }
    this.sources.clear(); this.next = 0; this.decoder.reset();
  }
}
