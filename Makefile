#
# Makefile for Docker Volume Manager
# https://github.com/lpgonzalez/docker-volume-manager
#
# Quick reference:
#   make build-prod                                 # build runtime image
#   make run-backup backup-file-name=foo            # backup ./in_dir -> ./out_dir
#   make run-backup input-volume=mydata \
#        backup-file-name=mydata-bak                # backup a Docker volume
#   make run-backup-parity encryption-key=secret \
#        backup-file-name=foo parity=30             # encrypted + PAR2
#   make run-interactive                            # launch the wizard
#   make run-volumes-list size=1                    # list volumes with sizes
#   make test                                       # build + run pytest suite
#
# Any lowercase-hyphen Makefile variable can be overridden at the CLI:
#   make run-backup compression=HIGH parity=30 encryption-key=secret tz=UTC
#

##############################################################################
# Variables
##############################################################################

image-name       = docker_volume_manager
image-version    = 3.1
container        = docker-volume-manager

# Public release (Docker Hub). Override docker-user / release-version as needed.
docker-user      = lpgonzalez
dockerhub-repo   = docker-volume-manager
dockerhub-image  = $(docker-user)/$(dockerhub-repo)
release-version  = $(image-version).0
platforms        = linux/amd64,linux/arm64

# Build-time metadata baked into the OCI image labels (see Dockerfile).
vcs-ref          = $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
build-date       = $(shell date -u +'%Y-%m-%dT%H:%M:%SZ')
build-args       = --build-arg VERSION=$(release-version) \
                   --build-arg VCS_REF=$(vcs-ref) \
                   --build-arg BUILD_DATE=$(build-date)

# Host <-> container path mappings
in-dir           = $(PWD)/in_dir
out-dir          = $(PWD)/out_dir
logs-dir         = $(PWD)/logs

# Default operation parameters (override at CLI)
backup-file-name = backup
compression      = ZSTD
parity           = 0

# Runtime config
tz               = Europe/Madrid
log-level        = INFO
log-output       = console,file,json_file

# Reusable fragments
tz-mounts        = -v /etc/timezone:/etc/timezone:ro -v /etc/localtime:/etc/localtime:ro
io-mounts        = -v "$(in-dir):/app/input_dir" -v "$(out-dir):/app/output_dir" -v "$(logs-dir):/app/logs"
docker-socket    = -v /var/run/docker.sock:/var/run/docker.sock
common-env       = -e TZ=$(tz) -e LOG_LEVEL=$(log-level) -e LOG_OUTPUT=$(log-output)

# Conditional fragments driven by make-variable presence
volume-args      = $(if $(input-volume),--input-volume $(input-volume),) $(if $(output-volume),--output-volume $(output-volume),)
needs-socket     = $(if $(or $(input-volume),$(output-volume)),$(docker-socket),)
enc-env          = $(if $(encryption-key),-e ENCRYPTION_KEY="$(encryption-key)",)
ts-env           = $(if $(timestamp),-e TIMESTAMP="$(timestamp)",)


##############################################################################
# Phony
##############################################################################

.PHONY: help build build-prod build-test test test-unit test-functional test-integration test-integration-slow test-integration-all coverage lint format \
        release release-dry buildx-create buildx-rm \
        run-backup run-backup-encrypt run-backup-parity \
        run-restore run-verify run-copy run-rename run-interactive \
        run-volumes-list run-volumes-inspect run-volumes-create run-volumes-remove \
        run-devel run-devel-test \
        stop clean bash update init \
        push-to-repo save-image load-image


##############################################################################
# Help
##############################################################################

