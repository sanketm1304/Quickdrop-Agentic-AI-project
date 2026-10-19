FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN groupadd --system quickdrop && useradd --system --gid quickdrop --home-dir /app quickdrop
COPY pyproject.toml README.md ./
COPY app ./app
COPY mockpayswift ./mockpayswift
COPY scripts ./scripts
COPY evals ./evals
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
RUN pip install --no-cache-dir . && mkdir -p /data && chown -R quickdrop:quickdrop /app /data

USER quickdrop
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

