import os

bind = os.environ.get("HOST", "0.0.0.0") + ":" + os.environ.get("PORT", "8088")
# One worker so the scraper and archiver threads run only once; video
# streaming is I/O bound, so threads handle the concurrency.
workers = 1
threads = int(os.environ.get("WEB_THREADS", "32"))
timeout = 120
accesslog = "-"
