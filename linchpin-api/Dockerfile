FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY . .
RUN pip install --no-cache-dir -e .

# Apply database migrations before starting the server.
# `alembic upgrade head` is idempotent and a no-op when up-to-date.
# `exec` replaces the shell with uvicorn so SIGTERM from `compose stop`
# propagates cleanly to the process.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
