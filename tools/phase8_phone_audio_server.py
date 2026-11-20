#!/usr/bin/env python3
"""Control exploratory WAV playback on an ADB-connected phone browser.

Use ``adb reverse tcp:8765 tcp:8765`` and open http://127.0.0.1:8765/ on
the phone. The workstation sends POST /play with a WAV filename. The phone
reports readiness, start and end to /ack. Localhost only; no cloud service.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlparse


PAGE = '''<!doctype html><html lang="es"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fase 8: altavoz</title><style>body{font:20px sans-serif;margin:8vw}button{font:22px sans-serif;padding:30px;width:100%}</style>
<button id="start">Habilitar audio</button><p id="status">Esperando activación</p><script>
let audio=null, current=null, last=0;
async function ack(id,status,detail='') {await fetch('/ack',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({id,status,detail,phone_time_ms:Date.now(),audio_state:audio?.state||'none'})});}
document.getElementById('start').onclick=async()=>{
 try {audio=new(window.AudioContext||window.webkitAudioContext)(); await audio.resume();
  const b=audio.createBuffer(1,1,audio.sampleRate),s=audio.createBufferSource();
  s.buffer=b;s.connect(audio.destination);s.start();
  document.getElementById('status').textContent='Listo: '+audio.sampleRate+' Hz';
  await ack(0,'ready','sample_rate_hz='+audio.sampleRate);
 } catch(e) {document.getElementById('status').textContent='Error: '+e;}
};
async function poll() {
 try {const c=await(await fetch('/poll?after='+last,{cache:'no-store'})).json();
  if(c.id<=last)return;last=c.id;
  if(!audio||audio.state!=='running'){await ack(c.id,'blocked','audio context inactive');return;}
  if(current){current.stop();current=null;}
  const r=await fetch('/wav/'+c.file,{cache:'no-store'});
  if(!r.ok)throw Error('WAV HTTP '+r.status);
  const b=await audio.decodeAudioData(await r.arrayBuffer());
  const s=audio.createBufferSource();s.buffer=b;s.connect(audio.destination);
  s.onended=()=>{if(current===s){current=null;ack(c.id,'ended');}};
  current=s;s.start();document.getElementById('status').textContent='Reproduciendo '+c.file;
  await ack(c.id,'started','duration_seconds='+b.duration);
 } catch(e) {document.getElementById('status').textContent='Error: '+e;}
}
setInterval(poll,200);
</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stimuli', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--initial-id', type=int, default=0,
                        help='Resume command IDs after a server restart when the browser page remains open')
    args = parser.parse_args()
    if args.initial_id < 0:
        raise ValueError('--initial-id must be non-negative')
    files = {p.name: p for p in args.stimuli.resolve().glob('*.wav') if p.is_file()}
    if not files:
        raise FileNotFoundError('No WAV files found')
    state = {'id': args.initial_id, 'file': None, 'ready': False, 'last_ack': None,
             'phone_seen_monotonic_ns': None}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def send(self, code, data, content_type):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, code, value):
            self.send(code, json.dumps(value).encode(), 'application/json')

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == '/':
                self.send(200, PAGE.encode(), 'text/html; charset=utf-8')
            elif url.path == '/poll':
                with lock:
                    state['phone_seen_monotonic_ns'] = time.monotonic_ns()
                    command = {'id': state['id'], 'file': state['file']}
                self.send_json(200, command)
            elif url.path == '/state':
                with lock:
                    snapshot = dict(state)
                self.send_json(200, snapshot)
            elif url.path.startswith('/wav/'):
                path = files.get(url.path[len('/wav/'):])
                if path is None:
                    self.send_json(404, {'error': 'unknown WAV'})
                else:
                    self.send(200, path.read_bytes(), 'audio/wav')
            else:
                self.send_json(404, {'error': 'not found'})

        def do_POST(self):
            size = int(self.headers.get('Content-Length', '0'))
            if size < 1 or size > 1024:
                self.send_json(413, {'error': 'invalid body size'})
                return
            try:
                payload = json.loads(self.rfile.read(size))
            except (ValueError, UnicodeDecodeError):
                self.send_json(400, {'error': 'invalid JSON'})
                return
            if self.path == '/play':
                name = payload.get('file')
                if name not in files:
                    self.send_json(400, {'error': 'unknown WAV'})
                    return
                with lock:
                    if not state['ready']:
                        self.send_json(409, {'error': 'phone audio not ready'})
                        return
                    state['id'] += 1
                    state['file'] = name
                    result = {'id': state['id'], 'file': name}
                self.send_json(200, result)
            elif self.path == '/ack':
                with lock:
                    state['last_ack'] = {'id': payload.get('id'), 'status': payload.get('status'),
                                         'detail': payload.get('detail'),
                                         'phone_time_ms': payload.get('phone_time_ms'),
                                         'pc_monotonic_ns': time.monotonic_ns()}
                    if payload.get('status') == 'ready':
                        state['ready'] = True
                self.send_json(200, {'ok': True})
            else:
                self.send_json(404, {'error': 'not found'})

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print('PHONE_AUDIO_URL=http://127.0.0.1:{}/'.format(args.port), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
