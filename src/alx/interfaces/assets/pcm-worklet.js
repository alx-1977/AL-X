class AlxPcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.targetRate = options.processorOptions.targetSampleRate;
    this.position = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel || channel.length === 0) return true;
    const ratio = sampleRate / this.targetRate;
    const samples = [];
    while (this.position < channel.length) {
      const index = Math.floor(this.position);
      const value = Math.max(-1, Math.min(1, channel[index] ?? 0));
      samples.push(value < 0 ? value * 0x8000 : value * 0x7fff);
      this.position += ratio;
    }
    this.position -= channel.length;
    const pcm = Int16Array.from(samples);
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
    return true;
  }
}

registerProcessor("alx-pcm-capture", AlxPcmCapture);
