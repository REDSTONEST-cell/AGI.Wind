"""Stream a bundled public model fixture in real time; no microphone or chat writes."""
import asyncio
import json
from pathlib import Path
import time
import wave

import numpy as np
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]


async def main():
    config = json.loads((ROOT/'config/asr.local.json').read_text(encoding='utf-8-sig'))
    sample = Path(config['model_dir'])/'test_wavs/0.wav'
    with wave.open(str(sample)) as wav:
        assert wav.getsampwidth() == 2 and wav.getnchannels() == 1
        rate = wav.getframerate()
        audio = np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').astype(np.float32)/32768
    if rate != 16000:
        audio = np.interp(np.arange(int(len(audio)*16000/rate))*rate/16000, np.arange(len(audio)),audio).astype(np.float32)
    duration = len(audio)/16000
    token = (ROOT/'data/asr-token.txt').read_text().strip()
    started = time.perf_counter()
    events = []
    async with connect('ws://127.0.0.1:8139/stream',additional_headers={'Authorization':'Bearer '+token},proxy=None) as ws:
        assert json.loads(await ws.recv())['type'] == 'ready'
        async def receive():
            async for raw in ws:
                value = json.loads(raw)
                value['wall_ms'] = round((time.perf_counter()-started)*1000)
                events.append(value)
                if value['type'] == 'final': return
        task = asyncio.create_task(receive())
        padded = np.concatenate((audio,np.zeros(16000,dtype=np.float32)))
        audio_started = time.perf_counter()
        for offset in range(0,len(padded),640):
            await ws.send(padded[offset:offset+640].tobytes())
            await asyncio.sleep(max(0,audio_started+(offset+640)/16000-time.perf_counter()))
        await asyncio.wait_for(task, timeout=5)
        # Reset (mute/unmute) must not restart utterance numbering within a call.
        await ws.send(json.dumps({'type':'reset'}))
        task = asyncio.create_task(receive())
        for offset in range(0,len(padded),640):
            await ws.send(padded[offset:offset+640].tobytes())
        await asyncio.wait_for(task, timeout=5)
        assert [event['utterance'] for event in events if event['type']=='final'] == [1,2]
    result = {'sample_seconds':round(duration,2), 'events':events}
    (ROOT/'data/asr-benchmark.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


asyncio.run(main())
