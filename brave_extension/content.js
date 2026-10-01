(() => {
  const BASE = 'http://127.0.0.1:18777';
  const VOLUME_KEY = 'soopUbuntuVolume';
  const MUTED_KEY = 'soopUbuntuMuted';
  const broadcastPath = /^\/([A-Za-z0-9_]+)\/(\d+)\/?$/;
  const nativeQuality = {auto: 'auto_quality', '540p': 'normal_quality', '360p': 'low_quality'};
  let quality = sessionStorage.getItem('soop-ubuntu-quality') || '1080p';
  if (!['1080p', '720p', '540p', '360p', 'auto'].includes(quality)) quality = '1080p';
  let activeUrl = '', sourceVideo = null, originalState = null, box = null, highVideo = null, pendingAuto720 = false,
    pendingFallback540 = false,
    hls = null, telemetryTimer = null, toolbar = null, chooser = null, barChooser = null, busy = false, retryAfter = 0,
    officialApplied = '', manualStoppedUrl = '', nativeLiveTimers = [];
  let preferredVolume = 0.3, preferredMuted = false, volumeTouched = false;

  function applySavedVolume() {
    if (!highVideo) return;
    highVideo.volume = preferredVolume;
    highVideo.muted = preferredMuted;
  }

  chrome.storage.local.get({[VOLUME_KEY]: 0.3, [MUTED_KEY]: false}).then(settings => {
    if (volumeTouched) return;
    if (Number.isFinite(settings[VOLUME_KEY])) {
      preferredVolume = Math.min(1, Math.max(0, settings[VOLUME_KEY]));
    }
    preferredMuted = settings[MUTED_KEY] === true;
    applySavedVolume();
  }).catch(() => {});

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    volumeTouched = true;
    if (VOLUME_KEY in changes && Number.isFinite(changes[VOLUME_KEY].newValue)) {
      preferredVolume = Math.min(1, Math.max(0, changes[VOLUME_KEY].newValue));
    }
    if (MUTED_KEY in changes) preferredMuted = changes[MUTED_KEY].newValue === true;
  });

  function saveVolume() {
    if (!highVideo) return;
    volumeTouched = true;
    preferredVolume = highVideo.volume;
    preferredMuted = highVideo.muted;
    chrome.storage.local.set({[VOLUME_KEY]: preferredVolume, [MUTED_KEY]: preferredMuted}).catch(() => {});
  }

  function targetVideo() {
    return [...document.querySelectorAll('video')]
      .filter(video => !box?.contains(video))
      .sort((a, b) => b.getBoundingClientRect().width * b.getBoundingClientRect().height - a.getBoundingClientRect().width * a.getBoundingClientRect().height)[0];
  }

  function place() {
    const candidate = targetVideo();
    const candidateRect = candidate?.getBoundingClientRect();
    if (box && candidate && candidateRect.width > 200 && candidateRect.height > 120) sourceVideo = candidate;
    const nativeVideo = sourceVideo || candidate;
    const rect = nativeVideo?.getBoundingClientRect();
    const hasVideoArea = rect && rect.width > 200 && rect.height > 120;
    if (box) {
      if (document.fullscreenElement === box) {
        for (const [key, value] of Object.entries({left: '0px', top: '0px', width: '100%', height: '100%', display: 'block'})) {
          if (box.style[key] !== value) box.style[key] = value;
        }
      } else if (hasVideoArea) {
        for (const [key, value] of Object.entries({
          left: `${rect.left}px`, top: `${rect.top}px`,
          width: `${rect.width}px`, height: `${rect.height}px`,
          display: 'block'
        })) {
          if (box.style[key] !== value) box.style[key] = value;
        }
      }
      if (nativeVideo?.isConnected) {
        nativeVideo.muted = true;
        if (!nativeVideo.paused) nativeVideo.pause();
      }
    }
    if (toolbar) {
      toolbar.style.left = hasVideoArea ? `${Math.max(0, rect.right - 158)}px` : '';
      toolbar.style.right = hasVideoArea ? '' : '18px';
      toolbar.style.top = hasVideoArea ? `${Math.max(0, rect.top + 10)}px` : '80px';
      toolbar.style.display = box && document.fullscreenElement === box ? 'none' : 'block';
    }
  }

  function cancelNativeLiveSync() {
    for (const timer of nativeLiveTimers) clearTimeout(timer);
    nativeLiveTimers = [];
  }

  function syncNativeLive(initialVideo) {
    const path = location.pathname;
    const started = performance.now();
    const catchUp = () => {
      if (box || location.pathname !== path || !(quality in nativeQuality)) return;
      const video = targetVideo() || initialVideo;
      if (!video?.isConnected) return;
      const ranges = video.seekable.length ? video.seekable : video.buffered;
      if (!ranges.length) return;
      const last = ranges.length - 1;
      const liveTime = Math.max(ranges.start(last), ranges.end(last) - 1);
      if (Number.isFinite(liveTime) && liveTime - video.currentTime > 2) {
        try { video.currentTime = liveTime; } catch (error) { console.debug('SOOP live seek:', error); }
      }
    };
    // SOOP may replace its video or refresh its live range after changing quality.
    for (const delay of [0, 300, 900, 1800, 3200]) {
      nativeLiveTimers.push(setTimeout(() => {
        if (performance.now() - started < 5000) catchUp();
      }, delay));
    }
  }

  function clear(restore = false) {
    cancelNativeLiveSync();
    const nativeVideo = targetVideo() || sourceVideo;
    if (telemetryTimer) { clearInterval(telemetryTimer); telemetryTimer = null; }
    if (box && document.fullscreenElement === box) document.exitFullscreen().catch(() => {});
    if (hls) { hls.destroy(); hls = null; }
    if (highVideo) { highVideo.pause(); highVideo.removeAttribute('src'); highVideo.load(); }
    box?.remove();
    box = highVideo = sourceVideo = barChooser = null;
    if (restore && nativeVideo?.isConnected) {
      nativeVideo.muted = originalState?.muted ?? false;
      nativeVideo.volume = originalState?.volume ?? 1;
      nativeVideo.play().catch(() => {});
      syncNativeLive(nativeVideo);
    }
    if (restore) originalState = null;
  }

  function message(type, url, selectedQuality, sample) {
    return new Promise((resolve, reject) => chrome.runtime.sendMessage({type, url, quality: selectedQuality, sample}, reply => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      if (!reply?.ok) return reject(new Error(reply?.error || '로컬 수신기 오류'));
      resolve(reply.result);
    }));
  }

  function ensureToolbar() {
    if (toolbar?.isConnected) return;
    toolbar = chooser = null;
    toolbar = document.createElement('div');
    toolbar.id = 'soop-ubuntu-quality-toolbar';
    toolbar.style.cssText = 'position:fixed;z-index:2147483647;padding:4px 6px;border-radius:7px;background:#14231de8;color:#fff;font:12px system-ui;box-shadow:0 2px 10px #0008';
    chooser = document.createElement('select');
    chooser.setAttribute('aria-label', 'SOOP 화질 선택');
    chooser.style.cssText = 'width:138px;border:0;background:#14231d;color:#c5f4d9;padding:5px;font:13px system-ui;cursor:pointer';
    for (const [value, label] of [['1080p', '1080p 고화질'], ['720p', '720p 고화질'],
      ['540p', '540p SOOP'], ['360p', '360p SOOP'], ['auto', '자동 SOOP']]) {
      const option = document.createElement('option');
      option.value = value; option.textContent = label; chooser.append(option);
    }
    chooser.value = quality;
    chooser.addEventListener('change', () => changeQuality(chooser.value));
    toolbar.append(chooser);
    document.body.append(toolbar);
    place();
  }

  async function changeQuality(nextQuality) {
    if (busy) {
      chooser.value = quality;
      if (barChooser) barChooser.value = quality;
      return;
    }
    if (nextQuality === quality) return;
    busy = true;
    const restore = nextQuality in nativeQuality;
    try {
      if (activeUrl) await message('stop').catch(() => {});
      activeUrl = '';
      manualStoppedUrl = '';
      clear(restore);
      quality = nextQuality;
      chooser.value = quality;
      sessionStorage.setItem('soop-ubuntu-quality', quality);
      retryAfter = 0;
      officialApplied = '';
    } finally {
      busy = false;
      tick();
    }
  }

  function selectOfficialQuality(url) {
    const key = `${url}|${quality}`;
    if (officialApplied === key) return;
    if (!targetVideo()) return;
    const button = document.querySelector(`button.${nativeQuality[quality]}`);
    if (!button) {
      toolbar.title = `SOOP 플레이어의 화질 메뉴에서 ${quality}를 선택해 주세요.`;
      return;
    }
    document.querySelector('button.btn_quality_mode')?.click();
    setTimeout(() => button.click(), 80);
    toolbar.title = `${quality} · SOOP 기본 플레이어`;
    officialApplied = key;
  }

  class ExtensionLoader {
    constructor() {
      this.aborted = false;
      this.context = null;
      this.stats = {aborted: false, loaded: 0, total: 0, retry: 0, chunkCount: 0, bwEstimate: 0,
        loading: {start: 0, first: 0, end: 0}, parsing: {start: 0, end: 0},
        buffering: {start: 0, first: 0, end: 0}};
    }
    load(context, _config, callbacks) {
      const start = performance.now();
      this.context = context;
      this.stats.loading.start = start;
      chrome.runtime.sendMessage({type: 'fetch', url: context.url}, reply => {
        if (this.aborted) return;
        if (chrome.runtime.lastError || !reply?.ok) {
          console.warn('SOOP HD segment:', context.url, chrome.runtime.lastError?.message || reply?.error);
          callbacks.onError({code: 0, text: chrome.runtime.lastError?.message || reply?.error || '영상 수신 오류'}, context, null, this.stats);
          return;
        }
        const binary = atob(reply.result.base64);
        const bytes = Uint8Array.from(binary, char => char.charCodeAt(0));
        const data = context.responseType === 'arraybuffer' ? bytes.buffer : new TextDecoder().decode(bytes);
        const now = performance.now();
        this.stats.loading.first = this.stats.loading.end = now;
        this.stats.loaded = this.stats.total = bytes.length;
        this.stats.chunkCount = 1;
        this.stats.bwEstimate = bytes.length * 8000 / Math.max(1, now - start);
        callbacks.onSuccess({url: context.url, data, code: 200}, this.stats, context, null);
      });
    }
    abort() { this.aborted = this.stats.aborted = true; }
    destroy() { this.abort(); }
  }

  function show(player) {
    sourceVideo = targetVideo();
    if (!sourceVideo) throw new Error('SOOP 영상 영역을 찾지 못했습니다.');
    if (!originalState) originalState = {muted: sourceVideo.muted, volume: sourceVideo.volume};
    box = document.createElement('div');
    box.id = 'soop-ubuntu-hd-overlay';
    box.style.cssText = 'position:fixed;z-index:2147483646;background:#000;color:white;font:13px system-ui;overflow:hidden';
    highVideo = document.createElement('video');
    highVideo.controls = false;
    highVideo.playsInline = true;
    applySavedVolume();
    highVideo.style.cssText = 'width:100%;height:100%;object-fit:contain;background:#000';
    const badge = document.createElement('span');
    badge.style.cssText = 'position:absolute;left:12px;top:10px;padding:4px 8px;border-radius:5px;background:#122a1edb;color:#92efbc;pointer-events:none';
    badge.textContent = '고화질 연결 중…';
    highVideo.addEventListener('loadedmetadata', () => {
      badge.textContent = `원본 ${highVideo.videoWidth} × ${highVideo.videoHeight}`;
      if (quality === '1080p' && highVideo.videoWidth === 1024 && highVideo.videoHeight === 576) {
        pendingFallback540 = true;
        badge.textContent = '576p 원본 감지 · SOOP 540p로 전환 중…';
        return;
      }
      if (quality === '1080p' && highVideo.videoWidth < 1920) {
        if (highVideo.videoWidth === 1280 && highVideo.videoHeight === 720) {
          quality = '720p';
          sessionStorage.setItem('soop-ubuntu-quality', quality);
          chooser.value = barChooser.value = quality;
          badge.textContent = '원본 1280 × 720 · 720p';
        } else if (highVideo.videoWidth > 0) pendingAuto720 = true;
      }
    });
    highVideo.addEventListener('playing', () => { badge.style.opacity = '0.35'; });
    highVideo.addEventListener('error', () => {
      if (!manualStoppedUrl) badge.textContent = '재생 오류 · 다시 열어 주세요';
    });
    const bar = document.createElement('div');
    bar.setAttribute('role', 'toolbar');
    bar.setAttribute('aria-label', 'SOOP 고화질 재생 조작');
    bar.style.cssText = 'position:absolute;left:0;right:0;bottom:0;z-index:2;display:flex;align-items:center;gap:8px;min-height:44px;padding:5px 12px;box-sizing:border-box;background:linear-gradient(transparent,#000c 20%,#000e);color:#fff;font:13px system-ui';
    const addButton = (label, title, action) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = label;
      button.title = title;
      button.setAttribute('aria-label', title);
      button.style.cssText = 'border:0;background:transparent;color:#fff;cursor:pointer;font:19px system-ui;min-width:28px;height:30px;padding:0 3px';
      button.addEventListener('click', action);
      bar.append(button);
      return button;
    };
    const playButton = addButton('⏸', '일시정지', () => {
      if (manualStoppedUrl) {
        manualStoppedUrl = '';
        clear();
        retryAfter = 0;
        tick();
      } else if (highVideo.paused) {
        highVideo.play().catch(() => {});
      } else {
        highVideo.pause();
      }
    });
    const updatePlay = () => {
      if (!playButton.isConnected || !highVideo) return;
      const paused = manualStoppedUrl || highVideo.paused;
      playButton.textContent = paused ? '▶' : '⏸';
      playButton.title = paused ? '재생' : '일시정지';
      playButton.setAttribute('aria-label', playButton.title);
    };
    highVideo.addEventListener('play', updatePlay);
    highVideo.addEventListener('pause', updatePlay);
    addButton('■', '재생 중지', async () => {
      if (busy || manualStoppedUrl) return;
      busy = true;
      manualStoppedUrl = `https://play.sooplive.com${location.pathname}`;
      activeUrl = '';
      if (hls) { hls.destroy(); hls = null; }
      highVideo.pause();
      highVideo.removeAttribute('src');
      highVideo.load();
      badge.textContent = '재생 중지됨 · ▶를 눌러 다시 재생';
      badge.style.opacity = '1';
      updatePlay();
      try { await message('stop'); } catch (error) { console.warn('SOOP HD stop:', error.message); }
      finally { busy = false; }
    });
    const volumeButton = addButton('🔊', '음소거', () => {
      if (highVideo.muted || highVideo.volume === 0) {
        if (highVideo.volume === 0) highVideo.volume = preferredVolume > 0 ? preferredVolume : 0.3;
        highVideo.muted = false;
      } else highVideo.muted = true;
      updateVolume();
      saveVolume();
    });
    const volume = document.createElement('input');
    volume.type = 'range'; volume.min = '0'; volume.max = '100'; volume.value = '30';
    volume.setAttribute('aria-label', '음량');
    volume.style.cssText = 'width:75px;max-width:15vw;accent-color:#78dfa6;cursor:pointer';
    volume.addEventListener('input', () => {
      highVideo.volume = Number(volume.value) / 100;
      highVideo.muted = highVideo.volume === 0;
      updateVolume();
      saveVolume();
    });
    const updateVolume = () => {
      if (!volume.isConnected || !highVideo) return;
      volumeButton.textContent = highVideo.muted || highVideo.volume === 0 ? '🔇' : '🔊';
      volumeButton.title = highVideo.muted ? '음소거 해제' : '음소거';
      volumeButton.setAttribute('aria-label', volumeButton.title);
      volume.value = highVideo.muted ? '0' : String(Math.round(highVideo.volume * 100));
    };
    highVideo.addEventListener('volumechange', updateVolume);
    bar.append(volume);
    const live = document.createElement('button');
    live.type = 'button'; live.title = '실시간 영상으로 이동';
    live.setAttribute('aria-label', live.title);
    live.style.cssText = 'border:0;background:transparent;color:#fff;cursor:pointer;font:13px system-ui;white-space:nowrap';
    live.innerHTML = '<span style="color:#f03b43;font-size:17px">●</span> LIVE';
    live.addEventListener('click', () => {
      if (manualStoppedUrl) return;
      if (highVideo.seekable.length) highVideo.currentTime = Math.max(0, highVideo.seekable.end(highVideo.seekable.length - 1) - 1);
      highVideo.play().catch(() => {});
    });
    bar.append(live);
    const spacer = document.createElement('span');
    spacer.style.flex = '1';
    bar.append(spacer);
    barChooser = document.createElement('select');
    barChooser.setAttribute('aria-label', '화질 선택');
    barChooser.style.cssText = 'border:0;background:#15231edd;color:#fff;padding:4px;cursor:pointer;font:13px system-ui';
    for (const value of ['1080p', '720p', '540p', '360p', 'auto']) {
      const option = document.createElement('option');
      option.value = value; option.textContent = value === 'auto' ? '자동' : value;
      barChooser.append(option);
    }
    barChooser.value = quality;
    barChooser.addEventListener('change', () => changeQuality(barChooser.value));
    bar.append(barChooser);
    const fullscreenButton = addButton('⛶', '전체 화면', () => {
      if (document.fullscreenElement === box) document.exitFullscreen().catch(() => {});
      else box.requestFullscreen().catch(() => {});
    });
    fullscreenButton.style.fontSize = '24px';
    box.addEventListener('dblclick', event => {
      if (bar.contains(event.target)) return;
      if (document.fullscreenElement === box) document.exitFullscreen().catch(() => {});
      else box.requestFullscreen().catch(() => {});
    });
    box.addEventListener('fullscreenchange', place);
    box.append(highVideo, badge, bar);
    document.body.append(box);
    updatePlay();
    updateVolume();
    place();
    if (Hls.isSupported()) {
      hls = new Hls({enableWorker: false, loader: ExtensionLoader,
        maxBufferLength: quality === '720p' ? 30 : 12,
        backBufferLength: quality === '720p' ? 60 : 15,
        capLevelToPlayerSize: false,
        liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 8});
      const video = highVideo;
      if (quality === '720p') telemetryTimer = setInterval(() => {
        if (!box?.contains(video) || manualStoppedUrl) return;
        const playback = video.getVideoPlaybackQuality?.();
        let bufferSeconds = 0;
        for (let index = 0; index < video.buffered.length; index++) {
          if (video.buffered.start(index) <= video.currentTime && video.buffered.end(index) >= video.currentTime) {
            bufferSeconds = video.buffered.end(index) - video.currentTime;
            break;
          }
        }
        message('telemetry', undefined, undefined, {
          browser: 'Brave extension 720p', width: video.videoWidth, height: video.videoHeight,
          currentTime: video.currentTime, paused: video.paused, muted: video.muted,
          readyState: video.readyState, bufferSeconds: Number(bufferSeconds.toFixed(2)),
          totalVideoFrames: playback?.totalVideoFrames ?? 0,
          droppedVideoFrames: playback?.droppedVideoFrames ?? 0
        }).catch(() => {});
      }, 3000);
      if (player.fallbackFrom === '1080p') {
        quality = '720p';
        sessionStorage.setItem('soop-ubuntu-quality', quality);
        chooser.value = barChooser.value = quality;
        badge.textContent = '1080p를 사용할 수 없어 720p로 전환했습니다';
      }
      hls.loadSource(`${BASE}${player.playlist}`);
      hls.attachMedia(highVideo);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        highVideo.play().catch(() => { badge.textContent = '▶ 눌러 소리 켜고 재생'; });
      });
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) {
          console.debug('SOOP HD HLS:', data.type, data.details);
          return;
        }
        console.warn('SOOP HD HLS:', data.type, data.details, data.response);
        if (quality === '720p' && data.type === Hls.ErrorTypes.NETWORK_ERROR) {
          hls.startLoad(-1);
          badge.textContent = '720p 연결 복구 중…';
        } else badge.textContent = '연결 오류 · 탭을 새로고침해 주세요';
      });
    } else {
      badge.textContent = '브라우저 HLS 재생 미지원';
    }
  }

  async function tick() {
    place();
    if (pendingFallback540 && !busy) {
      pendingFallback540 = false;
      changeQuality('540p');
      return;
    }
    if (pendingAuto720 && !busy && quality === '1080p') {
      pendingAuto720 = false;
      changeQuality('720p');
      return;
    }
    const match = location.pathname.match(broadcastPath);
    if (!match) {
      if (activeUrl && !busy) {
        busy = true;
        message('stop').catch(() => {}).finally(() => { clear(); originalState = null; activeUrl = ''; busy = false; });
      } else if (!busy) {
        clear(); originalState = null;
      }
      manualStoppedUrl = '';
      toolbar?.remove(); toolbar = chooser = null; officialApplied = '';
      return;
    }
    ensureToolbar();
    const url = `https://play.sooplive.com${location.pathname}`;
    if (manualStoppedUrl && url !== manualStoppedUrl) {
      manualStoppedUrl = '';
      clear();
      originalState = null;
    }
    if (quality in nativeQuality) {
      if (activeUrl && !busy) {
        busy = true;
        message('stop').catch(() => {}).finally(() => {
          clear(true); activeUrl = ''; busy = false; selectOfficialQuality(url);
        });
      } else if (!busy) selectOfficialQuality(url);
      return;
    }
    if (url === activeUrl || url === manualStoppedUrl || busy || Date.now() < retryAfter || !targetVideo()) return;
    busy = true;
    try {
      if (activeUrl) {
        await message('stop');
        if (url !== activeUrl) originalState = null;
      }
      clear();
      const player = await message('start', url, quality);
      activeUrl = url;
      retryAfter = 0;
      show(player);
    } catch (error) {
      console.warn('SOOP Ubuntu 1080p:', error.message);
      retryAfter = Date.now() + 10000;
    } finally { busy = false; }
  }

  setInterval(tick, 800);
  setInterval(() => { if (activeUrl) message('ping').catch(() => {}); }, 15000);
  tick();
})();
