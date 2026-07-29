FROM python:3.12-slim-bookworm

# Версия imapsync. Пока не закреплена на теге: точный работающий тег
# фиксируем на этапе 2, когда перенос будет проверен на живых серверах.
ARG IMAPSYNC_REF=master

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

# Perl-зависимости imapsync. Список — из INSTALL.d самого imapsync,
# минус то, что нужно только для сборки бинарников (par-packer, scandeps).
RUN apt-get update && apt-get install -y --no-install-recommends \
        perl \
        ca-certificates \
        curl \
        libauthen-ntlm-perl \
        libcgi-pm-perl \
        libcrypt-openssl-rsa-perl \
        libdata-uniqid-perl \
        libencode-imaputf7-perl \
        libfile-copy-recursive-perl \
        libfile-tail-perl \
        libio-socket-inet6-perl \
        libio-socket-ssl-perl \
        libio-tee-perl \
        libhtml-parser-perl \
        libjson-webtoken-perl \
        libmail-imapclient-perl \
        libparse-recdescent-perl \
        libreadonly-perl \
        libregexp-common-perl \
        libsys-meminfo-perl \
        libterm-readkey-perl \
        libtest-mockobject-perl \
        libunicode-string-perl \
        liburi-perl \
        libwww-perl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://raw.githubusercontent.com/imapsync/imapsync/${IMAPSYNC_REF}/imapsync" \
        -o /usr/local/bin/imapsync \
    && chmod +x /usr/local/bin/imapsync \
    && imapsync --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x /app/docker-entrypoint.sh

VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]

# workers=1 — принципиально: при нескольких воркерах поток-супервизор
# поднимется в каждом и один ящик поедет мигрировать в нескольких копиях.
# Дополнительно от этого защищает файловый лок в приложении.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", \
     "--timeout", "120", "--access-logfile", "-", "app.wsgi:app"]
