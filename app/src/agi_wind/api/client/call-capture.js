/** AudioContext runs at 16 kHz; the browser resamples the physical microphone. */
class CallCapture extends AudioWorkletProcessor {
  constructor() { super(); this.chunk = new Float32Array(640); this.offset = 0; }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (input) {
      for (const value of input) {
        this.chunk[this.offset++] = value;
        if (this.offset === this.chunk.length) {
          this.port.postMessage(this.chunk.buffer, [this.chunk.buffer]);
          this.chunk = new Float32Array(640); this.offset = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('agi-call-capture', CallCapture);
