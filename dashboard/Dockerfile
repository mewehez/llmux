FROM node:22-alpine AS base
RUN npm install -g pnpm
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .

FROM base AS dev
ENTRYPOINT ["pnpm"]
CMD ["dev", "--host", "0.0.0.0", "--port", "5173"]

FROM base AS builder
ARG VITE_API_URL=http://localhost:30000
RUN VITE_API_URL=$VITE_API_URL pnpm build

FROM node:22-alpine AS production
RUN npm install -g serve
WORKDIR /app
COPY --from=builder /app/dist ./dist
ENTRYPOINT ["serve"]
CMD ["-s", "dist", "-l", "5173"]