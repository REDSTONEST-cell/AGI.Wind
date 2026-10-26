"""Loopback-only streaming ASR sidecar. Audio and interim transcripts stay in RAM."""
from __future__ import annotations
import argparse
import asyncio
import hmac
import json
from pathlib import Path
import time
import numpy as np
import sherpa_onnx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
RATE = 16000
def create_denoiser(config):
    model = config.get('denoiser_model')
    if not config.get('denoise') or not model or not Path(model).is_file():
        return None
    denoiser_config = sherpa_onnx.OnlineSpeechDenoiserConfig(
        model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
            gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(model=str(model)),
            num_threads=1, debug=False, provider='cpu'))
    if not denoiser_config.validate():
        raise RuntimeError('invalid denoiser config')
    return sherpa_onnx.OnlineSpeechDenoiser(denoiser_config)
def create_offline(config):
    spec = config.get('offline')
    if not spec:
        return None
    model, tokens = spec.get('model'), spec.get('tokens')
    if not model or not tokens or not Path(model).is_file() or not Path(tokens).is_file():
        raise RuntimeError('offline recognizer files missing')
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model), tokens=str(tokens), num_threads=int(spec.get('threads', 2)),
        use_itn=True, debug=False, provider='cpu')
class RecognizerSession:
    def __init__(self, recognizer, vad_config, denoiser=None, offline=None, corrections=None, utterance=0,
                 utterance_silence=0.9, silence_seconds=0.4):
        self.recognizer = recognizer
        self.vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=30)
        self.stream = recognizer.create_stream()
        self.buffer = np.empty(0, dtype=np.float32)
        self.audio = np.empty(0, dtype=np.float32)
        self.previous = ''
        self.speaking = False
        self.samples = 0
        self.utterance = utterance
        self.denoiser = denoiser
        self.offline = offline
        self.corrections = corrections or {}
        self.paused = False
        self.paused_samples = 0
        self.utterance_silence = max(0.3, float(utterance_silence)) * RATE
        self.vad_silence = max(0.1, float(silence_seconds)) * RATE
    def reset(self):
        self.stream = self.recognizer.create_stream()
        self.previous = ''
        self.samples = 0
        self.audio = np.empty(0, dtype=np.float32)
        self.buffer = np.empty(0, dtype=np.float32)
        self.paused = False
        self.paused_samples = 0
    def reset_keep_tail(self, seconds=0.6):
        # Keep the last fraction of a second so the next utterance includes the
        # onset consonant; otherwise the first syllable is often clipped.
        tail = self.audio[-int(RATE * seconds):].copy() if len(self.audio) else np.empty(0, dtype=np.float32)
        self.reset()
        self.audio = tail
    def enhance(self, audio):
        if self.denoiser is None:
            return audio
        shift = int(self.denoiser.frame_shift_in_samples)
        output = []
        for start in range(0, len(audio), shift):
            result = self.denoiser(audio[start:start + shift], RATE)
            samples = getattr(result, 'samples', None)
            if samples is not None and len(samples):
                output.append(np.asarray(samples, dtype=np.float32))
        return np.concatenate(output) if output else np.empty(0, dtype=np.float32)
    def transcribe(self, audio):
        stream = self.offline.create_stream()
        stream.accept_waveform(RATE, audio)
        self.offline.decode_stream(stream)
        return stream.result.text.strip()
    def feed(self, data: bytes):
        started = time.perf_counter()
        if not data or len(data) > 12800 or len(data) % 4:
            raise ValueError('invalid PCM frame')
        audio = np.frombuffer(data, dtype='<f4')
        if not np.isfinite(audio).all() or np.max(np.abs(audio)) > 1.01:
            raise ValueError('invalid PCM samples')
        audio = self.enhance(audio)
        if not len(audio):
            return []
        self.stream.accept_waveform(RATE, audio)
        self.samples += len(audio)
        self.audio = np.concatenate((self.audio, audio))[-RATE * 25:]
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        events = []
        self.buffer = np.concatenate((self.buffer, audio))
        ended = False
        while len(self.buffer) >= 512:
            self.vad.accept_waveform(self.buffer[:512])
            self.buffer = self.buffer[512:]
            detected = self.vad.is_speech_detected()
            if detected and not self.speaking:
                self.speaking = True
                self.paused = False
                events.append({'type':'speech_start'})
            elif detected and self.paused:
                # The user resumed inside the same utterance; keep one stream.
                self.paused = False
                events.append({'type':'speech_start'})
            while not self.vad.empty():
                self.vad.pop()
                ended = True
        text = self.recognizer.get_result(self.stream).strip()
        if text and text != self.previous:
            self.previous = text
            events.append({'type':'partial', 'text':text})
        if ended and self.speaking and not self.paused:
            # Short pause: only hint the client; the final transcript waits for a
            # longer silence so short fragments are never decoded alone.
            self.paused = True
            self.paused_samples = max(0, self.samples - int(self.vad_silence))
            events.append({'type':'speech_end'})
        long_silence = self.paused and (self.samples - self.paused_samples >= self.utterance_silence)
        too_long = self.speaking and self.samples >= RATE * 20
        if (long_silence or too_long) and self.speaking:
            self.stream.accept_waveform(RATE, np.zeros(4800, dtype=np.float32))
            self.stream.input_finished()
            while self.recognizer.is_ready(self.stream):
                self.recognizer.decode_stream(self.stream)
            text = self.recognizer.get_result(self.stream).strip()
            if self.offline is not None and len(self.audio):
                try:
                    final_text = self.transcribe(self.audio)
                except Exception as error:
                    print('ASR_OFFLINE_FAILED', type(error).__name__, flush=True)
                    final_text = ''
                if final_text:
                    text = final_text
                    source = 'sensevoice'
                else:
                    source = 'streaming'
            else:
                source = 'streaming'
            for wrong, right in getattr(self, 'corrections', {}).items():
                if wrong in text:
                    text = text.replace(wrong, right)
            self.utterance += 1
            if text:
                print('ASR_FINAL', source, self.utterance, round(len(self.audio) / RATE, 2), json.dumps(text, ensure_ascii=False), flush=True)
                events.append({'type':'final', 'text':text, 'utterance':self.utterance})
            self.speaking = False
            self.reset_keep_tail(0.6)
        elif not self.speaking and self.samples > RATE * 4:
            self.reset_keep_tail(0.6)
        for event in events:
            event['decode_ms'] = round((time.perf_counter() - started) * 1000, 1)
        return events
