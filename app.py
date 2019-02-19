from flask import Flask, Response, render_template, send_from_directory, jsonify, send_file
import json
import os
import io
import random
import urllib3
import requests
from channer import Channer
API_URL = "http://api.4chan.org"
IMAGES_URL = "http://i.4cdn.org"
USERAGENTS = [
    "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/41.0.2228.0 Safari/537.36",
    "Mozilla/5.0 (Windows; U; Windows NT 6.1; rv:2.2) Gecko/20110201"
]

app = Flask(__name__)
buff = None


def getVideoFile(buff):
    json_data=open('./static/videos.json').read()
    url = random.choice(json.loads(json_data)['videos'])
    filename = url.split('/')[-1]
    f = requests.get(url)
    return send_file(io.BytesIO(f.content),
                     attachment_filename='video.webm',
                     mimetype='video/webm')


@app.route('/')
def tv():
    return render_template('index.html')


@app.route('/video.webm')
def vidlist():
    global buff
    if buff:
        try: 
            os.remove(buff)
        except FileNotFoundError:
            pass
    return getVideoFile(buff)
    

    


@app.route('/___update/')
def update():
    channer = Channer()
    data = channer.updateVids()
    return "update finished {} vids found.".format(data["size"])


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8088)
