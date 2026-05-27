FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    GROK_IMAGINE_ARCHIVE_ROOT=/data/archive

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

VOLUME ["/data"]
EXPOSE 7860

CMD ["grok-imagine-archive", "web", "--account", "demo", "--host", "0.0.0.0", "--port", "7860"]
