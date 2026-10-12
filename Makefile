# Bookmark -> RAG. Override any variable on the command line, e.g.
#   make process BATCH_SIZE=25
INPUT ?= $(word 2,$(MAKECMDGOALS))
dotenv = $(shell awk -F= '$$1 == "$(1)" {sub(/^[^=]*=/, ""); print; exit}' .env 2>/dev/null)
OUTPUT ?= $(or $(call dotenv,OUTPUT),storage/rag_bookmarks_processed.json)
CHECKPOINT ?= $(or $(call dotenv,CHECKPOINT),storage/checkpoint.json)
BATCH_SIZE ?= $(or $(call dotenv,BATCH_SIZE),100)
CONCURRENCY ?= $(or $(call dotenv,CONCURRENCY),10)

# `make process <path>` passes the export as a second goal, which make would
# otherwise reject. Swallow it; the recipe below runs it.
ifneq (,$(strip $(INPUT)))
ifneq (,$(filter-out process,$(MAKECMDGOALS)))
$(INPUT):
	@:
endif
endif

.PHONY: process help

process:
	@test -n "$(strip $(INPUT))" || { echo "usage: make process <pinboard_export.json>"; exit 2; }
	uv run process_bookmarks.py --input "$(INPUT)" --output "$(OUTPUT)" --checkpoint "$(CHECKPOINT)" --batch-size "$(BATCH_SIZE)" --concurrency "$(CONCURRENCY)"

help:
	@printf 'Usage:\n  make process <pinboard_export.json>  process the next batch of bookmarks\n\n'
	@printf 'Only INPUT is required. Override the defaults inline:\n'
	@printf '  make process ~/bookmarks.json BATCH_SIZE=25\n\n'
	@printf '  INPUT        %s\n' '$(if $(strip $(INPUT)),$(INPUT),<pinboard_export.json>)'
	@printf '  OUTPUT       %s\n' '$(OUTPUT)'
	@printf '  CHECKPOINT   %s\n' '$(CHECKPOINT)'
	@printf '  BATCH_SIZE   %s\n' '$(BATCH_SIZE)'
	@printf '  CONCURRENCY  %s\n' '$(CONCURRENCY)'
