import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.VOICE_UI_PORT || 8140);
const upstreamHost = process.env.BREEZE_HOST || '127.0.0.1';
const upstreamPort = Number(process.env.BREEZE_PORT || 8137);

const types = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8' };

function staticFile(response, name) {
  const file = path.join(root, name);
  if (!fs.existsSync(file)) {
    response.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    response.end('not found');
    return;
  }
  response.writeHead(200, {
    'content-type': types[path.extname(file)] || 'application/octet-stream',
    'cache-control': 'no-store',
  });
  fs.createReadStream(file).pipe(response);
}

function proxy(request, response) {
  // The UI can use the HTTP streaming endpoint; keeping ws_port at 0 avoids a second
  // cross-port WebSocket connection for the translated page.
  if (request.url === '/health') {
    response.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
    response.end(JSON.stringify({ status: 'ok', sample_rate: 24000, ws_port: 0 }));
    return;
  }
  const headers = { ...request.headers, host: `${upstreamHost}:${upstreamPort}` };
  const upstream = http.request({ host: upstreamHost, port: upstreamPort, path: request.url, method: request.method, headers }, (reply) => {
    response.writeHead(reply.statusCode || 502, reply.headers);
    reply.pipe(response);
  });
  upstream.on('error', (error) => {
    if (!response.headersSent) {
      response.writeHead(502, { 'content-type': 'application/json; charset=utf-8' });
    }
    response.end(JSON.stringify({ error: 'TTS 服务不可用：' + error.message }));
  });
  request.pipe(upstream);
}

const server = http.createServer((request, response) => {
  const pathname = new URL(request.url, 'http://localhost').pathname;
  if (request.method === 'GET' && (pathname === '/' || pathname === '/index.html')) return staticFile(response, 'index.html');
  if (request.method === 'GET' && pathname === '/app.js') return staticFile(response, 'app.js');
  if (request.method === 'GET' && pathname === '/style.css') return staticFile(response, 'style.css');
  if (pathname === '/health' || pathname.startsWith('/v1/')) return proxy(request, response);
  response.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
  response.end('not found');
});

server.listen(port, '127.0.0.1', () => {
  console.log(`Breeze 中文界面: http://127.0.0.1:${port}/  ->  ${upstreamHost}:${upstreamPort}`);
});
