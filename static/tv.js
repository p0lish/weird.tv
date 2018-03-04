window.requestAnimationFrame = window.requestAnimationFrame || window.webkitRequestAnimationFrame || window.mozRequestAnimationFrame || window.msRequestAnimationFrame || function (callback) {
    setTimeout(callback, 1000 / 60);
};
var TV = (function () {
    var TV = {},
        TV_WIDTH = 800,
        TV_HEIGHT = 450,
        video, videoWidth = TV_WIDTH,
        videoHeight = TV_HEIGHT,
        videoLoading = true,
        videoTimeoutID, radius = 10,
        canvas, canvasWidth = TV_WIDTH,
        canvasHeight = TV_HEIGHT,
        canvasTop = 0,
        testcardImage, testcardCanvas, testcardContext, tempCanvas, tempContext, testcardMod = 0,
        pixelOffset1, pixelOffset2, blockOffset, audio = {},
        audioContext, init = false;

    function getVideo() {
        shuffle(TV.playlist);
        if (TV.playlist === undefined) {
            setInterval(function(){
             getVideo();
            }, 100);
        } else {
            if (TV.playlist.length > 1) {
                return TV.playlist.pop();
            } else {
                return TV.playlist[0];
            }
        }
    }

    function shuffle(a) {
        if (a === undefined) {
            return null;
        }
        for (var i = a.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [a[i], a[j]] = [a[j], a[i]];
        }
    }

    function getCanvasRatio() {
        var body = document.body,
            html = document.documentElement,
            width = Math.max(body.scrollWidth, body.offsetWidth, html.clientWidth, html.scrollWidth, html.offsetWidth),
            height = Math.max(body.scrollHeight, body.offsetHeight, html.clientHeight, html.scrollHeight, html.offsetHeight);
        return width / height;
    }

    function resizeCanvas() {
        var canvasRatio = getCanvasRatio(),
            videoRatio = Math.max(videoWidth / videoHeight, Math.min(canvasRatio, 1.5));
        canvasWidth = canvas.width;
        canvasHeight = canvas.height * getCanvasRatio() / videoRatio;
        canvasTop = (canvas.height - canvasHeight) / 2;
    }

    function getVideoSource() {
        var ch = 'getvideo';
        fetch(window.location.href + ch)
            .then(function (response) {
                var contentType = response.headers.get("content-type");
                if (contentType && contentType.indexOf("application/json") !== -1) {
                    return response.json().then(function (json) {
                        TV.playlist = json.videos;
                    })
                }
            })
            .catch(function (error) {
                console.error(error);
            });
    }

    function loadNextVideo() {
        videoLoading = true;
        testcardContext.drawImage(testcardImage, 0, 0, testcardCanvas.width, testcardCanvas.height);
        pixelOffset1 = Math.floor(Math.random() * 3) + 1;
        pixelOffset2 = Math.floor(Math.random() * 3) + 1;
        blockOffset = Math.floor(Math.random() * 150) + 2;
        playAudio('st');
        video.src = getVideo();
        ga('send', 'event', 'Video', video.src);
    }

    function drawVideo() {
        var lineOffset = Math.round((Math.random() - 0.5) * 6);
        if (videoLoading) {
            testcardMod++;
            testcardMod %= 3;
            if (testcardMod === 0) {
                var imageData = testcardContext.getImageData(0, 0, testcardCanvas.width, testcardCanvas.height),
                    pixels = imageData.data,
                    length = pixels.length,
                    offset, i = 0,
                    x, y;
                for (y = 0; y < testcardCanvas.height; y++) {
                    offset = (y % ((Math.random() * blockOffset) | 0) === 0) ? ((Math.random() * lineOffset * lineOffset) | 0) : offset;
                    for (x = 0; x < testcardCanvas.width; x++) {
                        i += 4;
                        pixels[i] = pixels[i + pixelOffset1 * (offset + lineOffset * lineOffset)];
                        pixels[i + pixelOffset1] = pixels[i + pixelOffset2 + 4 * (offset * lineOffset)];
                    }
                }
                tempContext.putImageData(imageData, 0, 0);
                context.clearRect(0, 0, canvas.width, canvas.height);
                context.drawImage(tempCanvas, 0, 0, canvas.width, canvas.height);
            }
        } else {
            context.clearRect(0, 0, canvas.width, canvas.height);
            try {
                context.drawImage(video, 0, canvasTop, canvasWidth, canvasHeight);
            } catch (e) {}
        }
        requestAnimationFrame(drawVideo);
    }

    function playVideo() {
        videoLoading = false;
        videoWidth = video.videoWidth || TV_WIDTH;
        videoHeight = video.videoHeight || TV_HEIGHT;
        resizeCanvas();
        var promise = video.play();
        if (promise !== undefined) {
            promise.catch(function (error) {
                var play = document.querySelector('.autoplay');
                play.style.display = 'block';
                play.onmousedown = function (event) {
                    play.style.display = 'none';
                   playVideo();
                };
            });
        }
    }

    function loadAudio(name) {
        var xhr = new XMLHttpRequest();
        xhr.open('GET', './static/' + name + '.mp3', true);
        xhr.responseType = 'arraybuffer';
        xhr.onload = function () {
            audioContext.decodeAudioData(xhr.response, function (buffer) {
                audio[name] = buffer;
            }, function () {
                ("error")
            });
        };
        xhr.send();
    }

    function playAudio(name) {
        if (!audio[name]) {
            return;
        }
        audioContext.resume();
        var source = audioContext.createBufferSource();
        source.buffer = audio[name];
        source.connect(audioContext.destination);
        if (source.start) {
            source.start(0);
        } else {
            source.noteOn(0);
        }
    }
    TV.init = function () {
        if (init) {
            return;
        }
        init = true;
        canvas = document.querySelector('.tv');
        getVideoSource();
        testcardImage = new Image();
        testcardImage.src = testcard;
        window.AudioContext = window.AudioContext || window.webkitAudioContext;
        if (window.AudioContext) {
            audioContext = new AudioContext();
            loadAudio('st');
        }
        video = document.createElement('video');
        video.autoPlay = false;
        video.loop = false;
        video.muted = false;
        video.oncanplaythrough = function () {
            clearTimeout(videoTimeoutID);
            videoTimeoutID = setTimeout(playVideo, 200);
        };
        video.onstalled = video.onerror = video.onended = loadNextVideo;
        video.load();
        window.onresize = resizeCanvas;
        canvas.width = TV_WIDTH;
        canvas.height = TV_HEIGHT;
        canvas.oncontextmenu = function (event) {
            event.preventDefault();
        };
        testcardCanvas = document.createElement('canvas');
        testcardCanvas.width = canvas.width / 2;
        testcardCanvas.height = canvas.height / 2;
        tempCanvas = document.createElement('canvas');
        tempCanvas.width = canvas.width / 2;
        tempCanvas.height = canvas.height / 2;
        var alt = document.querySelector('.alt');
        try {
            context = canvas.getContext('2d');
            tempContext = tempCanvas.getContext('2d');
            testcardContext = testcardCanvas.getContext('2d');
        } catch (e) {
            alt.style.display = 'block';
            return;
        }
        canvas.onmousedown = loadNextVideo;
        if (document.body.className === 'disabled') {
            alt.style.display = 'block';
            TV.playlist = [];
        }
        drawVideo();
        clearTimeout(videoTimeoutID);
        videoTimeoutID = setTimeout(loadNextVideo, 100);
    };
    return TV;
})();
document.addEventListener('DOMContentLoaded', TV.init);