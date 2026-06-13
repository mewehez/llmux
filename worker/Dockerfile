FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml ./
RUN uv sync --no-cache

COPY . .

ENTRYPOINT ["uv", "run", "python", "main.py"]
