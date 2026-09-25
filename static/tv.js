const TV = (() => {
    const TESTCARD_URL = '/static/testcard.png';
    const STATIC_SOUND = 'st';
    // skip a video if it hasn't started playing after this long
    const LOAD_TIMEOUT_MS = 15000;
    // how long the channel info stays on screen
    const OSD_MS = 4000;
    const HISTORY_SIZE = 50;

    let canvas, context, video, osd, autoplayOverlay;
    let testcardImage, testcardCanvas, testcardContext, tempCanvas, tempContext;
    let testcardMod = 0, pixelOffset1 = 1, pixelOffset2 = 1, blockOffset = 2;
    let drawRect = { x: 0, y: 0, w: 0, h: 0 };
    let videoLoading = true, loadTimeoutID, osdTimeoutID, playTimeoutID;
    let audioContext, audio = {};
    let muted = false;
    let channel = 0;
    // previously watched videos; `cursor` points at the one on screen
    let history = [], cursor = -1;
    let requestID = 0;
    let initialized = false;

    function resizeCanvas() {
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
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
        playAudio(STATIC_SOUND);
    }

    async function fetchNext() {
        const response = await fetch('/api/next', { cache: 'no-store' });
        if (!response.ok) {
            throw new Error('no video available (' + response.status + ')');
        }
        return response.json();
    }

    function tune(item) {
        clearTimeout(loadTimeoutID);
        clearTimeout(playTimeoutID);
        video.pause();
        video.src = item.src;
        video.load();
        loadTimeoutID = setTimeout(nextChannel, LOAD_TIMEOUT_MS);
    }

    async function nextChannel() {
        const id = ++requestID;
        showStatic();
        if (cursor < history.length - 1) {
            // we went back earlier, so walk forward through history first
            cursor++;
            channel++;
            tune(history[cursor]);
            return;
        }
        let item;
        try {
            item = await fetchNext();
        } catch (e) {
            console.warn(e);
            loadTimeoutID = setTimeout(nextChannel, 5000);
            return;
        }
        // a newer channel change happened while we were waiting
        if (id !== requestID) {
            return;
        }
        history.push(item);
        if (history.length > HISTORY_SIZE) {
            history.shift();
        }
        cursor = history.length - 1;
        channel++;
        tune(item);
    }

    function previousChannel() {
        if (cursor <= 0) {
            return;
        }
        requestID++;
        showStatic();
        cursor--;
        channel = Math.max(1, channel - 1);
        tune(history[cursor]);
    }

    function current() {
        return history[cursor];
    }

    function showOSD() {
        const item = current();
        if (!item) {
            return;
        }
        const number = String(channel).padStart(2, '0');
        osd.querySelector('.osd-channel').textContent = 'CH ' + number + (muted ? '  🔇' : '');
        const title = osd.querySelector('.osd-title');
        title.textContent = item.title || item.filename || '';
        const source = osd.querySelector('.osd-source');
        if (item.source) {
            source.href = item.source;
            source.textContent = '/' + item.board + '/';
            source.style.display = '';
        } else {
            source.style.display = 'none';
        }
        osd.classList.add('visible');
        clearTimeout(osdTimeoutID);
        osdTimeoutID = setTimeout(() => osd.classList.remove('visible'), OSD_MS);
    }

    function drawTestcard() {
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

    function draw() {
        if (videoLoading) {
            drawTestcard();
        } else {
            context.fillStyle = '#000';
            context.fillRect(0, 0, canvas.width, canvas.height);
            context.drawImage(video, drawRect.x, drawRect.y, drawRect.w, drawRect.h);
        }
        requestAnimationFrame(draw);
    }

    function playVideo() {
        clearTimeout(loadTimeoutID);
        videoLoading = false;
        fitVideo();
        video.muted = muted;
        video.play().then(showOSD).catch(() => {
            // browsers block unmuted autoplay until the user interacts with the page
            video.muted = true;
            video.play().then(() => {
                showOSD();
                autoplayOverlay.style.display = 'block';
            }).catch(() => {
                autoplayOverlay.style.display = 'block';
            });
        });
    }

    function unlockAudio() {
        autoplayOverlay.style.display = 'none';
        if (audioContext) {
            audioContext.resume();
        }
        video.muted = muted;
        video.play().catch(() => {});
    }

    function toggleMute() {
        muted = !muted;
        video.muted = muted;
        showOSD();
    }

    function toggleFullscreen() {
        if (document.fullscreenElement) {
            document.exitFullscreen();
        } else if (document.documentElement.requestFullscreen) {
            document.documentElement.requestFullscreen().catch(() => {});
        }
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
        if (!audioContext || !audio[name] || muted || audioContext.state !== 'running') {
            return;
        }
        const source = audioContext.createBufferSource();
        source.buffer = audio[name];
        source.connect(audioContext.destination);
        source.start(0);
    }

    function onKey(event) {
        if (event.ctrlKey || event.metaKey || event.altKey) {
            return;
        }
        switch (event.key) {
            case ' ':
            case 'ArrowRight':
            case 'ArrowUp':
            case 'n':
                nextChannel();
                break;
            case 'ArrowLeft':
            case 'ArrowDown':
            case 'p':
                previousChannel();
                break;
            case 'm':
                toggleMute();
                break;
            case 'f':
                toggleFullscreen();
                break;
            case 'i':
                showOSD();
                break;
            default:
                return;
        }
        event.preventDefault();
        unlockAudio();
    }

    function init() {
        if (initialized) {
            return;
        }
        initialized = true;
        canvas = document.querySelector('.tv');
        osd = document.querySelector('.osd');
        autoplayOverlay = document.querySelector('.autoplay');

        try {
            context = canvas.getContext('2d');
        } catch (e) {
            context = null;
        }
        if (!context) {
            document.querySelector('.alt').style.display = 'block';
            return;
        }

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

        video = document.createElement('video');
        video.playsInline = true;
        video.preload = 'auto';
        video.addEventListener('error', () => {
            if (video.getAttribute('src')) {
                nextChannel();
            }
        });
        video.addEventListener('ended', nextChannel);
        video.addEventListener('canplay', () => {
            if (videoLoading) {
                // hold the static for a moment, like a real channel change
                clearTimeout(playTimeoutID);
                playTimeoutID = setTimeout(playVideo, 600);
            }
        });
        video.addEventListener('loadedmetadata', fitVideo);

        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (AudioContextClass) {
            audioContext = new AudioContextClass();
            loadAudio(STATIC_SOUND);
        }

        window.addEventListener('resize', resizeCanvas);
        document.addEventListener('keydown', onKey);
        canvas.addEventListener('click', () => {
            unlockAudio();
            nextChannel();
        });
        canvas.addEventListener('contextmenu', (event) => event.preventDefault());
        autoplayOverlay.addEventListener('click', unlockAudio);

        resizeCanvas();
        testcardImage.onload = () => testcardContext.drawImage(testcardImage, 0, 0, testcardCanvas.width, testcardCanvas.height);
        draw();
        nextChannel();
    }

    return { init, next: nextChannel, previous: previousChannel, mute: toggleMute };
})();

document.addEventListener('DOMContentLoaded', TV.init);
