FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN uv sync --no-cache

COPY . .

ENTRYPOINT ["uv", "run", "uvicorn", "main:app"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--reload"]
