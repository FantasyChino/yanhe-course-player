/* 同频: local dual video transport. Media stays on this computer. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const roles = ['camera', 'screen'];
  const video = { camera: $('video-camera'), screen: $('video-screen') };
  const clips = { camera: null, screen: null };
  const state = {
    intent: false, starting: false, scrubbing: false, buffering: false,
    rate: 1, volume: 0.8, audio: 'camera', mode: 'dual', offset: 0,
    time: 0, anchor: performance.now(), clockRunning: false,
    audioTouched: false, lastSaved: 0, resume: null, resumeKey: '', transportGeneration: 0,
  };
  const icons = {
    play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 11 7-11 7z" fill="currentColor" stroke="none"/></svg>',
    pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14M16 5v14" stroke="currentColor" stroke-width="4"/></svg>',
  };
  const readStore = (key, fallback = null) => {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
  };
  const writeStore = (key, value) => {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Private mode/storage full. */ }
  };
  const prefs = readStore('duo.preferences', {});
  if ([0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3].includes(prefs.rate)) state.rate = prefs.rate;
  if (Number.isFinite(prefs.volume)) state.volume = Math.min(1, Math.max(0, prefs.volume));
  if (['camera', 'screen', 'mute'].includes(prefs.audio)) state.audio = prefs.audio;
  if (['dual', 'camera', 'screen'].includes(prefs.mode)) state.mode = prefs.mode;
  const subtitles = {
    cues: [], ends: [], file: null, request: 0, text: '', delay: 0,
    enabled: prefs.subtitleEnabled !== false,
    source: prefs.subtitleSource === 'screen' ? 'screen' : 'camera',
    size: [20, 26, 32, 40].includes(prefs.subtitleSize) ? prefs.subtitleSize : 26,
  };
  const library = { courses: [], courseId: '', lessonId: '', active: null };

  function message(text) { $('status-message').textContent = text; }
  function available() { return roles.filter(role => clips[role] && !video[role].error); }
  function paired() { return available().length === 2; }
  function duration() {
    return Math.max(0, ...available().map(role => {
      const end = video[role].duration;
      return Number.isFinite(end) ? end - (role === 'screen' && paired() ? state.offset : 0) : 0;
    }));
  }
  function clockSource() {
    if (!state.clockRunning) return null;
    const ready = available().filter(role => {
      const media = video[role];
      return media.readyState >= 1 && !media.paused && !media.ended && !media.seeking
        && media.currentTime - (role === 'screen' && paired() ? state.offset : 0) >= 0;
    });
    return [state.audio, 'camera', 'screen'].find(role => ready.includes(role)) || null;
  }
  function current() {
    const source = clockSource();
    if (source) {
      // The audible media is the clock. Decoder delays must not turn into a seek loop.
      state.time = Math.max(0, Math.min(video[source].currentTime
        - (source === 'screen' && paired() ? state.offset : 0), duration()));
      state.anchor = performance.now();
      return state.time;
    }
    // Bridge an offset gap where neither source is on the timeline yet.
    const elapsed = state.clockRunning ? (performance.now() - state.anchor) / 1000 * state.rate : 0;
    return Math.max(0, Math.min(state.time + elapsed, duration() || state.time + elapsed));
  }
  function freezeClock() {
    state.time = current(); state.anchor = performance.now(); state.clockRunning = false;
  }
  function runClock() {
    if (state.clockRunning) return;
    state.anchor = performance.now(); state.clockRunning = true;
  }
  function format(seconds) {
    seconds = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor(seconds / 60) % 60;
    const s = seconds % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
      : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }
  function target(role, time) {
    return time + (role === 'screen' && paired() ? state.offset : 0);
  }
  function inRange(role, time) {
    const t = target(role, time), d = video[role].duration;
    return Number.isFinite(d) && t >= 0 && t < d - 0.06;
  }
  function clampTime(role, time) {
    const d = video[role].duration;
    return Math.min(Math.max(0, target(role, time)), Number.isFinite(d) ? Math.max(0, d - 0.02) : 0);
  }
  function preferences() {
    writeStore('duo.preferences', {
      rate: state.rate, audio: state.audio, volume: state.volume, mode: state.mode,
      subtitleEnabled: subtitles.enabled, subtitleSize: subtitles.size, subtitleSource: subtitles.source,
    });
  }
  function subtitleTime(value) {
    if (!/^(?:\d+:)?\d{2}:\d{2}[.,]\d{1,3}$/.test(value)) return NaN;
    const parts = value.replace(',', '.').split(':').map(Number);
    const seconds = parts.pop(), minutes = parts.pop(), hours = parts.pop() || 0;
    if (seconds >= 60 || minutes >= 60) return NaN;
    return hours * 3600 + minutes * 60 + seconds;
  }
  function subtitleText(value) {
    // Render with textContent. Only discard known subtitle formatting, never interpret HTML.
    const entities = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', lrm: '\u200e', rlm: '\u200f' };
    return value.replace(/<br\s*\/?\s*>/gi, '\n')
      .replace(/<\/?(?:b|i|u|c|v|lang|ruby|rt)(?:[.\s][^>]*)?>/gi, '')
      .replace(/<(?:\d+:)?\d{2}:\d{2}[.,]\d{1,3}>/g, '')
      .replace(/\{\\an[1-9]\}/g, '')
      .replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp|lrm|rlm);/gi, (match, entity) => {
        if (entity[0] !== '#') return entities[entity.toLowerCase()] ?? match;
        const point = entity[1].toLowerCase() === 'x' ? parseInt(entity.slice(2), 16) : Number(entity.slice(1));
        return point > 0 && point <= 0x10ffff && !(point >= 0xd800 && point <= 0xdfff)
          ? String.fromCodePoint(point) : '';
      }).trim();
  }
  function parseSubtitles(text) {
    const cues = [];
    for (const block of text.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').split(/\n[\t ]*\n/)) {
      const lines = block.trim().split('\n');
      if (/^(?:NOTE(?:[ \t]|$)|STYLE\s*$|REGION\s*$)/.test(lines[0].trim())) continue;
      const timing = lines.findIndex(line => line.includes('-->'));
      if (timing < 0) continue;
      const match = lines[timing].trim().match(/^(\S+)\s+-->\s+(\S+)(?:\s+.*)?$/);
      if (!match) continue;
      const start = subtitleTime(match[1]), end = subtitleTime(match[2]);
      const text = subtitleText(lines.slice(timing + 1).join('\n'));
      if (Number.isFinite(start) && Number.isFinite(end) && end > start && text) cues.push({ start, end, text });
    }
    return cues.sort((a, b) => a.start - b.start || a.end - b.end);
  }
  function renderSubtitles() {
    const time = target(subtitles.source, current()) - subtitles.delay;
    const cues = subtitles.cues;
    let text = '';
    if (subtitles.enabled && available().length && cues.length) {
      // Binary search and prefix end times also handle overlapping cues after a backwards seek.
      let low = 0, high = cues.length;
      while (low < high) {
        const middle = (low + high) >>> 1;
        if (cues[middle].start <= time) low = middle + 1;
        else high = middle;
      }
      const active = [];
      for (let i = low - 1; i >= 0 && subtitles.ends[i] > time; i--) {
        if (cues[i].end > time) active.push(cues[i].text);
      }
      text = active.reverse().join('\n');
    }
    if (text !== subtitles.text) { $('subtitle-overlay').textContent = text; subtitles.text = text; }
    $('subtitle-overlay').hidden = !text;
  }
  function renderSubtitleSettings() {
    $('subtitle-toggle').disabled = !subtitles.cues.length;
    $('subtitle-toggle').setAttribute('aria-pressed', String(subtitles.enabled));
    $('subtitle-toggle').textContent = subtitles.enabled ? '字幕开' : '字幕关';
    $('subtitle-name').textContent = subtitles.file ? `${subtitles.file.name} · ${subtitles.cues.length} 条` : '支持 SRT / VTT，拖入或选择字幕';
    $('subtitle-name').title = subtitles.file?.name || '选择与视频对应的本地字幕文件';
    $('subtitle-delay').value = String(subtitles.delay);
    $('subtitle-size').value = String(subtitles.size);
    $('subtitle-source').value = subtitles.source;
    $('subtitle-overlay').style.setProperty('--subtitle-size', `${subtitles.size}px`);
    renderSubtitles();
  }
  function isSubtitle(file) { return file && /\.(srt|vtt)$/i.test(file.name); }
  async function importSubtitle(file) {
    if (!isSubtitle(file)) { message('请选择 SRT 或 VTT 字幕文件。'); return; }
    if (file.size > 10 * 1024 * 1024) { message('字幕文件超过 10 MB，请选择文本格式的 SRT / VTT 文件。'); return; }
    const request = ++subtitles.request;
    try {
      const data = new Uint8Array(await file.arrayBuffer());
      let text;
      if (data[0] === 0xff && data[1] === 0xfe) text = new TextDecoder('utf-16le').decode(data);
      else if (data[0] === 0xfe && data[1] === 0xff) text = new TextDecoder('utf-16be').decode(data);
      else {
        try { text = new TextDecoder('utf-8', { fatal: true }).decode(data); }
        catch { text = new TextDecoder('gb18030').decode(data); }
      }
      const cues = parseSubtitles(text);
      if (request !== subtitles.request) return;
      if (!cues.length) { message('没有读到有效的字幕时间轴，请检查 SRT / VTT 文件；原字幕已保留。'); return; }
      subtitles.cues = cues; subtitles.file = file; subtitles.enabled = true; subtitles.delay = 0;
      let end = 0;
      subtitles.ends = cues.map(cue => { end = Math.max(end, cue.end); return end; });
      renderSubtitleSettings(); preferences();
      message(`已载入 ${cues.length} 条字幕。正值偏移会让字幕晚一点出现。`);
    } catch {
      if (request === subtitles.request) message('无法读取字幕文件，请重新选择本地 SRT / VTT 文件。');
    }
  }
  function pairKey() {
    if (!available().length) return '';
    return 'duo.progress.' + roles.map(role => {
      const file = clips[role]?.file;
      return file ? `${role}:${file.identity || file.name}:${file.size}:${file.lastModified}` : `${role}:-`;
    }).join('|');
  }
  function saveProgress(force = false) {
    const key = pairKey();
    if (!key || (!force && Date.now() - state.lastSaved < 5000)) return;
    state.lastSaved = Date.now();
    writeStore(key, { time: current(), offset: state.offset });
  }
  function restoreOffer() {
    const key = pairKey();
    if (state.resumeKey === key) return;
    state.resumeKey = key;
    const saved = readStore(key);
    state.resume = saved && Number.isFinite(saved.time) && saved.time > 5 ? saved : null;
    $('resume-button').hidden = !state.resume;
    if (state.resume) $('resume-button').textContent = `继续上次 · ${format(state.resume.time)}`;
  }
  function setAudio() {
    roles.forEach(role => {
      // A hidden view remains loaded and can still be the selected audio source.
      video[role].muted = state.audio !== role;
      video[role].volume = state.volume;
    });
    $('audio-select').value = state.audio;
    $('volume-range').value = state.volume;
    preferences();
  }
  function setMode(mode) {
    if (!['dual', 'camera', 'screen'].includes(mode)) return;
    state.mode = mode;
    $('workspace').dataset.mode = mode;
    document.querySelectorAll('button[data-mode]').forEach(button => {
      const active = button.dataset.mode === mode;
      button.setAttribute('aria-pressed', String(active));
      button.classList.toggle('active', active);
      button.classList.toggle('is-active', active);
    });
    preferences();
  }
  function setRate(rate) {
    state.time = current(); state.anchor = performance.now();
    state.rate = rate;
    roles.forEach(role => {
      if (Math.abs(video[role].playbackRate - rate) > 0.000001) video[role].playbackRate = rate;
      if (Math.abs(video[role].defaultPlaybackRate - rate) > 0.000001) video[role].defaultPlaybackRate = rate;
    });
    $('speed-select').value = String(rate);
    preferences();
  }
  function pauseMedia() {
    freezeClock(); state.transportGeneration++;
    roles.forEach(role => video[role].pause());
  }
  function renderTransport() {
    const loaded = available().length > 0;
    $('play-toggle').innerHTML = state.intent ? icons.pause : icons.play;
    $('play-toggle').setAttribute('aria-label', state.intent ? '暂停两个视频' : '同步播放');
    $('play-toggle').title = state.intent ? '暂停（空格）' : '播放（空格）';
    ['play-toggle', 'backward', 'forward', 'reset-button'].forEach(id => { $(id).disabled = !loaded; });
    $('swap-files').disabled = !clips.camera && !clips.screen;
    $('seek-range').disabled = !duration();
    const time = current();
    $('time-current').textContent = format(time);
    $('time-duration').textContent = format(duration());
    if (!state.scrubbing) $('seek-range').value = duration() ? Math.min(1000, time / duration() * 1000) : 0;
    $('seek-range').setAttribute('aria-valuetext', `${format(time)} / ${format(duration())}`);
    let status = '等待导入视频';
    if (loaded) status = state.buffering && state.intent ? '正在缓冲…'
      : available().length === 2 ? (state.intent ? '双画面同步播放' : '双画面已就绪')
      : (state.intent ? '单画面播放中' : '视频已就绪');
    $('sync-status').textContent = status;
    $('sync-status').dataset.state = loaded ? (state.buffering ? 'waiting' : 'ready') : 'empty';
    renderSubtitles();
  }
  function setMediaTimes(time, force = true) {
    available().forEach(role => {
      const media = video[role];
      if (media.readyState < 1) return;
      const next = clampTime(role, time);
      if (force || Math.abs(media.currentTime - next) > 0.06) {
        try { media.currentTime = next; clips[role].lastCorrection = performance.now(); } catch { /* Metadata not yet available. */ }
      }
      if (Math.abs(media.playbackRate - state.rate) > 0.000001) media.playbackRate = state.rate;
    });
  }
  function allReady() {
    const time = current();
    return available().every(role => !inRange(role, time)
      || (!video[role].seeking && video[role].readyState >= 3));
  }
  async function startMedia() {
    if (!state.intent || state.starting || state.scrubbing || !available().length) return;
    const time = current();
    const playing = available().filter(role => inRange(role, time));
    if (state.clockRunning && playing.length && playing.every(role => !video[role].paused)) return;
    state.starting = true;
    const generation = state.transportGeneration;
    const results = await Promise.allSettled(playing.map(role => {
      const media = video[role];
      if (Math.abs(media.playbackRate - state.rate) > 0.000001) media.playbackRate = state.rate;
      return media.paused ? media.play() : Promise.resolve();
    }));
    state.starting = false;
    if (generation !== state.transportGeneration) { maybeResume(); return; }
    if (!state.intent) { pauseMedia(); return; }
    const denied = results.find(result => result.status === 'rejected' && result.reason?.name === 'NotAllowedError');
    if (denied) {
      state.intent = false;
      pauseMedia();
      message('浏览器暂停了自动播放，请点击播放按钮继续。');
    }
    else if (state.intent && !state.scrubbing && allReady()) {
      state.buffering = false; runClock();
    }
    renderTransport();
  }
  function maybeResume() {
    if (state.intent && !state.scrubbing && allReady()) {
      state.buffering = false;
      startMedia();
    }
  }
  function togglePlay() {
    if (!available().length) { message('先拖入一个或两个本地视频。'); return; }
    if (state.intent) {
      pauseMedia(); state.intent = false; state.buffering = false; saveProgress(true);
    } else {
      if (duration() && current() >= duration() - 0.1) seekTo(0);
      state.intent = true; state.buffering = false;
      setMediaTimes(current(), false);
      // Call play from this user gesture even if metadata is still buffering.
      startMedia();
    }
    renderTransport();
  }
  function seekTo(time, persist = true) {
    if (!available().length) return;
    pauseMedia();
    state.time = Math.max(0, Math.min(time, duration() || time));
    available().forEach(role => { clips[role].startTime = state.time; });
    if (duration() && state.time >= duration() - 0.04) {
      state.intent = false; state.buffering = false;
    }
    setMediaTimes(state.time);
    renderTransport();
    if (persist) saveProgress(true);
    maybeResume();
  }
  function setOffset(value) {
    value = Number(value);
    if (!Number.isFinite(value)) return;
    const time = current();
    state.offset = Math.min(86400, Math.max(-86400, Math.round(value * 10) / 10));
    $('offset-input').value = state.offset.toFixed(1);
    seekTo(time);
    message(`PPT 时间 = 课堂时间 ${state.offset >= 0 ? '+' : '−'} ${Math.abs(state.offset).toFixed(1)} 秒。`);
    saveProgress(true);
  }
  function isVideo(file) {
    return file && (file.type?.startsWith('video/') || /\.(mp4|m4v|webm|mov|mkv|ogv|ogg|ts)$/i.test(file.name));
  }
  function clearClip(role) {
    const previous = clips[role], media = video[role];
    clips[role] = null;
    media.removeAttribute('src'); media.load();
    if (previous?.objectURL) URL.revokeObjectURL(previous.url);
    $('name-' + role).textContent = '尚未选择视频';
    $('name-' + role).title = '尚未选择视频';
    $('empty-' + role).hidden = false;
    delete $('stage-' + role).dataset.loaded;
  }
  function clearSubtitles() {
    subtitles.request++;
    subtitles.cues = []; subtitles.ends = []; subtitles.file = null; subtitles.delay = 0;
    renderSubtitleSettings();
  }
  function assignFiles(assignments, timeOverride, options = {}) {
    const valid = Object.entries(assignments).filter(([, file]) => isVideo(file));
    if (!valid.length) { message('请选择视频文件，推荐 MP4（H.264 / AAC）。'); return; }
    saveProgress(true);
    const time = Number.isFinite(timeOverride) ? Math.max(0, timeOverride) : current();
    const playing = options.paused ? false : state.intent;
    state.intent = false;
    pauseMedia();
    state.scrubbing = false;
    if (options.resetOffset) { state.offset = 0; $('offset-input').value = '0.0'; }
    if (options.replaceAll) { roles.forEach(clearClip); clearSubtitles(); }
    if (!options.fromLibrary && !options.keepLibrary) {
      library.active = null;
      if (library.courses.length) { $('library-current').textContent = '当前：手动导入的视频'; renderLibraryLessons(); }
    }
    valid.forEach(([role, file]) => {
      const media = video[role];
      const previous = clips[role];
      media.removeAttribute('src');
      if (previous?.objectURL) URL.revokeObjectURL(previous.url);
      const objectURL = file._libraryMedia !== true;
      const url = objectURL ? URL.createObjectURL(file) : file.url;
      clips[role] = { file, url, objectURL, startTime: time, lastCorrection: performance.now() };
      media.preload = objectURL ? 'auto' : 'metadata';
      media.src = url;
      media.load();
      $('name-' + role).textContent = file.name;
      $('name-' + role).title = file.name;
      $('empty-' + role).hidden = true;
      $('stage-' + role).dataset.loaded = 'true';
    });
    state.time = time;
    state.intent = playing;
    state.buffering = false;
    state.resumeKey = '';
    if ((!state.audioTouched || options.fromLibrary) && state.audio !== 'mute'
        && !clips[state.audio] && available().length === 1) state.audio = available()[0];
    if (options.fromLibrary && available().length === 1) setMode(available()[0]);
    setAudio(); setRate(state.rate); restoreOffer(); renderTransport();
    message(valid.length === 2 ? '两个视频已导入。可随时切换画面、音频和倍速。' : '视频已导入；另一画面可以继续拖入。');
  }
  function importPair(fileList, preferredRole) {
    const files = Array.from(fileList || []).filter(isVideo);
    if (!files.length) { message('没有找到可播放的视频文件。'); return; }
    if (files.length === 1) {
      let role = preferredRole;
      if (!role) role = /screen|vga|ppt|屏幕|课件/i.test(files[0].name) ? 'screen'
        : (!clips.camera ? 'camera' : !clips.screen ? 'screen' : 'camera');
      assignFiles({ [role]: files[0] });
    } else {
      const screenIndex = files.findIndex(file => /screen|vga|ppt|屏幕|课件/i.test(file.name));
      const screenFile = files[screenIndex >= 0 ? screenIndex : 1];
      const cameraFile = files.find(file => file !== screenFile);
      assignFiles({ camera: cameraFile, screen: screenFile });
      message(`已导入两个视频${files.length > 2 ? '（只使用前两个匹配文件）' : ''}；若左右来源相反，点击“交换视频”。`);
    }
  }
  function importDropped(fileList, preferredRole) {
    const files = Array.from(fileList || []);
    const subtitle = files.find(isSubtitle);
    if (subtitle) importSubtitle(subtitle);
    if (files.some(isVideo)) importPair(files, preferredRole);
    else if (!subtitle) message('请拖入视频或 SRT / VTT 字幕文件。');
  }
  function libraryMedia(source, courseId, lessonId, role) {
    if (!source || typeof source.url !== 'string' || typeof source.name !== 'string') return null;
    const url = new URL(source.url, location.href);
    if (url.origin !== location.origin || !/^\/media\/[^/]+\/[^/]+\/(camera|screen)$/.test(url.pathname)) return null;
    return { name: source.name, size: Number(source.size) || 0, lastModified: Number(source.lastModified) || 0,
      type: 'video/mp4', url: url.href, identity: `library:${courseId}:${lessonId}:${role}`, _libraryMedia: true };
  }
  function renderLibraryLessons() {
    const course = library.courses.find(item => item.id === library.courseId);
    if (!course) return;
    $('lesson-course-name').textContent = course.name;
    $('lesson-count').textContent = `${course.lessons.length} 节`;
    const list = document.createDocumentFragment();
    course.lessons.forEach((lesson, index) => {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'lesson-button'; button.dataset.lessonId = lesson.id;
      const active = library.active?.courseId === course.id && library.active?.lessonId === lesson.id;
      if (active) button.setAttribute('aria-current', 'true');
      button.classList.toggle('is-active', active);
      button.disabled = !lesson.camera && !lesson.screen;
      const number = document.createElement('span'); number.className = 'lesson-number';
      number.textContent = String(lesson.index || index + 1).padStart(2, '0');
      const title = document.createElement('span'); title.className = 'lesson-title'; title.textContent = lesson.title;
      const meta = document.createElement('span'); meta.className = 'lesson-meta';
      meta.textContent = lesson.camera && lesson.screen ? '课堂 + PPT' : lesson.camera ? '仅课堂录像' : '仅 PPT';
      button.append(number, title, meta);
      button.title = `${course.name} · 第 ${lesson.index || index + 1} 节 · ${lesson.title}`;
      button.addEventListener('click', () => loadLibraryLesson(course.id, lesson.id));
      list.append(button);
    });
    if (!course.lessons.length) {
      const empty = document.createElement('p'); empty.textContent = '这个课程文件夹中还没有视频。'; list.append(empty);
    }
    $('lesson-list').replaceChildren(list);
  }
  function loadLibraryLesson(courseId, lessonId) {
    const course = library.courses.find(item => item.id === courseId);
    const lesson = course?.lessons.find(item => item.id === lessonId);
    if (!lesson) return;
    if (library.active?.courseId === courseId && library.active?.lessonId === lessonId) return;
    const assignments = {};
    roles.forEach(role => {
      const source = libraryMedia(lesson[role], courseId, lessonId, role);
      if (source) assignments[role] = source;
    });
    if (!Object.keys(assignments).length) { message('这一节没有可读取的视频，请检查课程文件夹。'); return; }
    library.courseId = courseId; library.lessonId = lessonId;
    library.active = { courseId, lessonId };
    assignFiles(assignments, 0, { paused: true, resetOffset: true, replaceAll: true, fromLibrary: true });
    $('library-current').textContent = `${course.name} · 第 ${lesson.index} 节 · ${lesson.title}`;
    $('library-status').textContent = `已载入本地课程库 · ${library.courses.length} 门课程 · 选择节次后点击播放`;
    $('library-course').value = courseId;
    writeStore('duo.library.selection', { courseId, lessonId });
    writeStore('duo.library.last.' + courseId, lessonId);
    renderLibraryLessons();
    message(`已选择${course.name}第 ${lesson.index} 节，${Object.keys(assignments).length === 2 ? '课堂与 PPT 已自动配对' : '视频已载入'}。点击播放开始学习。`);
  }
  function chooseLibraryCourse(courseId) {
    const course = library.courses.find(item => item.id === courseId);
    if (!course) return;
    library.courseId = courseId;
    const last = readStore('duo.library.last.' + courseId, '');
    const lesson = course.lessons.find(item => item.id === last) || course.lessons[0];
    if (lesson) loadLibraryLesson(courseId, lesson.id);
    else {
      saveProgress(true); state.intent = false; pauseMedia();
      roles.forEach(clearClip); clearSubtitles();
      state.time = 0; state.offset = 0; state.buffering = false;
      state.resume = null; state.resumeKey = ''; $('resume-button').hidden = true; $('offset-input').value = '0.0';
      library.active = null; library.lessonId = '';
      $('library-course').value = courseId;
      $('library-current').textContent = `${course.name} · 暂无视频`;
      $('library-status').textContent = '课程目录中没有找到 MP4，请检查课程配置与视频目录。';
      writeStore('duo.library.selection', { courseId, lessonId: '' });
      renderLibraryLessons(); renderTransport();
    }
  }
  async function initializeLibrary() {
    if (!/^https?:$/.test(location.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)) return;
    $('library-status').textContent = '正在读取本地课程库…';
    try {
      const response = await fetch('/api/library', { cache: 'no-store' });
      if (!response.ok) throw new Error('Library unavailable');
      const data = await response.json();
      if (!Array.isArray(data.courses)) throw new Error('Invalid library');
      library.courses = data.courses.filter(course => typeof course.id === 'string' && typeof course.name === 'string'
        && Array.isArray(course.lessons));
      $('library-course').replaceChildren(...library.courses.map(course => {
        const option = document.createElement('option'); option.value = course.id;
        option.textContent = course.name + (course.teacher ? ` · ${course.teacher}` : ''); return option;
      }));
      $('library-controls').hidden = false; $('lesson-sidebar').hidden = false;
      $('study-layout').classList.add('library-ready');
      if (!library.courses.length) {
        $('library-course').disabled = true; $('library-status').textContent = '还没有找到课程，请检查本地课程文件夹。'; return;
      }
      const saved = readStore('duo.library.selection', {});
      const course = library.courses.find(item => item.id === saved.courseId) || library.courses[0];
      chooseLibraryCourse(course.id);
    } catch {
      $('library-status').textContent = '本地课程库暂时无法读取，请从桌面快捷方式重新打开。也可以手动导入视频。';
    }
  }
  $('library-course').addEventListener('change', event => chooseLibraryCourse(event.target.value));

  const playerCard = document.querySelector('.player-card');
  const controlDock = $('control-dock');
  const fullscreenUI = { visible: false, hovering: false, keyboard: false, menuOpen: false, editing: false,
    hideTimer: 0, cursorTimer: 0, suppressVideoTapUntil: 0 };
  const isFullscreen = () => document.fullscreenElement === playerCard;
  function measureDock() {
    if (isFullscreen()) playerCard.style.setProperty('--control-dock-height', `${controlDock.offsetHeight}px`);
  }
  function hideControls(force = false) {
    clearTimeout(fullscreenUI.hideTimer);
    if (!isFullscreen()) return;
    if (!force && (fullscreenUI.hovering || fullscreenUI.menuOpen || fullscreenUI.editing || state.scrubbing
        || (fullscreenUI.keyboard && controlDock.contains(document.activeElement)))) return;
    fullscreenUI.visible = false;
    playerCard.classList.remove('fullscreen-controls-visible');
    playerCard.classList.add('fullscreen-cursor-hidden');
    controlDock.inert = true;
  }
  function scheduleHide(delay = 1200) {
    clearTimeout(fullscreenUI.hideTimer);
    fullscreenUI.hideTimer = setTimeout(() => hideControls(), delay);
  }
  function showControls() {
    if (!isFullscreen()) return;
    clearTimeout(fullscreenUI.hideTimer);
    fullscreenUI.visible = true;
    controlDock.inert = false;
    playerCard.classList.add('fullscreen-controls-visible');
    playerCard.classList.remove('fullscreen-cursor-hidden');
    measureDock();
  }
  playerCard.addEventListener('pointermove', event => {
    if (!isFullscreen() || event.pointerType === 'touch') return;
    playerCard.classList.remove('fullscreen-cursor-hidden');
    clearTimeout(fullscreenUI.cursorTimer);
    const rect = playerCard.getBoundingClientRect(), dock = controlDock.getBoundingClientRect();
    fullscreenUI.hovering = fullscreenUI.visible && event.clientX >= dock.left && event.clientX <= dock.right
      && event.clientY >= dock.top && event.clientY <= dock.bottom;
    if (event.clientY >= rect.bottom - 80 || fullscreenUI.hovering) showControls();
    else scheduleHide();
    fullscreenUI.cursorTimer = setTimeout(() => {
      if (!fullscreenUI.visible) playerCard.classList.add('fullscreen-cursor-hidden');
    }, 1400);
  });
  playerCard.addEventListener('pointerleave', () => { fullscreenUI.hovering = false; scheduleHide(); });
  playerCard.addEventListener('pointerdown', event => {
    if (!isFullscreen()) return;
    fullscreenUI.keyboard = false;
    if (controlDock.contains(event.target)) {
      fullscreenUI.menuOpen = event.target.tagName === 'SELECT';
      fullscreenUI.editing = event.target.matches('input[type="number"]');
      showControls();
    } else {
      fullscreenUI.menuOpen = false; fullscreenUI.editing = false;
      if (controlDock.contains(document.activeElement)) document.activeElement.blur();
      if (event.pointerType === 'touch') {
        fullscreenUI.suppressVideoTapUntil = performance.now() + 600;
        fullscreenUI.hovering = false;
        if (fullscreenUI.visible) hideControls(true);
        else { showControls(); scheduleHide(3500); }
      } else scheduleHide();
    }
  });
  controlDock.addEventListener('focusin', () => { if (fullscreenUI.keyboard) showControls(); });
  controlDock.addEventListener('focusout', () => {
    fullscreenUI.menuOpen = false; fullscreenUI.editing = false; scheduleHide();
  });
  controlDock.addEventListener('change', () => {
    fullscreenUI.menuOpen = false; fullscreenUI.editing = false; scheduleHide();
  });
  document.addEventListener('keydown', event => {
    if (!isFullscreen()) return;
    if (event.key === 'Tab') { fullscreenUI.keyboard = true; showControls(); scheduleHide(3500); }
    else if (event.key === 'Escape') { fullscreenUI.menuOpen = false; fullscreenUI.editing = false; }
  }, true);
  document.addEventListener('fullscreenchange', () => {
    clearTimeout(fullscreenUI.hideTimer); clearTimeout(fullscreenUI.cursorTimer);
    fullscreenUI.hovering = false; fullscreenUI.keyboard = false;
    fullscreenUI.menuOpen = false; fullscreenUI.editing = false;
    if (isFullscreen()) {
      if (controlDock.contains(document.activeElement)) document.activeElement.blur();
      measureDock(); hideControls(true);
    } else {
      fullscreenUI.visible = false; controlDock.inert = false;
      playerCard.classList.remove('fullscreen-controls-visible', 'fullscreen-cursor-hidden');
    }
    $('fullscreen').title = isFullscreen() ? '退出全屏（Esc / F）' : '进入全屏（F）';
    $('fullscreen').setAttribute('aria-label', isFullscreen() ? '退出播放器全屏' : '进入播放器全屏');
  });
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(measureDock).observe(controlDock);
  window.addEventListener('resize', measureDock);

  roles.forEach(role => {
    const media = video[role], input = $('file-' + role), drop = $('drop-' + role);
    media.muted = true;
    $('pick-' + role).addEventListener('click', () => input.click());
    input.addEventListener('change', () => {
      if (input.files.length) importPair(input.files, role);
      input.value = '';
    });
    drop.addEventListener('click', event => {
      if (event.target !== media) input.click();
    });
    drop.addEventListener('dragover', event => {
      event.preventDefault(); event.stopPropagation();
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
      drop.dataset.dragging = 'true';
    });
    drop.addEventListener('dragleave', event => {
      if (!drop.contains(event.relatedTarget)) delete drop.dataset.dragging;
    });
    drop.addEventListener('drop', event => {
      event.preventDefault(); event.stopPropagation(); delete drop.dataset.dragging;
      importDropped(event.dataTransfer.files, role);
    });
    drop.addEventListener('keydown', event => {
      if (event.target === drop && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault(); input.click();
      }
    });
    media.addEventListener('loadedmetadata', () => {
      const initial = clips[role]?.startTime ?? state.time;
      try { media.currentTime = clampTime(role, initial); } catch { /* Unsupported file. */ }
      restoreOffer(); renderTransport();
      if (clips.camera && clips.screen && video.camera.readyState >= 1 && video.screen.readyState >= 1
          && Math.abs(video.camera.duration - video.screen.duration) > 10) {
        message('两段视频时长不同；可用 PPT 偏移微调同步，较短的视频到结尾后会停留在最后一帧。');
      }
      maybeResume();
    });
    ['canplay', 'seeked', 'loadeddata'].forEach(event => media.addEventListener(event, maybeResume));
    media.addEventListener('waiting', () => {
      if (state.intent && !state.scrubbing && inRange(role, current()) && !media.seeking && media.readyState < 3) {
        state.buffering = true; pauseMedia(); renderTransport();
      }
    });
    media.addEventListener('ended', () => {
      if (current() >= duration() - 0.08) {
        state.intent = false; state.buffering = false; pauseMedia(); saveProgress(true); renderTransport();
      }
    });
    media.addEventListener('error', () => {
      if (!clips[role] || !media.src) return;
      state.intent = false; pauseMedia();
      $('empty-' + role).hidden = false;
      message(`${role === 'camera' ? '课堂' : 'PPT'}视频无法解码。请使用 H.264/AAC 的 MP4，或浏览器支持的 WebM 文件。`);
      renderTransport();
    });
    media.addEventListener('click', event => {
      event.stopPropagation();
      if (performance.now() < fullscreenUI.suppressVideoTapUntil) return;
      if (clips[role]) togglePlay();
      else input.click();
    });
    media.addEventListener('dblclick', () => setMode(state.mode === role ? 'dual' : role));
  });

  document.addEventListener('dragover', event => event.preventDefault());
  document.addEventListener('drop', event => { event.preventDefault(); importDropped(event.dataTransfer.files); });
  $('pick-pair').addEventListener('click', () => $('file-pair').click());
  $('file-pair').addEventListener('change', event => {
    importPair(event.target.files); event.target.value = '';
  });
  $('pick-subtitle').addEventListener('click', () => $('file-subtitle').click());
  $('file-subtitle').addEventListener('change', event => {
    if (event.target.files.length) importSubtitle(event.target.files[0]);
    event.target.value = '';
  });
  $('subtitle-toggle').addEventListener('click', () => {
    subtitles.enabled = !subtitles.enabled; renderSubtitleSettings(); preferences();
    if (subtitles.enabled && !subtitles.cues.length) message('请先选择或拖入本地 SRT / VTT 字幕文件。');
  });
  $('subtitle-delay').addEventListener('change', event => {
    const value = Number(event.target.value);
    if (Number.isFinite(value)) subtitles.delay = Math.max(-86400, Math.min(86400, Math.round(value * 10) / 10));
    renderSubtitleSettings();
  });
  $('subtitle-size').addEventListener('change', event => {
    const size = Number(event.target.value);
    if ([20, 26, 32, 40].includes(size)) subtitles.size = size;
    renderSubtitleSettings(); preferences();
  });
  $('subtitle-source').addEventListener('change', event => {
    subtitles.source = event.target.value === 'screen' ? 'screen' : 'camera';
    renderSubtitleSettings(); preferences();
  });
  document.querySelectorAll('button[data-mode]').forEach(button => {
    button.addEventListener('click', () => setMode(button.dataset.mode));
  });
  $('play-toggle').addEventListener('click', togglePlay);
  $('backward').addEventListener('click', () => seekTo(current() - 10));
  $('forward').addEventListener('click', () => seekTo(current() + 10));
  $('reset-button').addEventListener('click', () => seekTo(0));
  $('seek-range').addEventListener('pointerdown', () => {
    pauseMedia(); state.scrubbing = true;
  });
  $('seek-range').addEventListener('input', event => {
    const time = Number(event.target.value) / 1000 * duration();
    seekTo(time, false);
    $('time-current').textContent = format(time);
  });
  const endScrub = () => {
    if (!state.scrubbing) return;
    state.scrubbing = false; saveProgress(true); maybeResume();
  };
  window.addEventListener('pointerup', endScrub);
  window.addEventListener('pointercancel', endScrub);
  $('seek-range').addEventListener('change', () => { saveProgress(true); maybeResume(); });
  $('speed-select').addEventListener('change', event => setRate(Number(event.target.value)));
  $('audio-select').addEventListener('change', event => {
    state.audioTouched = true; state.audio = event.target.value; setAudio();
    if (state.audio !== 'mute' && !clips[state.audio]) message('所选音频来源尚未导入，请拖入对应的视频。');
    else message(state.audio === 'mute' ? '已静音。' : `当前使用${state.audio === 'camera' ? '课堂' : 'PPT'}音频。`);
  });
  $('volume-range').addEventListener('input', event => { state.volume = Number(event.target.value); setAudio(); });
  $('offset-input').addEventListener('change', event => setOffset(event.target.value));
  $('offset-minus').addEventListener('click', () => setOffset(state.offset - 0.5));
  $('offset-plus').addEventListener('click', () => setOffset(state.offset + 0.5));
  $('reset-offset').addEventListener('click', () => setOffset(0));
  $('resume-button').addEventListener('click', () => {
    if (!state.resume) return;
    const saved = state.resume;
    setOffset(Number(saved.offset) || 0); seekTo(saved.time);
    $('resume-button').hidden = true;
    message(`已回到 ${format(saved.time)}，点击播放继续。`);
  });
  $('swap-files').addEventListener('click', () => {
    const camera = clips.camera?.file, screen = clips.screen?.file;
    if (!camera || !screen) { message('请先导入两个视频，再交换课堂与 PPT 来源。'); return; }
    const newTime = video.screen.readyState >= 1 ? video.screen.currentTime : current() + state.offset;
    const previousOffset = state.offset;
    assignFiles({ camera: screen, screen: camera }, newTime, { keepLibrary: true });
    setOffset(-previousOffset);
    message('两个视频的来源已交换；音频仍使用当前选择的来源。');
  });
  async function fullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if (playerCard.requestFullscreen) {
        await playerCard.requestFullscreen();
      }
      else message('此浏览器不支持页面全屏，可用浏览器全屏功能。');
    } catch { message('未能进入全屏，请点击全屏按钮重试。'); }
  }
  $('fullscreen').addEventListener('click', fullscreen);
  $('shortcut-toggle').addEventListener('click', () => {
    $('shortcut-panel').hidden = !$('shortcut-panel').hidden;
    $('shortcut-toggle').setAttribute('aria-expanded', String(!$('shortcut-panel').hidden));
  });
  document.addEventListener('keydown', event => {
    if (event.ctrlKey || event.metaKey || event.altKey || /^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(event.target.tagName)
        || event.target.isContentEditable) return;
    const key = event.key.toLowerCase();
    if (event.code === 'Space') { event.preventDefault(); togglePlay(); }
    else if (key === 'arrowleft') { event.preventDefault(); seekTo(current() - 10); }
    else if (key === 'arrowright') { event.preventDefault(); seekTo(current() + 10); }
    else if (key === '1') setMode('dual');
    else if (key === '2') setMode('screen');
    else if (key === '3') setMode('camera');
    else if (key === 'f') { event.preventDefault(); fullscreen(); }
  });

  // Keep both media elements alive across mode changes; only CSS visibility changes.
  setInterval(() => {
    if (state.intent && !state.scrubbing) {
      const time = current();
      const master = clockSource();
      if (duration() && time >= duration() - 0.04) {
        pauseMedia(); state.time = duration(); state.intent = false; state.buffering = false;
        setMediaTimes(state.time); saveProgress(true); renderTransport(); return;
      }
      available().forEach(role => {
        const media = video[role];
        if (media.readyState < 1) return;
        const expected = clampTime(role, time);
        if (!inRange(role, time)) {
          media.pause();
          if (Math.abs(media.currentTime - expected) > 0.15) media.currentTime = expected;
        } else if (master && role !== master && !media.seeking && !state.buffering && !state.starting) {
          const drift = media.currentTime - expected;
          // Only a muted follower may be corrected, with time to settle after every seek.
          if (Math.abs(drift) > 0.35 && performance.now() - clips[role].lastCorrection > 1500) {
            clips[role].lastCorrection = performance.now();
            media.currentTime = expected;
          }
        }
      });
      if (allReady()) {
        state.buffering = false;
        if (!state.clockRunning || available().some(role => inRange(role, time) && video[role].paused)) startMedia();
      }
      saveProgress();
    }
    renderTransport();
  }, 200);
  window.addEventListener('pagehide', () => saveProgress(true));
  function subtitleFrame() {
    if (state.intent && subtitles.enabled && subtitles.cues.length) renderSubtitles();
    requestAnimationFrame(subtitleFrame);
  }
  requestAnimationFrame(subtitleFrame);
  renderSubtitleSettings(); setMode(state.mode); setRate(state.rate); setAudio(); renderTransport();
  initializeLibrary();
})();
