FROM python:3.12-slim

# Install Node.js for MCP server subprocess support (npx)
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY . .
RUN pip install --no-cache-dir -e .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
