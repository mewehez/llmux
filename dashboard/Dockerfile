FROM node:22-alpine

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN npm install -g pnpm

WORKDIR /app

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .

ENTRYPOINT ["pnpm"]
CMD ["dev", "--host", "0.0.0.0", "--port", "5173"]