help:
	@echo "Build / test:"
	@echo "  build-prod                               # build $(image-name):$(image-version)"
	@echo "  build-test                               # build $(image-name):$(image-version)-test"
	@echo "  test                                     # unit + functional (no Docker daemon needed)"
	@echo "  test-unit / test-functional              # single tier"
	@echo "  test-integration                         # integration tier, fast (mounts docker.sock)"
	@echo "  test-integration-slow                    # heavy integration: full helper pipelines"
	@echo "  test-integration-all                     # both combined"
	@echo "  coverage                                 # term-missing coverage (unit + functional)"
	@echo "  lint                                     # ruff check (no changes)"
	@echo "  format                                   # ruff format + ruff check --fix (writes)"
	@echo ""
	@echo "Release (multi-arch amd64+arm64 via buildx; needs docker login):"
	@echo "  release      release-version=3.0.0       # build + push to $(dockerhub-image)"
	@echo "  release-dry  release-version=3.0.0       # multi-arch build, no push"
	@echo ""
	@echo "Backup / restore / verify / copy"
	@echo "  Shared vars: backup-file-name=, compression=(NONE|GZ|ZSTD), parity=,"
	@echo "               encryption-key=, input-volume=, output-volume=, timestamp="
	@echo "  GPG asymmetric: pass -e GPG_RECIPIENTS=alice@x,bob@y and mount ~/.gnupg"
	@echo "  run-backup                               # plain backup"
	@echo "  run-backup-encrypt encryption-key=..."
	@echo "  run-backup-parity  encryption-key=... [parity=30]"
	@echo "  run-restore        backup-file-name=... [timestamp=... encryption-key=...]"
	@echo "  run-verify         backup-file-name=... [encryption-key=...]"
	@echo "  run-copy           [overwrite=N]"
	@echo ""
	@echo "Rename a Docker volume (requires docker.sock):"
	@echo "  run-rename source=<src> target=<dst> [keep-source=1 force=1 yes=1]"
	@echo ""
	@echo "Interactive:"
	@echo "  run-interactive                          # wizard (-it + docker.sock)"
	@echo ""
	@echo "Docker volume management (requires docker.sock):"
	@echo "  run-volumes-list       [size=1 orphans=1]"
	@echo "  run-volumes-inspect    name=NAME"
	@echo "  run-volumes-create     name=NAME"
	@echo "  run-volumes-remove     name=NAME [force=1 yes=1]"
	@echo ""
	@echo "Dev / lifecycle / distribution:"
	@echo "  run-devel / run-devel-test / bash / stop / clean"
	@echo "  update / init / push-to-repo / save-image / load-image"


##############################################################################
# Build
##############################################################################

build: build-prod

# Builds for the host architecture. OCI labels live in the Dockerfile and are
# populated from the build-args below.
build-prod:
	docker build --target runtime $(build-args) \
		--tag $(image-name):$(image-version) \
		--tag $(image-name):latest \
		.

build-test:
	docker build --target test \
		--tag $(image-name):$(image-version)-test \
		--label org.opencontainers.image.title="$(image-name)-test" \
		--label org.opencontainers.image.version="$(image-version)" \
		.

##############################################################################
# Multi-arch release to Docker Hub (amd64 + arm64 via buildx).
# CI does this automatically on a v*.*.* tag (.github/workflows). These targets
# are for manual / dry-run releases. Requires `docker login` first.
##############################################################################

# One-off: create a buildx builder that supports multi-arch (qemu-driven).
buildx-create:
	docker buildx create --name dvm-builder --use --bootstrap || \
		docker buildx use dvm-builder

buildx-rm:
	docker buildx rm dvm-builder || echo "Builder not present, nothing to remove."

# Build + push the multi-arch image to Docker Hub, tagged with the release
# version and `latest`. Example: make release release-version=3.0.0
release: buildx-create
	docker buildx build --target runtime $(build-args) \
		--platform $(platforms) \
		--tag $(dockerhub-image):$(release-version) \
		--tag $(dockerhub-image):latest \
		--push \
		.

# Same as `release` but builds without pushing (validates the multi-arch build).
release-dry: buildx-create
	docker buildx build --target runtime $(build-args) \
		--platform $(platforms) \
		--tag $(dockerhub-image):$(release-version) \
		.

test: build-test
	docker run --rm --name=$(container)-test \
		-e PYTHONPATH=/app \
		$(image-name):$(image-version)-test \
		pytest /app/tests -m "not integration"

test-unit: build-test
	docker run --rm --name=$(container)-test \
		-e PYTHONPATH=/app \
		$(image-name):$(image-version)-test \
		pytest /app/tests -m unit

