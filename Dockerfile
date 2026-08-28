# sspc-operator image (operator + embedded UI). Build context: repo root
FROM node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui .
RUN npm run build

FROM rust:1.92-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends cmake perl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY --from=ui /ui/dist ./ui/dist
RUN cargo build --release --bin sspc-operator

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/sspc-operator /usr/local/bin/sspc-operator
USER 65534
ENTRYPOINT ["/usr/local/bin/sspc-operator"]
