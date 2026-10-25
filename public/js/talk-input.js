const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const damp = (current, target, rate, dt) => target + (current - target) * Math.exp(-rate * dt);

const WORKLET_SOURCE = `
class KineticVoiceMeter extends AudioWorkletProcessor {
  constructor() {
    super();
    this.total = 0;
    this.sumSquares = 0;
    this.chunkSize = Math.max(512, Math.floor(sampleRate / 30));
  }

  process(inputs, outputs) {
    const channels = inputs[0] || [];
    for (const channel of channels) {
      for (let i = 0; i < channel.length; i++) {
        const sample = channel[i];
        this.sumSquares += sample * sample;
        this.total += 1;
      }
    }
    if (this.total >= this.chunkSize) {
      this.port.postMessage(Math.sqrt(this.sumSquares / this.total));
      this.total = 0;
      this.sumSquares = 0;
    }
    for (const output of outputs) {
      for (const channel of output) channel.fill(0);
    }
    return true;
  }
}
registerProcessor('kinetic-voice-meter', KineticVoiceMeter);
`;

export class TalkInput {
  constructor({ onSample, onStatus }) {
    this.onSample = onSample;
    this.onStatus = onStatus;
    this.stream = null;
    this.context = null;
    this.source = null;
    this.processor = null;
    this.muteGain = null;
    this.deviceId = '';
    this.lastSampleAt = 0;
    this.envelope = 0;
    this.config = { enabled: true, sensitivity: 3.2, threshold: 0.05, smoothing: 0.1 };
    this.error = '';
    this.resumeAudio = () => {
      if (this.active && this.context?.state === 'suspended') {
        this.context.resume().catch(() => {});
      }
    };
  }

  get active() {
    return Boolean(this.stream);
  }

  setConfig(config) {
    this.config = config;
  }

  async selectDevice(deviceId) {
    const nextDeviceId = deviceId || '';
    if (nextDeviceId === this.deviceId) return;
    const restart = this.active;
    this.deviceId = nextDeviceId;
    if (!restart) return;
    this.stop();
    await this.start();
  }

  status() {
    return { audio: this.active, error: this.error };
  }

  async start() {
    if (this.active) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('Microphone access requires localhost or HTTPS.');
    }
    try {
      const audio = {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: false,
        ...(this.deviceId ? { deviceId: { exact: this.deviceId } } : {})
      };
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({ audio });
      } catch (error) {
        if (!this.deviceId || !['OverconstrainedError', 'NotFoundError'].includes(error.name)) throw error;
        this.deviceId = '';
        delete audio.deviceId;
        this.stream = await navigator.mediaDevices.getUserMedia({ audio });
      }

      const Context = window.AudioContext || window.webkitAudioContext;
      this.context = new Context();
      await this.context.resume();
      this.source = this.context.createMediaStreamSource(this.stream);
      this.muteGain = this.context.createGain();
      this.muteGain.gain.value = 0;
      this.source.connect(await this.createMeterNode());
      this.processor.connect(this.muteGain);
      this.muteGain.connect(this.context.destination);
      this.error = '';
      this.envelope = 0;
      this.lastSampleAt = 0;
      document.addEventListener('visibilitychange', this.resumeAudio);
      window.addEventListener('focus', this.resumeAudio);
      this.emitStatus();
    } catch (error) {
      this.stop();
      this.error = error?.name === 'NotAllowedError'
        ? 'Allow microphone access in this app, then try again.'
        : `Microphone could not start: ${error?.message || 'unknown error'}`;
      this.emitStatus();
      throw new Error(this.error);
    }
  }

  async createMeterNode() {
    if (this.context.audioWorklet && typeof AudioWorkletNode !== 'undefined') {
      const sourceUrl = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: 'application/javascript' }));
      try {
        await this.context.audioWorklet.addModule(sourceUrl);
      } finally {
        URL.revokeObjectURL(sourceUrl);
      }
      this.processor = new AudioWorkletNode(this.context, 'kinetic-voice-meter', {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1]
      });
      this.processor.port.onmessage = (event) => this.processRms(Number(event.data));
      return this.processor;
    }

    const processor = this.context.createScriptProcessor(2048, 1, 1);
    processor.onaudioprocess = (event) => {
      const samples = event.inputBuffer.getChannelData(0);
      let sum = 0;
      for (const sample of samples) sum += sample * sample;
      this.processRms(Math.sqrt(sum / samples.length));
      event.outputBuffer.getChannelData(0).fill(0);
    };
    this.processor = processor;
    return processor;
  }

  processRms(rms) {
    if (!this.active || !Number.isFinite(rms)) return;
    const now = performance.now();
    const dt = this.lastSampleAt ? clamp((now - this.lastSampleAt) / 1000, 1 / 120, 0.15) : 1 / 30;
    this.lastSampleAt = now;
    const boosted = clamp(rms * this.config.sensitivity * 5.2, 0, 1);
    const threshold = this.config.threshold;
    const target = !this.config.enabled || boosted <= threshold
      ? 0
      : clamp((boosted - threshold) / Math.max(0.08, 0.55 - threshold), 0, 1);
    const smooth = this.config.smoothing;
    const rate = target > this.envelope ? 34 + (1 - smooth) * 46 : 18 + (1 - smooth) * 28;
    this.envelope = damp(this.envelope, target, rate, dt);
    this.onSample?.({ talk: this.envelope, level: boosted, audio: true });
  }

  stop() {
    document.removeEventListener('visibilitychange', this.resumeAudio);
    window.removeEventListener('focus', this.resumeAudio);
    this.processor?.port?.close();
    this.processor?.disconnect();
    this.source?.disconnect();
    this.muteGain?.disconnect();
    this.processor = null;
    this.source = null;
    this.muteGain = null;
    for (const track of this.stream?.getTracks() || []) track.stop();
    this.stream = null;
    this.envelope = 0;
    if (this.context && this.context.state !== 'closed') this.context.close().catch(() => {});
    this.context = null;
    this.lastSampleAt = 0;
    this.onSample?.({ talk: 0, level: 0, audio: false });
    this.emitStatus();
  }

  emitStatus() {
    this.onStatus?.(this.status());
  }
}
