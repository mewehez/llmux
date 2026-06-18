# llm-server — common dev/CI tasks.
#
# config/models.json is the SINGLE SOURCE OF TRUTH. Two files are GENERATED from
# it and must never drift:
#   - docker-compose.yml                       (infra/scripts/generate-compose.py)
#   - infra/helm/llm-server/files/models.json  (copy the Helm chart reads via .Files)
# `make generate` rebuilds both; `make verify-generated` fails loudly if either drifted.

SHELL := /bin/bash

REGISTRY      := config/models.json
HELM_REGISTRY := infra/helm/llm-server/files/models.json
COMPOSE       := docker-compose.yml
GEN           := infra/scripts/generate-compose.py
GENCMD        := uv run --with pyyaml python $(GEN)

.PHONY: help generate sync-helm verify-generated

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

generate: ## Regenerate docker-compose.yml and sync the Helm registry copy from config/models.json
	$(GENCMD)
	cp $(REGISTRY) $(HELM_REGISTRY)
	@echo "OK: regenerated $(COMPOSE) and synced $(HELM_REGISTRY)"

sync-helm: ## Copy config/models.json into the chart (files/models.json) only
	cp $(REGISTRY) $(HELM_REGISTRY)
	@echo "OK: synced $(HELM_REGISTRY)"

verify-generated: ## CI guard: fail if generated files drifted from config/models.json
	@echo "==> Helm registry copy in sync with $(REGISTRY)?"
	@diff -u $(REGISTRY) $(HELM_REGISTRY) \
		|| { echo "FAIL: $(HELM_REGISTRY) is stale — run: make sync-helm (or make generate)"; exit 1; }
	@echo "==> $(COMPOSE) matches the generator?"
	@$(GENCMD) --check \
		|| { echo "FAIL: $(COMPOSE) is stale — run: make generate"; exit 1; }
	@echo "OK: generated files are in sync with $(REGISTRY)"
