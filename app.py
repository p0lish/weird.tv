from flask import Flask, Response, render_template, send_from_directory
from channer import Channer
API_URL = "http://api.4chan.org"
IMAGES_URL = "http://i.4cdn.org"
USERAGENTS = [
    "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/41.0.2228.0 Safari/537.36",
    "Mozilla/5.0 (Windows; U; Windows NT 6.1; rv:2.2) Gecko/20110201"
]

app = Flask(__name__)


@app.route('/')
def tv():
    return render_template('index.html')


@app.route('/getvideo/')
def vidlist():
    return send_from_directory('static', 'videos.json')


@app.route('/___update/')
def update():
    channer = Channer()
    data = channer.updateVids()
    return "update finished {} vids found.".format(data["size"])


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
