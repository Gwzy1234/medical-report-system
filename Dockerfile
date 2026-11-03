FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY sql/ ./sql/
COPY scripts/ ./scripts/

RUN groupadd -g 10001 report && \
    useradd -u 10001 -g 10001 -r -s /usr/sbin/nologin report && \
    mkdir -p /app/data && chown -R 10001:10001 /app/data

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