test-functional: build-test
	docker run --rm --name=$(container)-test \
		-e PYTHONPATH=/app \
		$(image-name):$(image-version)-test \
		pytest /app/tests -m functional

test-integration: build-prod build-test
	docker run --rm --name=$(container)-test \
		-v /var/run/docker.sock:/var/run/docker.sock \
		-e PYTHONPATH=/app \
		-e DVM_HELPER_IMAGE=$(image-name):$(image-version) \
		$(image-name):$(image-version)-test \
		pytest /app/tests -m "integration and not slow"

test-integration-slow: build-prod build-test
	docker run --rm --name=$(container)-test \
		-v /var/run/docker.sock:/var/run/docker.sock \
		-e PYTHONPATH=/app \
		-e DVM_HELPER_IMAGE=$(image-name):$(image-version) \
		$(image-name):$(image-version)-test \
		pytest /app/tests -m "integration and slow"

test-integration-all: build-prod build-test
	docker run --rm --name=$(container)-test \
		-v /var/run/docker.sock:/var/run/docker.sock \
		-e PYTHONPATH=/app \
		-e DVM_HELPER_IMAGE=$(image-name):$(image-version) \
		$(image-name):$(image-version)-test \
		pytest /app/tests -m integration

coverage: build-test
	docker run --rm --name=$(container)-test \
		-e PYTHONPATH=/app \
		$(image-name):$(image-version)-test \
		pytest /app/tests -m "not integration" \
		--cov=/app --cov-report=term-missing

# Lint (read-only) and format. Both mount the repo so ruff sees pyproject.toml
# and writes back to the host tree; -u keeps edited files owned by the caller.
lint: build-test
	docker run --rm --name=$(container)-lint \
		-u "$$(id -u):$$(id -g)" \
		-v "$(PWD):/work" -w /work \
		$(image-name):$(image-version)-test \
		ruff check app

format: build-test
	docker run --rm --name=$(container)-fmt \
		-u "$$(id -u):$$(id -g)" \
		-v "$(PWD):/work" -w /work \
		$(image-name):$(image-version)-test \
		sh -c "ruff format app && ruff check app --fix --exit-zero"


##############################################################################
# Operations — backup / restore / verify / copy
##############################################################################

run-backup: clean
	docker run --rm --name=$(container) \
		$(tz-mounts) $(io-mounts) $(common-env) $(needs-socket) \
		$(image-name):$(image-version) \
		python main.py backup \
		-n "$(backup-file-name)" -c $(compression) -p $(parity) \
		$(volume-args)

run-backup-encrypt: clean
	@test -n "$(encryption-key)" || { echo "ERROR: pass encryption-key=..."; exit 1; }
	docker run --rm --name=$(container) \
		$(tz-mounts) $(io-mounts) $(common-env) $(needs-socket) \
		-e ENCRYPTION_KEY="$(encryption-key)" \
		$(image-name):$(image-version) \
		python main.py backup \
		-n "$(backup-file-name)" -c $(compression) -p $(parity) \
		$(volume-args)

run-backup-parity: clean
	@test -n "$(encryption-key)" || { echo "ERROR: pass encryption-key=..."; exit 1; }
	docker run --rm --name=$(container) \
		$(tz-mounts) $(io-mounts) $(common-env) $(needs-socket) \
		-e ENCRYPTION_KEY="$(encryption-key)" \
		$(image-name):$(image-version) \
		python main.py backup \
		-n "$(backup-file-name)" -c $(compression) \
		-p $(if $(filter 0,$(parity)),30,$(parity)) \
		$(volume-args)

run-restore: clean
	docker run --rm --name=$(container) \
		$(tz-mounts) $(io-mounts) $(common-env) $(needs-socket) \
		$(enc-env) $(ts-env) \
		$(image-name):$(image-version) \
		python main.py restore -n "$(backup-file-name)" $(volume-args)

run-verify: clean
	docker run --rm --name=$(container) \
		$(tz-mounts) $(io-mounts) $(common-env) $(needs-socket) \
		$(enc-env) \
		$(image-name):$(image-version) \
		python main.py verify -n "$(backup-file-name)" $(volume-args)

