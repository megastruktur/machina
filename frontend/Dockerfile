# syntax=docker/dockerfile:1
# mai-web image (card 01-compose): SvelteKit adapter-node SSR, Compose-internal
# (windowed-workspace ADR D4 / svelte.config.js). The node CLI pins live in
# frontend/package.json (engines >=22, packageManager pnpm@12.6.0); corepack
# activates exactly that pnpm version from packageManager — no second pin here.

FROM node:22-slim AS build
RUN corepack enable
WORKDIR /app
ENV CI=1
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM node:22-slim
ENV NODE_ENV=production \
    PORT=3000
WORKDIR /app
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/build ./build
# Non-root: the stock `node` user (uid 1000).
USER node
EXPOSE 3000
CMD ["node", "build"]
