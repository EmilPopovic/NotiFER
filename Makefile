# Default compose file (can be overridden with COMPOSE_FILE=compose.dev.yaml)
COMPOSE_FILE ?= compose.yaml

.PHONY: up
up:
	docker compose -f $(COMPOSE_FILE) up --build

.PHONY: upd
upd:
	docker compose -f $(COMPOSE_FILE) up -d --build

.PHONY: down
down:
	docker compose -f $(COMPOSE_FILE) down

.PHONY: initdb
initdb:
	docker compose -f $(COMPOSE_FILE) run --build --rm notifer python -m src.db_manager create

.PHONY: resetdb
resetdb:
	docker compose -f $(COMPOSE_FILE) run --build --rm notifer python -m src.db_manager reset

.PHONY: dropdb
dropdb:
	docker compose -f $(COMPOSE_FILE) run --build --rm notifer python -m src.db_manager drop

.PHONY: checkdb
checkdb:
	docker compose -f $(COMPOSE_FILE) run --build --rm notifer python -m src.db_manager check

.PHONY: encryptdb
encryptdb:
	docker compose -f $(COMPOSE_FILE) run --build --rm notifer python -m src.db_manager encrypt

.PHONY: verifydb
verifydb:
	docker compose -f $(COMPOSE_FILE) run --build --rm notifer python -m src.db_manager verify

# Migration bundles. FILE is relative to this directory, which is mounted at
# /migration inside the one-off container.
# The passphrase is prompted for; set MIGRATION_PASSPHRASE to script it.
FILE ?= notifer-export.nfer
MIGRATION_RUN = docker compose -f $(COMPOSE_FILE) run --build --rm \
	-v "$(CURDIR):/migration" -e MIGRATION_PASSPHRASE

.PHONY: exportdb
exportdb:
	@echo "Exporting to $(FILE) — contains live calendar tokens, keep it safe"
	@# Runs as the host user so the bundle lands owned by you, not root.
	$(MIGRATION_RUN) --user "$(shell id -u):$(shell id -g)" \
		notifer python -m src.db_manager export /migration/$(FILE) $(EXPORT_FLAGS)

# make importdb FILE=bundle.nfer DRY_RUN=1   # validate only
# make importdb FILE=bundle.nfer            # replace all data
.PHONY: importdb
importdb:
	@# Stays root: restoring cached calendars writes into the app_data volume.
	$(MIGRATION_RUN) notifer python -m src.db_manager import /migration/$(FILE) \
		$(if $(DRY_RUN),--dry-run,--replace)

.PHONY: snapshot
snapshot:
	@echo "Ensuring postgres container is running..."
	@docker compose -f $(COMPOSE_FILE) up -d postgres
	@TIMESTAMP=$$(date +%Y%m%d_%H%M%S) && \
	POSTGRES_USER=$$(grep '^POSTGRES_USER=' .env | cut -d '=' -f2) && \
	BACKUP_FILE="snapshot_$${TIMESTAMP}.sql.gz" && \
	echo "Creating database snapshot: $${BACKUP_FILE}" && \
	docker compose -f $(COMPOSE_FILE) exec -T postgres pg_dumpall --clean --if-exists --username=$${POSTGRES_USER} | gzip > "$${BACKUP_FILE}" && \
	echo "Snapshot created successfully: $${BACKUP_FILE}"
