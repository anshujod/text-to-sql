.PHONY: up down seed reset test eval

up:
	docker compose up -d --wait

down:
	docker compose down

seed:
	uv run python scripts/generate_seed.py
	docker compose exec -T db psql -v ON_ERROR_STOP=1 -U app_owner -d t2sql -f /dev/stdin < scripts/verify_seed.sql

reset:
	docker compose down -v
	$(MAKE) up

test:
	uv run pytest -q

eval:
	@echo "make eval: not implemented yet (Phase 1.5)"
