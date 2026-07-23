.DEFAULT_GOAL := help
COMPOSE := docker compose
SERVICE := sys-buddy

.PHONY: help build up down restart logs ps shell token url open clean test local serve lock task tasks invite \
	dc-up dc-down dc-logs dc-ps dc-tunnel-url

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

build: ## Build the docker image
	$(COMPOSE) build

up: ## Start the broker in the background
	$(COMPOSE) up -d

down: ## Stop the broker (keeps the data volume)
	$(COMPOSE) down

restart: down up ## Restart the broker

logs: ## Follow broker logs
	$(COMPOSE) logs -f $(SERVICE)

ps: ## Show container status
	$(COMPOSE) ps

shell: ## Open a shell inside the running container
	$(COMPOSE) exec $(SERVICE) sh

viewer: ## Mint a host-viewer token and print the dashboard URL
	@$(COMPOSE) exec $(SERVICE) sys-buddy host-viewer

url: token ## Alias for `make token`

open: ## Mint a host-viewer token and open the dashboard (macOS)
	@t=$$($(COMPOSE) exec -T $(SERVICE) sys-buddy host-viewer | grep -oE '[A-Za-z0-9_-]{20,}' | tail -1); \
	open "http://localhost:8787/ui?v=$$t"

clean: ## Stop the broker and DELETE the data volume (destructive)
	$(COMPOSE) down -v

task: ## Create a task — usage: make task ID=demo ROLES=backend,frontend [TITLE="Demo"] [MODE=contract]
	@if [ -z "$(ID)" ] || [ -z "$(ROLES)" ]; then \
		echo "usage: make task ID=<id> ROLES=<role1,role2> [TITLE=...] [MODE=contract|debug]"; exit 1; \
	fi
	$(COMPOSE) exec $(SERVICE) sys-buddy task create $(ID) --roles $(ROLES) $(if $(TITLE),--title "$(TITLE)") $(if $(MODE),--mode $(MODE))

tasks: ## List tasks
	$(COMPOSE) exec $(SERVICE) sys-buddy tasks

invite: ## Mint an invite for a role — usage: make invite TASK=demo ROLE=backend
	@if [ -z "$(TASK)" ] || [ -z "$(ROLE)" ]; then \
		echo "usage: make invite TASK=<id> ROLE=<role>"; exit 1; \
	fi
	$(COMPOSE) exec $(SERVICE) sys-buddy invite --task $(TASK) --role $(ROLE)

test: ## Run the test suite locally (not in docker)
	uv run pytest -q

local: ## Run the broker locally, no docker (loopback, no auth)
	uv run sys-buddy local

serve: ## Run the broker locally in remote/auth-enforced mode, no docker
	uv run sys-buddy serve

lock: ## Refresh uv.lock after a dependency change
	uv lock
