# Local-dev helpers. Derives a meaningful VERSION from the git tag + short SHA
# so local images don't run with the meaningless "0.0.0+unknown" default that
# shows up in logs and the navbar tooltip.

TAG := $(shell git describe --tags --abbrev=0 2>/dev/null | sed 's/^v//')
SHA := $(shell git rev-parse --short HEAD 2>/dev/null)
VERSION := $(if $(TAG),$(TAG),0.0.0)+$(if $(SHA),$(SHA),unknown)

export VERSION

.PHONY: up test

# Build + start the agent in the background with the derived version.
up:
	VERSION=$(VERSION) docker compose up --build -d

# Run the test suite with the same derived version.
test:
	VERSION=$(VERSION) docker compose --profile test run --rm test
