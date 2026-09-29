const BASE = 'http://127.0.0.1:18777';
const sessionFor = tabId => `tab-${tabId}`;

async function request(path, data) {
  const tokenResponse = await fetch(`${BASE}/api/token`, {cache: 'no-store'});
  if (!tokenResponse.ok) throw new Error('로컬 재생 서버를 실행해 주세요.');
  const {token} = await tokenResponse.json();
  const response = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Local-Token': token},
    body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '재생 서버 요청 실패');
  if (path !== '/api/start') return result;
  for (let attempt = 0; attempt < 40; attempt++) {
    const statusResponse = await fetch(`${BASE}/api/status?session=${encodeURIComponent(data.session)}`, {cache: 'no-store'});
    const status = await statusResponse.json();
    if (status.ready) return status;
    if (!status.running) throw new Error(status.error || 'SOOP 영상 수신이 종료됐습니다.');
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error('영상 준비에 시간이 걸립니다. 탭을 새로고침해 주세요.');
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const tabId = sender.tab?.id;
  if (tabId == null) return;
  const session = sessionFor(tabId);
  const task = message.type === 'start'
    ? request('/api/start', {session, url: message.url, quality: message.quality || '1080p'})
    : message.type === 'stop'
      ? request('/api/stop', {session})
      : message.type === 'ping'
        ? request('/api/ping', {session})
      : message.type === 'telemetry'
        ? request('/api/telemetry', {session, ...message.sample})
      : message.type === 'fetch'
        ? fetchMedia(message.url, session)
        : Promise.reject(new Error('알 수 없는 요청'));
  task.then(result => sendResponse({ok: true, result}))
      .catch(error => sendResponse({ok: false, error: error.message}));
  return true;
});

async function fetchMedia(url, session) {
  const expected = new RegExp(`^${BASE}/sessions/${session}/streams/[a-f0-9]{12}/(?:index\\.m3u8|segment-\\d{8}\\.ts)$`);
  if (!expected.test(url)) throw new Error('허용되지 않은 영상 주소');
  const response = await fetch(url, {cache: 'no-store'});
  if (!response.ok) throw new Error(`영상 조각 HTTP ${response.status}`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  let binary = '';
  for (let i = 0; i < bytes.length; i += 32768) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 32768));
  }
  return {base64: btoa(binary), length: bytes.length};
}

chrome.tabs.onRemoved.addListener(tabId => {
  request('/api/stop', {session: sessionFor(tabId)}).catch(() => {});
});
