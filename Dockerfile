FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WEIRDTV_DATA_DIR=/data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --system --home /app weirdtv && mkdir -p /data && chown weirdtv /data
USER weirdtv

VOLUME /data
EXPOSE 8088
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
