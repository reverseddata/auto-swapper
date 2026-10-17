FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/app/data LOG_DIR=/app/logs
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd -r -u 10001 sniper && mkdir -p data logs \
    && chown sniper data logs
USER sniper
CMD ["mm2sniper", "run"]
