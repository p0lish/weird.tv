const TV = (() => {
    const TESTCARD_URL = '/static/testcard.png';
    const STATIC_SOUND = 'st';
    // skip a video if it hasn't started playing after this long
    const LOAD_TIMEOUT_MS = 15000;
    // how long the channel info stays on screen
    const OSD_MS = 4000;
    // hold the static for a moment on every change, like a real TV
    const STATIC_HOLD_MS = 600;
    const OFFAIR_RETRY_MS = 30000;
    const HEARTBEAT_MS = 20000;
    const LIVE_SYNC_MS = 15000;
    // resync live playback when it drifts further than this from the broadcast
    const LIVE_MAX_DRIFT = 3;
    const HISTORY_SIZE = 50;
    const MAX_CANVAS_WIDTH = 1920;
    const SWIPE_PX = 50;
    const STORAGE = { effects: 'weirdtv.effects', channel: 'weirdtv.channel', client: 'weirdtv.client' };
    // toggleable visual/audio effects and the key that toggles each one
    const EFFECTS = {
        scanlines: { key: 's', label: 'SCANLINES' },
        crt: { key: 't', label: 'CRT TUBE' },
        glitch: { key: 'g', label: 'GLITCH' },
        vhs: { key: 'v', label: 'VHS' },
        noise: { key: 'z', label: 'STATIC SOUND' },
        blip: { key: 'b', label: 'CHANNEL BLIP' },
    };
    const REDUCED_MOTION = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

    let canvas, context, crtCanvas, crt, video, spare, osd, offairEl, autoplayOverlay, menu, vhs;
    let testcardImage, testcardCanvas, testcardContext, tempCanvas, tempContext;
    let testcardMod = 0, pixelOffset1 = 1, pixelOffset2 = 1, blockOffset = 2;
    let drawRect = { x: 0, y: 0, w: 0, h: 0 };
    let videoLoading = true, loadTimeoutID, osdTimeoutID, playTimeoutID, retryTimeoutID, liveSyncID;
    let audioContext, audio = {};
    let muted = false;
    let channels = [], channel = 'mix';
    // previously watched clips; `cursor` points at the one on screen
    let history = [], cursor = -1;
    // a clip preloaded into the spare <video> so the next change is instant
    let upcoming = null;
    // what the live broadcast is showing, and when we asked
    let liveTarget = null, liveEndedID = null;
    let offair = null;
    let watching = null;
    let requestID = 0;
    let initialized = false;
    const clientID = loadClientID();
    let effects = loadEffects();

    // --- storage & settings ----------------------------------------------

    function storageGet(key) {
        try {
            return localStorage.getItem(key);
        } catch (e) {
            return null;
        }
    }

    function storageSet(key, value) {
        try {
            localStorage.setItem(key, value);
        } catch (e) {
            // not fatal, the setting just won't survive a reload
        }
    }

    function loadClientID() {
        let id = storageGet(STORAGE.client);
        if (!id || !/^[A-Za-z0-9-]{8,64}$/.test(id)) {
            const bytes = new Uint8Array(16);
            crypto.getRandomValues(bytes);
            id = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
            storageSet(STORAGE.client, id);
        }
        return id;
    }

    // defaults < saved preference < ?fx=scanlines,noise (or ?fx=none) in the URL
    function loadEffects() {
        const result = { scanlines: true, crt: true, glitch: !REDUCED_MOTION, vhs: false, noise: true, blip: true };
        try {
            Object.assign(result, JSON.parse(storageGet(STORAGE.effects)) || {});
        } catch (e) {
            // corrupt setting, keep the defaults
        }
        const fx = new URLSearchParams(window.location.search).get('fx');
        if (fx !== null) {
            const enabled = fx.split(',');
            Object.keys(EFFECTS).forEach((name) => {
                result[name] = enabled.includes(name) || enabled.includes('all');
            });
        }
        return result;
    }

    function applyEffects() {
        document.body.classList.toggle('no-scanlines', !effects.scanlines);
        document.body.classList.toggle('crt-on', !!(effects.crt && crt));
        document.body.classList.toggle('vhs-on', effects.vhs);
        renderMenu();
    }

    function setEffect(name, enabled) {
        effects[name] = enabled;
        storageSet(STORAGE.effects, JSON.stringify(effects));
        applyEffects();
        showOSD(EFFECTS[name].label + (enabled ? ' ON' : ' OFF'));
    }

    // turn everything off, or back on if everything is already off
    function toggleAllEffects() {
        const enable = !Object.keys(EFFECTS).some((name) => effects[name]);
        Object.keys(EFFECTS).forEach((name) => { effects[name] = enable; });
        storageSet(STORAGE.effects, JSON.stringify(effects));
        applyEffects();
        showOSD('EFFECTS ' + (enable ? 'ON' : 'OFF'));
    }

    // --- server ----------------------------------------------------------

    async function api(path, body) {
        const options = { cache: 'no-store', headers: { 'X-Client-Id': clientID } };
        if (body !== undefined) {
            options.method = 'POST';
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(body);
        }
        const response = await fetch(path, options);
        if (!response.ok) {
            throw new Error(path + ' failed (' + response.status + ')');
        }
        return response.status === 204 ? null : response.json();
    }

    function recentIDs() {
        const ids = history.map((item) => item.id);
        if (upcoming) {
            ids.push(upcoming.item.id);
        }
        return ids.join(',');
    }

    function fetchNext() {
        return api('/api/next?channel=' + encodeURIComponent(channel) + '&exclude=' + encodeURIComponent(recentIDs()));
    }

    async function heartbeat() {
        try {
            watching = await api('/api/heartbeat', { channel });
            renderMenu();
        } catch (e) {
            // the counter is decoration, ignore failures
        }
    }

    // --- drawing ---------------------------------------------------------

    function resizeCanvas() {
        const ratio = Math.min(window.devicePixelRatio || 1, 2, MAX_CANVAS_WIDTH / window.innerWidth);
        canvas.width = Math.round(window.innerWidth * ratio);
        canvas.height = Math.round(window.innerHeight * ratio);
        fitVideo();
    }

    // letterbox the video inside the canvas, keeping its aspect ratio
    function fitVideo() {
        const vw = video.videoWidth || 16, vh = video.videoHeight || 9;
        const scale = Math.min(canvas.width / vw, canvas.height / vh);
        const w = vw * scale, h = vh * scale;
        drawRect = { x: (canvas.width - w) / 2, y: (canvas.height - h) / 2, w, h };
    }

    function showStatic() {
        videoLoading = true;
        testcardContext.drawImage(testcardImage, 0, 0, testcardCanvas.width, testcardCanvas.height);
        pixelOffset1 = Math.floor(Math.random() * 3) + 1;
        pixelOffset2 = Math.floor(Math.random() * 3) + 1;
        blockOffset = Math.floor(Math.random() * 150) + 2;
        if (effects.noise) {
            playAudio(STATIC_SOUND);
        }
    }

    function drawCleanTestcard() {
        if (testcardImage.complete) {
            context.drawImage(testcardImage, 0, 0, canvas.width, canvas.height);
        }
    }

    function drawGlitch() {
        testcardMod = (testcardMod + 1) % 3;
        if (testcardMod !== 0) {
            return;
        }
        const lineOffset = Math.round((Math.random() - 0.5) * 6);
        const imageData = testcardContext.getImageData(0, 0, testcardCanvas.width, testcardCanvas.height),
            pixels = imageData.data;
        let offset = 0, i = 0;
        for (let y = 0; y < testcardCanvas.height; y++) {
            offset = (y % ((Math.random() * blockOffset) | 0) === 0) ? ((Math.random() * lineOffset * lineOffset) | 0) : offset;
            for (let x = 0; x < testcardCanvas.width; x++) {
                i += 4;
                pixels[i] = pixels[i + pixelOffset1 * (offset + lineOffset * lineOffset)];
                pixels[i + pixelOffset1] = pixels[i + pixelOffset2 + 4 * (offset * lineOffset)];
            }
        }
        tempContext.putImageData(imageData, 0, 0);
        context.drawImage(tempCanvas, 0, 0, canvas.width, canvas.height);
    }

    function draw(time) {
        if (offair || (videoLoading && !effects.glitch)) {
            drawCleanTestcard();
        } else if (videoLoading) {
            drawGlitch();
        } else {
            context.fillStyle = '#000';
            context.fillRect(0, 0, canvas.width, canvas.height);
            context.drawImage(video, drawRect.x, drawRect.y, drawRect.w, drawRect.h);
        }
        if (crt && effects.crt) {
            crt.render(canvas, time / 1000);
        }
        requestAnimationFrame(draw);
    }

    // WebGL pass that bends the picture like a CRT: curvature, colour
    // fringing, glow, vignette and a little flicker. Returns null without WebGL.
    function createCRT(target) {
        const gl = target.getContext('webgl', { alpha: false, antialias: false });
        if (!gl) {
            return null;
        }
        const vertexSource = `
            attribute vec2 position;
            varying vec2 uv;
            void main() {
                uv = position * 0.5 + 0.5;
                gl_Position = vec4(position, 0.0, 1.0);
            }`;
        const fragmentSource = `
            precision mediump float;
            varying vec2 uv;
            uniform sampler2D image;
            uniform float time;
            uniform float motion;

            vec2 curve(vec2 p) {
                p = p * 2.0 - 1.0;
                vec2 offset = abs(p.yx) / vec2(5.0, 4.0);
                p = p + p * offset * offset;
                return p * 0.5 + 0.5;
            }

            float rand(vec2 p) {
                return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
            }

            void main() {
                vec2 p = curve(uv);
                if (p.x < 0.0 || p.x > 1.0 || p.y < 0.0 || p.y > 1.0) {
                    gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0);
                    return;
                }
                float fringe = 0.0015 + 0.0006 * motion * sin(time * 0.7);
                vec3 color = vec3(
                    texture2D(image, p + vec2(fringe, 0.0)).r,
                    texture2D(image, p).g,
                    texture2D(image, p - vec2(fringe, 0.0)).b);
                color += 0.08 * texture2D(image, p + vec2(0.003, 0.003)).rgb;
                color *= pow(16.0 * p.x * p.y * (1.0 - p.x) * (1.0 - p.y), 0.2);
                color *= 1.0 - 0.025 * motion * sin(time * 110.0);
                color += (rand(p * fract(time)) - 0.5) * 0.05 * motion;
                gl_FragColor = vec4(color * 1.08, 1.0);
            }`;

        function compile(type, source) {
            const shader = gl.createShader(type);
            gl.shaderSource(shader, source);
            gl.compileShader(shader);
            if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
                throw new Error(gl.getShaderInfoLog(shader));
            }
            return shader;
        }

        let program;
        try {
            program = gl.createProgram();
            gl.attachShader(program, compile(gl.VERTEX_SHADER, vertexSource));
            gl.attachShader(program, compile(gl.FRAGMENT_SHADER, fragmentSource));
            gl.linkProgram(program);
            if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
                throw new Error(gl.getProgramInfoLog(program));
            }
        } catch (e) {
            console.warn('CRT shader unavailable', e);
            return null;
        }
        gl.useProgram(program);
        gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
        const position = gl.getAttribLocation(program, 'position');
        gl.enableVertexAttribArray(position);
        gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
        gl.bindTexture(gl.TEXTURE_2D, gl.createTexture());
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
        const timeUniform = gl.getUniformLocation(program, 'time');
        gl.uniform1f(gl.getUniformLocation(program, 'motion'), REDUCED_MOTION ? 0 : 1);

        return {
            render(source, seconds) {
                if (target.width !== source.width || target.height !== source.height) {
                    target.width = source.width;
                    target.height = source.height;
                    gl.viewport(0, 0, target.width, target.height);
                }
                gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
                gl.uniform1f(timeUniform, seconds % 1000);
                gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
            },
        };
    }

    // --- sound -----------------------------------------------------------

    function audioReady() {
        return audioContext && audioContext.state === 'running' && !muted;
    }

    async function loadAudio(name) {
        try {
            const response = await fetch('/static/' + name + '.mp3');
            audio[name] = await audioContext.decodeAudioData(await response.arrayBuffer());
        } catch (e) {
            console.warn('could not load sound', name, e);
        }
    }

    function playAudio(name) {
        if (!audioReady() || !audio[name]) {
            return;
        }
        const source = audioContext.createBufferSource();
        source.buffer = audio[name];
        source.connect(audioContext.destination);
        source.start(0);
    }

    function playOscillator(type, fromHz, toHz, seconds, volume) {
        const now = audioContext.currentTime;
        const oscillator = audioContext.createOscillator();
        const gain = audioContext.createGain();
        oscillator.type = type;
        oscillator.frequency.setValueAtTime(fromHz, now);
        oscillator.frequency.exponentialRampToValueAtTime(toHz, now + seconds);
        gain.gain.setValueAtTime(volume, now);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + seconds);
        oscillator.connect(gain).connect(audioContext.destination);
        oscillator.start(now);
        oscillator.stop(now + seconds);
    }

    // the click of an old tuner changing channel
    function playBlip() {
        if (effects.blip && audioReady()) {
            playOscillator('square', 1400, 300, 0.07, 0.05);
        }
    }

    // the classic 1 kHz test card tone
    function playTone() {
        if (effects.noise && audioReady()) {
            playOscillator('sine', 1000, 1000, 2.5, 0.03);
        }
    }

    // --- playback --------------------------------------------------------

    function current() {
        return history[cursor];
    }

    function isLive() {
        return channel === 'live';
    }

    function channelInfo(slug) {
        return channels.find((c) => c.slug === slug) || { number: 1, slug, name: slug.toUpperCase() };
    }

    function createVideo() {
        const el = document.createElement('video');
        el.playsInline = true;
        el.preload = 'auto';
        el.addEventListener('error', () => {
            if (el === spare) {
                upcoming = null;
            } else if (el.getAttribute('src')) {
                skipBroken();
            }
        });
        el.addEventListener('ended', () => {
            if (el !== video) {
                return;
            }
            if (isLive()) {
                liveEndedID = el.item && el.item.id;
                tuneLive();
            } else {
                nextClip();
            }
        });
        el.addEventListener('canplay', () => {
            if (el === video && videoLoading && !offair) {
                clearTimeout(playTimeoutID);
                playTimeoutID = setTimeout(playVideo, STATIC_HOLD_MS);
            }
        });
        el.addEventListener('loadedmetadata', () => {
            if (el === video) {
                fitVideo();
            }
            reportDuration(el);
        });
        return el;
    }

    function clearElement(el) {
        el.pause();
        el.item = null;
        el.removeAttribute('src');
        el.load();
    }

    function discardUpcoming() {
        if (upcoming) {
            upcoming = null;
            clearElement(spare);
        }
    }

    function reportDuration(el) {
        const item = el.item;
        if (item && item.duration == null && isFinite(el.duration) && el.duration > 0) {
            item.duration = el.duration;
            api('/api/videos/' + encodeURIComponent(item.id) + '/duration', { duration: el.duration }).catch(() => {});
        }
    }

    function tune(item) {
        clearTimeout(loadTimeoutID);
        clearTimeout(playTimeoutID);
        clearTimeout(retryTimeoutID);
        setOffAir(null);
        if (upcoming && upcoming.item.id === item.id) {
            // swap in the preloaded element and recycle the old one
            [video, spare] = [spare, video];
            upcoming = null;
            clearElement(spare);
            fitVideo();
            if (video.readyState >= 3) {
                playTimeoutID = setTimeout(playVideo, STATIC_HOLD_MS);
            }
        } else {
            video.pause();
            video.item = item;
            video.src = item.src;
            video.load();
        }
        loadTimeoutID = setTimeout(skipBroken, LOAD_TIMEOUT_MS);
    }

    function pushHistory(item) {
        history.length = cursor + 1;
        history.push(item);
        if (history.length > HISTORY_SIZE) {
            history.shift();
        }
        cursor = history.length - 1;
    }

    function skipBroken() {
        if (isLive()) {
            retryTimeoutID = setTimeout(tuneLive, 2000);
        } else {
            nextClip();
        }
    }

    async function nextClip() {
        if (isLive()) {
            showOSD('LIVE - NO SKIPPING');
            return;
        }
        const id = ++requestID;
        showStatic();
        if (cursor < history.length - 1) {
            // we went back earlier, so walk forward through history first
            cursor++;
            tune(history[cursor]);
            return;
        }
        let item = upcoming && upcoming.channel === channel ? upcoming.item : null;
        if (!item) {
            discardUpcoming();
            try {
                item = await fetchNext();
            } catch (e) {
                console.warn(e);
                retryTimeoutID = setTimeout(nextClip, 5000);
                return;
            }
            // a newer channel change happened while we were waiting
            if (id !== requestID) {
                return;
            }
            if (item.offair) {
                setOffAir(item.message);
                return;
            }
        }
        pushHistory(item);
        tune(item);
    }

    function previousClip() {
        if (isLive()) {
            showOSD('LIVE - NO REWINDING');
            return;
        }
        if (cursor <= 0) {
            return;
        }
        requestID++;
        showStatic();
        cursor--;
        tune(history[cursor]);
    }

    async function preloadNext() {
        if (upcoming || isLive() || offair || cursor < history.length - 1) {
            return;
        }
        const forChannel = channel;
        let item;
        try {
            item = await fetchNext();
        } catch (e) {
            return;
        }
        if (item.offair || forChannel !== channel || upcoming || item.id === (current() || {}).id) {
            return;
        }
        upcoming = { item, channel: forChannel };
        spare.item = item;
        spare.src = item.src;
        spare.load();
    }

    function playVideo() {
        clearTimeout(loadTimeoutID);
        const item = current();
        if (isLive() && liveTarget && item && item.id === liveTarget.id) {
            const target = liveOffset();
            video.currentTime = isFinite(video.duration) ? Math.min(target, Math.max(0, video.duration - 0.5)) : target;
        }
        videoLoading = false;
        fitVideo();
        video.muted = muted;
        const started = () => {
            showOSD();
            if (item && !item.viewed) {
                item.viewed = true;
                api('/api/videos/' + encodeURIComponent(item.id) + '/view', {})
                    .then((r) => { item.plays = r.plays; }).catch(() => {});
            }
            preloadNext();
        };
        video.play().then(started).catch(() => {
            // browsers block unmuted autoplay until the user interacts with the page
            video.muted = true;
            video.play().then(started).catch(() => {});
            autoplayOverlay.style.display = 'block';
        });
    }

    function setOffAir(message) {
        offair = message;
        offairEl.textContent = message || '';
        document.body.classList.toggle('offair', !!message);
        renderVHS();
        if (!message) {
            return;
        }
        clearTimeout(loadTimeoutID);
        clearTimeout(playTimeoutID);
        clearTimeout(retryTimeoutID);
        videoLoading = true;
        video.pause();
        playTone();
        showOSD();
        retryTimeoutID = setTimeout(() => (isLive() ? tuneLive() : nextClip()), OFFAIR_RETRY_MS);
    }

    // --- channels & live -------------------------------------------------

    function setChannel(slug) {
        if (!channels.some((c) => c.slug === slug)) {
            return;
        }
        playBlip();
        if (slug === channel && !offair) {
            showOSD();
            return;
        }
        channel = slug;
        storageSet(STORAGE.channel, slug);
        discardUpcoming();
        history.length = cursor + 1;
        clearInterval(liveSyncID);
        heartbeat();
        renderMenu();
        if (isLive()) {
            liveSyncID = setInterval(syncLive, LIVE_SYNC_MS);
            tuneLive();
        } else {
            liveTarget = null;
            nextClip();
        }
    }

    function stepChannel(step) {
        const index = channels.findIndex((c) => c.slug === channel);
        const next = channels[(index + step + channels.length) % channels.length];
        if (next) {
            setChannel(next.slug);
        }
    }

    function tuneNumber(number) {
        const target = channels.find((c) => c.number === number);
        if (target) {
            setChannel(target.slug);
        } else {
            playBlip();
            showOSD('CH ' + String(number).padStart(2, '0') + ' - NOTHING HERE');
        }
    }

    function liveOffset() {
        return liveTarget.offset + (performance.now() - liveTarget.at) / 1000;
    }

    async function tuneLive() {
        const id = ++requestID;
        showStatic();
        let state;
        try {
            state = await api('/api/live');
        } catch (e) {
            retryTimeoutID = setTimeout(tuneLive, 5000);
            return;
        }
        if (id !== requestID || !isLive()) {
            return;
        }
        if (state.offair) {
            setOffAir(state.message);
            return;
        }
        if (state.id === liveEndedID) {
            // we finished a little ahead of the broadcast, wait for it to move on
            retryTimeoutID = setTimeout(tuneLive, 1000);
            return;
        }
        liveEndedID = null;
        liveTarget = { id: state.id, offset: state.offset, at: performance.now() };
        pushHistory(state);
        tune(state);
    }

    async function syncLive() {
        if (!isLive() || (videoLoading && !offair)) {
            return;
        }
        let state;
        try {
            state = await api('/api/live');
        } catch (e) {
            return;
        }
        if (!isLive()) {
            return;
        }
        const item = current();
        if (state.offair || offair || !item || state.id !== item.id) {
            tuneLive();
            return;
        }
        liveTarget = { id: state.id, offset: state.offset, at: performance.now() };
        if (Math.abs(video.currentTime - state.offset) > LIVE_MAX_DRIFT) {
            video.currentTime = state.offset;
        }
    }

    // --- viewer actions --------------------------------------------------

    async function vote(value) {
        const item = current();
        if (!item || offair) {
            return;
        }
        const newValue = item.vote === value ? 0 : value;
        try {
            const result = await api('/api/videos/' + encodeURIComponent(item.id) + '/vote', { value: newValue });
            item.vote = result.vote;
            item.score = result.score;
        } catch (e) {
            showOSD('VOTE FAILED');
            return;
        }
        showOSD(newValue > 0 ? '▲ LIKED' : newValue < 0 ? '▼ DISLIKED' : 'VOTE REMOVED');
        renderMenu();
        if (newValue < 0 && !isLive()) {
            setTimeout(nextClip, 800);
        }
    }

    async function share() {
        const item = current();
        if (!item || offair) {
            return;
        }
        const url = window.location.origin + item.share;
        if (navigator.share && window.matchMedia('(hover: none)').matches) {
            try {
                await navigator.share({ title: 'Weird TV', text: item.title || 'weird.tv', url });
                return;
            } catch (e) {
                // cancelled or unsupported, fall back to copying
            }
        }
        try {
            await navigator.clipboard.writeText(url);
            showOSD('LINK COPIED');
        } catch (e) {
            window.prompt('Copy this link', url);
        }
    }

    function toggleMute() {
        muted = !muted;
        video.muted = muted;
        showOSD(muted ? 'MUTE' : 'SOUND ON');
        renderMenu();
    }

    function toggleFullscreen() {
        if (document.fullscreenElement) {
            document.exitFullscreen();
        } else if (document.documentElement.requestFullscreen) {
            document.documentElement.requestFullscreen().catch(() => {});
        }
    }

    function unlockAudio() {
        autoplayOverlay.style.display = 'none';
        if (audioContext && audioContext.state !== 'running') {
            audioContext.resume();
        }
        if (!videoLoading) {
            video.muted = muted;
            video.play().catch(() => {});
        }
    }

    // --- on-screen display -----------------------------------------------

    function showOSD(message) {
        const info = channelInfo(channel);
        const item = offair ? null : current();
        osd.querySelector('.osd-channel').textContent = 'CH ' + String(info.number).padStart(2, '0') + (muted ? ' 🔇' : '');
        osd.querySelector('.osd-name').textContent = info.name;
        osd.querySelector('.osd-title').textContent = item ? (item.title || item.filename || '') : '';
        const meta = [];
        if (item) {
            meta.push((item.score > 0 ? '▲ +' : item.score < 0 ? '▼ ' : '▲ ') + item.score);
            meta.push(item.plays + (item.plays === 1 ? ' view' : ' views'));
            if (item.archived) {
                meta.push('VAULT');
            }
        }
        if (watching) {
            meta.push('👁 ' + (isLive() ? watching.channel_watching : watching.watching));
        }
        osd.querySelector('.osd-meta').textContent = meta.join('  ·  ');
        const source = osd.querySelector('.osd-source');
        if (item && item.source) {
            source.href = item.source;
            source.textContent = '/' + item.board + '/';
            source.style.display = '';
        } else {
            source.style.display = 'none';
        }
        osd.querySelector('.osd-message').textContent = typeof message === 'string' ? message : '';
        osd.classList.add('visible');
        clearTimeout(osdTimeoutID);
        osdTimeoutID = setTimeout(() => osd.classList.remove('visible'), OSD_MS);
        renderVHS();
    }

    function renderVHS() {
        if (!vhs) {
            return;
        }
        vhs.querySelector('.vhs-mode').textContent = offair ? '■ STOP' : isLive() ? '● LIVE' : '▶ PLAY';
        const now = new Date();
        const months = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
        const hours = now.getHours();
        const pad = (n) => String(n).padStart(2, '0');
        vhs.querySelector('.vhs-clock').textContent =
            (hours < 12 ? 'AM ' : 'PM ') + ((hours % 12) || 12) + ':' + pad(now.getMinutes()) + ':' + pad(now.getSeconds()) +
            '\n' + months[now.getMonth()] + '. ' + pad(now.getDate()) + ' ' + now.getFullYear();
    }

    // --- menu ------------------------------------------------------------

    function button(label, action, pressed) {
        const el = document.createElement('button');
        el.type = 'button';
        el.textContent = label;
        el.dataset.action = action;
        if (pressed !== undefined) {
            el.setAttribute('aria-pressed', String(pressed));
        }
        return el;
    }

    function renderMenu() {
        if (!menu) {
            return;
        }
        const channelList = menu.querySelector('.menu-channels');
        channelList.replaceChildren(...channels.map((c) =>
            button(c.number + ' ' + c.name, 'channel:' + c.slug, c.slug === channel)));
        const effectList = menu.querySelector('.menu-effects');
        effectList.replaceChildren(...Object.keys(EFFECTS).map((name) =>
            button(EFFECTS[name].label.toLowerCase() + ' (' + EFFECTS[name].key + ')', 'effect:' + name, !!effects[name])));
        const item = current();
        menu.querySelector('[data-action="up"]').setAttribute('aria-pressed', String(!!item && item.vote === 1));
        menu.querySelector('[data-action="down"]').setAttribute('aria-pressed', String(!!item && item.vote === -1));
        menu.querySelector('.menu-score').textContent = item ? item.score : '';
        menu.querySelector('[data-action="mute"]').textContent = muted ? '🔇 sound off' : '🔊 sound on';
        menu.querySelector('.menu-watching').textContent = watching
            ? '👁 ' + watching.watching + ' watching now, ' + watching.channel_watching + ' on this channel' : '';
    }

    function toggleMenu(open) {
        const show = open === undefined ? menu.hidden : open;
        menu.hidden = !show;
        if (show) {
            renderMenu();
        }
    }

    function onMenuClick(event) {
        const target = event.target.closest('button');
        if (!target) {
            return;
        }
        unlockAudio();
        const [action, arg] = target.dataset.action.split(':');
        const actions = {
            channel: () => setChannel(arg),
            effect: () => setEffect(arg, !effects[arg]),
            prev: previousClip,
            next: nextClip,
            up: () => vote(1),
            down: () => vote(-1),
            share,
            mute: toggleMute,
            fullscreen: toggleFullscreen,
            close: () => toggleMenu(false),
        };
        if (actions[action]) {
            actions[action]();
        }
    }

    // --- input -----------------------------------------------------------

    function onKey(event) {
        if (event.ctrlKey || event.metaKey || event.altKey) {
            return;
        }
        const key = event.key;
        if (/^[0-9]$/.test(key)) {
            tuneNumber(Number(key));
        } else if (key === ' ' || key === 'ArrowRight' || key === 'n') {
            nextClip();
        } else if (key === 'ArrowLeft' || key === 'p') {
            previousClip();
        } else if (key === 'ArrowUp') {
            stepChannel(1);
        } else if (key === 'ArrowDown') {
            stepChannel(-1);
        } else if (key === '+' || key === '=') {
            vote(1);
        } else if (key === '-' || key === '_') {
            vote(-1);
        } else if (key === 'c') {
            share();
        } else if (key === 'm') {
            toggleMute();
        } else if (key === 'f') {
            toggleFullscreen();
        } else if (key === 'i') {
            showOSD();
        } else if (key === 'e') {
            toggleAllEffects();
        } else if (key === '?' || key === 'h') {
            toggleMenu();
        } else if (key === 'Escape') {
            toggleMenu(false);
        } else {
            const name = Object.keys(EFFECTS).find((n) => EFFECTS[n].key === key);
            if (!name) {
                return;
            }
            setEffect(name, !effects[name]);
        }
        event.preventDefault();
        unlockAudio();
    }

    function onScreenClick() {
        unlockAudio();
        if (!menu.hidden) {
            toggleMenu(false);
            return;
        }
        nextClip();
    }

    // swipe up/down changes channel, left/right skips clips
    function setupSwipe(el) {
        let start = null;
        el.addEventListener('touchstart', (event) => {
            const t = event.changedTouches[0];
            start = { x: t.clientX, y: t.clientY };
        }, { passive: true });
        el.addEventListener('touchend', (event) => {
            if (!start) {
                return;
            }
            const t = event.changedTouches[0];
            const dx = t.clientX - start.x, dy = t.clientY - start.y;
            start = null;
            if (Math.max(Math.abs(dx), Math.abs(dy)) < SWIPE_PX) {
                return;
            }
            event.preventDefault();
            unlockAudio();
            if (Math.abs(dy) > Math.abs(dx)) {
                stepChannel(dy < 0 ? 1 : -1);
            } else if (dx < 0) {
                nextClip();
            } else {
                previousClip();
            }
        });
    }

    // --- startup ---------------------------------------------------------

    async function loadChannels() {
        try {
            channels = await api('/api/channels');
        } catch (e) {
            channels = [{ number: 1, slug: 'mix', name: 'WEIRD MIX', live: false }];
        }
    }

    async function init() {
        if (initialized) {
            return;
        }
        initialized = true;
        canvas = document.querySelector('.tv');
        crtCanvas = document.querySelector('.crt');
        osd = document.querySelector('.osd');
        offairEl = document.querySelector('.offair-message');
        autoplayOverlay = document.querySelector('.autoplay');
        menu = document.querySelector('.menu');
        vhs = document.querySelector('.vhs');

        try {
            context = canvas.getContext('2d');
        } catch (e) {
            context = null;
        }
        if (!context) {
            document.querySelector('.alt').style.display = 'block';
            return;
        }
        crt = createCRT(crtCanvas);

        testcardImage = new Image();
        testcardImage.src = TESTCARD_URL;
        testcardCanvas = document.createElement('canvas');
        testcardCanvas.width = 400;
        testcardCanvas.height = 225;
        testcardContext = testcardCanvas.getContext('2d', { willReadFrequently: true });
        tempCanvas = document.createElement('canvas');
        tempCanvas.width = testcardCanvas.width;
        tempCanvas.height = testcardCanvas.height;
        tempContext = tempCanvas.getContext('2d');
        testcardImage.onload = () => testcardContext.drawImage(testcardImage, 0, 0, testcardCanvas.width, testcardCanvas.height);

        video = createVideo();
        spare = createVideo();

        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (AudioContextClass) {
            audioContext = new AudioContextClass();
            loadAudio(STATIC_SOUND);
        }

        window.addEventListener('resize', resizeCanvas);
        document.addEventListener('keydown', onKey);
        canvas.addEventListener('click', onScreenClick);
        canvas.addEventListener('contextmenu', (event) => event.preventDefault());
        setupSwipe(canvas);
        autoplayOverlay.addEventListener('click', unlockAudio);
        menu.addEventListener('click', onMenuClick);
        document.querySelector('.menu-button').addEventListener('click', () => {
            unlockAudio();
            toggleMenu();
        });

        resizeCanvas();
        requestAnimationFrame(draw);
        setInterval(renderVHS, 1000);

        await loadChannels();
        const initial = window.WEIRDTV_INITIAL;
        const saved = storageGet(STORAGE.channel);
        channel = !initial && channels.some((c) => c.slug === saved) ? saved
            : (channels.find((c) => !c.live) || channels[0]).slug;
        applyEffects();
        heartbeat();
        setInterval(heartbeat, HEARTBEAT_MS);

        if (initial) {
            // opened from a shared /v/<id> link
            showStatic();
            pushHistory(initial);
            tune(initial);
        } else if (isLive()) {
            liveSyncID = setInterval(syncLive, LIVE_SYNC_MS);
            tuneLive();
        } else {
            nextClip();
        }
    }

    return {
        init, next: nextClip, previous: previousClip, mute: toggleMute, channel: setChannel,
        setEffect, effects: () => Object.assign({}, effects),
        state: () => ({
            channel, item: current(), offair, loading: videoLoading,
            upcoming: upcoming && upcoming.item.id, time: video && video.currentTime,
        }),
    };
})();

document.addEventListener('DOMContentLoaded', TV.init);
