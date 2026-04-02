FROM python:3.14-alpine

COPY docker_entrypoint.sh /

COPY requirements.txt ./
RUN apk add --no-cache --virtual .build-deps gcc g++ make libffi-dev openssl-dev && \
    apk add --no-cache ca-certificates ffmpeg && \
    pip install --no-cache-dir -r requirements.txt && \
    apk del .build-deps && \
    addgroup -S v380 && \
    adduser -S v380 -G v380

USER v380

COPY . /app
WORKDIR /app

ENTRYPOINT ["/docker_entrypoint.sh"]
