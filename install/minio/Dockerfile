# Build the historical demo object store without deleted upstream image artifacts.
FROM golang:1.19.13-bullseye@sha256:2fdfcb03b1445f06f1cf8a342516bfd34026b527fef8427f40ea7b140168fda2 AS build
ARG COMPONENT
ARG COMMIT
ARG SOURCE_SHA256
WORKDIR /src
RUN curl --fail --location --retry 3 "https://codeload.github.com/minio/${COMPONENT}/tar.gz/${COMMIT}" -o /tmp/source.tar.gz \
    && echo "${SOURCE_SHA256}  /tmp/source.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/source.tar.gz --strip-components=1 \
    && CGO_ENABLED=0 GOMAXPROCS=2 go build -mod=readonly -trimpath -o /out/program .

# The existing pinned busybox provides the shell used by the bucket setup Job.
FROM busybox:1.36@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662
ARG COMPONENT
ARG COMMIT
COPY --from=build /out/program /usr/bin/${COMPONENT}
RUN ln -s /usr/bin/${COMPONENT} /usr/bin/program
ENTRYPOINT ["/usr/bin/program"]
COPY --from=build /src/LICENSE /usr/share/licenses/minio/LICENSE
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
LABEL org.opencontainers.image.source="https://github.com/minio/${COMPONENT}" \
      org.opencontainers.image.revision="${COMMIT}"