run-copy: clean
	docker run --rm --name=$(container) \
		$(tz-mounts) $(io-mounts) $(common-env) $(needs-socket) \
		$(image-name):$(image-version) \
		python main.py copy \
		$(if $(filter N n no,$(overwrite)),--no-overwrite,) \
		$(volume-args)


##############################################################################
# Rename Docker volume (requires docker.sock)
##############################################################################

run-rename:
	@test -n "$(source)" || { echo "ERROR: pass source=..."; exit 1; }
	@test -n "$(target)" || { echo "ERROR: pass target=..."; exit 1; }
	docker run --rm -it $(docker-socket) $(common-env) \
		$(image-name):$(image-version) \
		python main.py rename "$(source)" "$(target)" \
		$(if $(filter Y y yes true 1,$(keep-source)),--keep-source,) \
		$(if $(filter Y y yes true 1,$(force)),--force,) \
		$(if $(filter Y y yes true 1,$(yes)),--yes,)


##############################################################################
# Interactive wizard (requires TTY + docker.sock for volume actions)
##############################################################################

run-interactive: clean
	docker run --rm -it --name=$(container) \
		$(tz-mounts) $(io-mounts) $(common-env) $(docker-socket) \
		$(image-name):$(image-version) \
		python main.py interactive


##############################################################################
# Docker volume management (always requires docker.sock)
##############################################################################

run-volumes-list:
	docker run --rm $(docker-socket) $(common-env) \
		$(image-name):$(image-version) \
		python main.py volumes list \
		$(if $(filter Y y yes true 1,$(size)),--size,) \
		$(if $(filter Y y yes true 1,$(orphans)),--orphans,)

run-volumes-inspect:
	@test -n "$(name)" || { echo "ERROR: pass name=..."; exit 1; }
	docker run --rm $(docker-socket) $(common-env) \
		$(image-name):$(image-version) \
		python main.py volumes inspect "$(name)"

run-volumes-create:
	@test -n "$(name)" || { echo "ERROR: pass name=..."; exit 1; }
	docker run --rm $(docker-socket) $(common-env) \
		$(image-name):$(image-version) \
		python main.py volumes create "$(name)"

run-volumes-remove:
	@test -n "$(name)" || { echo "ERROR: pass name=..."; exit 1; }
	docker run --rm -it $(docker-socket) $(common-env) \
		$(image-name):$(image-version) \
		python main.py volumes remove "$(name)" \
		$(if $(filter Y y yes true 1,$(force)),--force,) \
		$(if $(filter Y y yes true 1,$(yes)),--yes,)


##############################################################################
# Development shells (mount ./app live so edits are reflected)
##############################################################################

run-devel: clean
	docker run --rm -it --name=$(container) \
		-v "$(PWD)/app:/app" \
		$(tz-mounts) $(io-mounts) $(docker-socket) \
		-e TZ=$(tz) -e LOG_LEVEL=DEBUG -e LOG_OUTPUT=console \
		$(image-name):$(image-version) bash

run-devel-test: clean
	docker run --rm -it --name=$(container) \
		-v "$(PWD)/app:/app" \
		$(tz-mounts) $(io-mounts) $(docker-socket) \
		-e TZ=$(tz) -e LOG_LEVEL=DEBUG -e PYTHONPATH=/app \
		$(image-name):$(image-version)-test bash


##############################################################################
# Container lifecycle
##############################################################################

stop:
	-@docker stop $(container) >/dev/null 2>&1 || true

clean: stop
	-@docker rm $(container) >/dev/null 2>&1 || true

bash:
	docker exec -it $(container) bash


##############################################################################
# Image distribution
##############################################################################

update:
	docker pull $(image-name):$(image-version)

init: clean update build-prod

push-to-repo:
	docker push -a $(image-name)

save-image:
	docker save $(image-name):$(image-version) | gzip > "$(image-name) v$(image-version).tar.gz"

load-image:
	docker load -i "$(image-name) v$(image-version).tar.gz"
