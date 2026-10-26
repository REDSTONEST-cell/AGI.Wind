"""Verify deployed call transport with public fixture audio; no microphone/chat writes."""
import asyncio
import json
from pathlib import Path
import struct
import time
import wave

import httpx
import numpy as np
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]


async def main():
    token = (ROOT/'data/access-token.txt').read_text().strip()
    async with httpx.AsyncClient(base_url='http://127.0.0.1:18440',trust_env=False,headers={'Authorization':'Bearer '+token},timeout=8) as client:
        status = (await client.get('/api/v1/call/status')).json()
        assert status['available'], status
        companions = (await client.get('/api/v1/companions')).json()
        conversations = (await client.get('/api/v1/conversations',params={'companion_id':companions[0]['id']})).json()
        assert conversations, 'No active conversation available; no conversation created by this smoke test'
        issued = (await client.post('/api/v1/call/ticket',json={'conversation_id':conversations[0]['id']})).json()
    result = {'services_ready':True}
    async with connect('ws://127.0.0.1:18440/api/v1/call/stream',proxy=None,max_size=2**20) as ws:
        await ws.send(json.dumps({'ticket':issued['ticket']}))
        assert json.loads(await ws.recv())['type']=='ready'
        await ws.send(json.dumps({'type':'cancel','epoch':1}))
        assert json.loads(await ws.recv())['type']=='speech_reset'
        started = time.perf_counter()
        await ws.send(json.dumps({'type':'speak','epoch':1,'text':'哥哥，我听到了。'}))
        while True:
            message = await asyncio.wait_for(ws.recv(),timeout=35)
            if isinstance(message,bytes):
                assert struct.unpack('<I',message[:4])[0]==1
                result['tts_first_audio_ms']=round((time.perf_counter()-started)*1000)
                assert len(message)>4
                break
            value=json.loads(message)
            assert value.get('type')!='error', value
        cancelled = time.perf_counter()
        await ws.send(json.dumps({'type':'cancel','epoch':2}))
        while True:
            message = await asyncio.wait_for(ws.recv(),timeout=5)
            if isinstance(message,str) and json.loads(message).get('type')=='speech_reset':
                result['cancel_ack_ms']=round((time.perf_counter()-cancelled)*1000); break
        config=json.loads((ROOT/'config/asr.local.json').read_text(encoding='utf-8-sig'))
        with wave.open(str(Path(config['model_dir'])/'test_wavs/0.wav')) as wav:
            assert wav.getframerate()==16000 and wav.getnchannels()==1 and wav.getsampwidth()==2
            audio=np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').astype(np.float32)/32768
        async def listen():
            while True:
                message=await ws.recv()
                if isinstance(message,bytes): continue
                event=json.loads(message)
                assert event.get('type')!='error', event
                if event['type']=='final': return event['text']
        task=asyncio.create_task(listen())
        audio=np.concatenate((audio,np.zeros(16000,dtype=np.float32)))
        started=time.perf_counter()
        for offset in range(0,len(audio),640):
            await ws.send(audio[offset:offset+640].tobytes())
            await asyncio.sleep(max(0,started+(offset+640)/16000-time.perf_counter()))
        result['transcript']=await asyncio.wait_for(task,timeout=5)
        assert len(result['transcript'])>10
        result['gateway_asr_streaming']=True
    (ROOT/'data/call-smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=True))


asyncio.run(main())
