# Pinned demo object-store builds

The legacy Kubernetes demo uses MinIO; the native stack's storage configuration
is unchanged. The upstream MinIO and mc registry pins became unavailable in
September 2026 ([#92](https://github.com/supabricks/platform/issues/92)).

`python3 install/minio/build.py` builds local images from the exact commits and
SHA-256-verified source archives in `sources.json`. `install/up.sh` runs it before
loading images into kind. Docker's layer cache avoids recompiling unchanged input.
The Go builder and BusyBox runtime are pinned to multi-platform image digests;
Go uses the upstream go.mod/go.sum with read-only module resolution. The source
licenses are included in the images. No images are published by this script.

The server retains RELEASE.2022-10-20T00-55-09Z. The client is pinned to the
companion RELEASE.2022-10-20T23-26-33Z; its alias/bucket and S3 compatibility are
checked by the real installer, end-to-end, chaos and cold-restore CI gates.
These historical versions are for the existing local demo, not a new production
object-store recommendation or a security upgrade.

The chart's `source-sha256:` annotations identify verified source archives, not
OCI image digests. The component inventory must match these identities. Images
also carry upstream source/revision and Dockerfile-hash labels. The installer
compares actual image IDs before skipping a kind load, including rebuilt local
images. Source archive retrieval and module download require network access on
an uncached build. A failed source hash or build aborts installation.
