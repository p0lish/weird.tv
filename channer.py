import json
import random
import socket

import requests
from requests import HTTPError


class Channer:
    POSSIBLE_ERRORS = (HTTPError,
                       socket.error,
                       requests.exceptions.InvalidSchema,
                       requests.exceptions.ConnectionError)

    vids = []
    API_URL = "http://api.4chan.org"
    IMAGES_URL = "http://i.4cdn.org"
    USERAGENTS = [
        "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/41.0.2228.0 Safari/537.36",
        "Mozilla/5.0 (Windows; U; Windows NT 6.1; rv:2.2) Gecko/20110201"
    ]

    def _get_image_url(self, thread, filename, extension):
        return self.IMAGES_URL + "/" + thread + "/" + filename + extension

    def _get_thread_url(self, thread, thread_no):
        return "/".join((self.API_URL, thread, "thread", str(thread_no) + ".json"))

    def _get_content(self, url):
        try:
            req = requests.get(
                url, headers={'User-Agent': random.choice(self.USERAGENTS)}, verify=False, timeout=20)
            if req.status_code == 200:
                return req.content
        except self.POSSIBLE_ERRORS:
            return ""
        except requests.exceptions.Timeout:
            return "timeout"

    def _get_thread_elements(self, thread):
        self.vids = []
        current_url = "/".join((self.API_URL, thread, "catalog.json"))
        content = json.loads(self._get_content(current_url).decode('utf-8'))
        for pages in content:
            for threads in pages['threads']:
                content = self._get_content(self._get_thread_url(thread, threads["no"]))
                if content is not None:
                    thread_content = json.loads(content.decode('utf-8'))
                    for post in thread_content['posts']:
                        if "tim" in post:
                            image_url = self._get_image_url(thread, str(post["tim"]), post["ext"])
                            if post["ext"] == ".webm":
                                self.vids.append(image_url)

    def getRandomVideo(self):
        if self.vids:
            return self.vids.pop()
        else:
            self.vids = self.updateVids()
            random.shuffle(self.vids)
            return self.vids.pop()

    def updateVids(self):
        self._get_thread_elements("wsg")
        filename = "static/videos.json"
        videos = {"videos": self.vids}
        with open(filename, "w") as f:
            f.write(json.dumps(videos))
        data = {"size": len(self.vids)}
        return data