def make_app(root: Path):
    config = json.loads((root / 'config/asr.local.json').read_text(encoding='utf-8-sig'))
    token = (root / 'data/asr-token.txt').read_text(encoding='utf-8').strip()
    model = Path(config['model_dir'])
    recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(model / 'tokens.txt'), encoder=str(model / 'encoder.int8.onnx'),
        decoder=str(model / 'decoder.onnx'), joiner=str(model / 'joiner.int8.onnx'),
        num_threads=int(config.get('threads', 2)), sample_rate=RATE, feature_dim=80,
        decoding_method='greedy_search', provider='cpu',
    )
    vad_config = sherpa_onnx.VadModelConfig()
    vad_config.silero_vad.model = config['vad_model']
    vad_config.silero_vad.min_silence_duration = float(config.get('silence_seconds', 0.4))
    vad_config.silero_vad.min_speech_duration = 0.1
    vad_config.silero_vad.threshold = 0.5
    vad_config.sample_rate = RATE
    denoiser = create_denoiser(config)
    offline = create_offline(config)
    corrections = config.get('corrections') or {}
    utterance_silence = float(config.get('utterance_silence', 0.9))
    silence_seconds = float(config.get('silence_seconds', 0.4))
    warmup = RecognizerSession(recognizer, vad_config, denoiser=denoiser, offline=offline, corrections=corrections,
                               utterance_silence=utterance_silence, silence_seconds=silence_seconds)
    warmup.feed(np.zeros(1280, dtype=np.float32).tobytes())
    if offline is not None:
        try:
            warmup.transcribe(np.zeros(RATE // 2, dtype=np.float32))
        except Exception:
            pass
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    active = set()
    decode_lock = asyncio.Lock()
    @app.get('/health')
    async def health():
        return {'status':'ready', 'model':model.name, 'streaming':True, 'device':'cpu', 'sample_rate':RATE,
                'denoise':denoiser is not None, 'final_model':('sensevoice' if offline is not None else 'streaming')}
    @app.websocket('/stream')
    async def stream(ws: WebSocket):
        supplied = ws.headers.get('authorization', '')
        if ws.headers.get('origin') or not hmac.compare_digest(supplied, 'Bearer ' + token) or len(active) >= 2:
            await ws.close(code=1008)
            return
        await ws.accept()
        active.add(ws)
        session = RecognizerSession(recognizer, vad_config, denoiser=denoiser, offline=offline, corrections=corrections,
                                    utterance_silence=utterance_silence, silence_seconds=silence_seconds)
        try:
            await ws.send_json({'type':'ready', 'sample_rate':RATE})
            while True:
                message = await asyncio.wait_for(ws.receive(), timeout=30)
                if message['type'] == 'websocket.disconnect':
                    break
                if message.get('bytes') is not None:
                    async with decode_lock:
                        events = await asyncio.to_thread(session.feed, message['bytes'])
                    for event in events:
                        await ws.send_json(event)
                elif message.get('text'):
                    control = json.loads(message['text'])
                    if control.get('type') == 'reset':
                        session = RecognizerSession(recognizer, vad_config, denoiser=denoiser, offline=offline, corrections=corrections,
                                                    utterance=session.utterance, utterance_silence=utterance_silence,
                                                    silence_seconds=silence_seconds)
                    elif control.get('type') != 'ping':
                        raise ValueError('invalid control')
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except (ValueError, KeyError, TypeError):
            await ws.close(code=1008)
        finally:
            active.discard(ws)
    return app
if __name__ == '__main__':
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    uvicorn.run(make_app(args.root), host='127.0.0.1', port=8139, ws_max_size=16384, ws_max_queue=8, log_level='warning')
