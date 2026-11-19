# DarGlobal & Wasalt property assistant
# Build:  docker build -t property-assistant .
# Run:    docker run -p 7860:7860 --env-file .env property-assistant   ->  http://localhost:7860
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860

# Hugging Face Spaces runs containers as user 1000
RUN useradd -m -u 1000 appuser
WORKDIR /home/appuser/app

COPY app/requirements.txt app/requirements.txt
RUN pip install -r app/requirements.txt

# only the app and the cleaned knowledge base go into the image (no scrapers, no raw HTML)
COPY --chown=appuser:appuser app/ app/
COPY --chown=appuser:appuser data/processed/knowledge.jsonl data/processed/knowledge.jsonl

USER appuser
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",\"7860\")}/health')"

WORKDIR /home/appuser/app/app
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
