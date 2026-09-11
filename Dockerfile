FROM python:3.14.4-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system app \
    && adduser --system --home /home/app --ingroup app app \
    && install -d -o app -g app /home/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod -R a-w /app \
    && chmod 0555 /app/deploy/app-entrypoint.sh

USER app
EXPOSE 8000

ENTRYPOINT ["/app/deploy/app-entrypoint.sh"]
