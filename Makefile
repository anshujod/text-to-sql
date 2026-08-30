.PHONY: up down seed reset test eval ablation demo

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

# Costs real money (real OpenRouter calls) -- reproduces the Task 4.3 dev-set
# ablation at the same $1.20 hard ceiling the real run used. Does not touch
# the held-out test set; that run is separate (scripts/run_ablation.py
# --dataset data/test.jsonl), one-time, and already done -- see results/RESULTS.md.
ablation:
	uv run python scripts/run_ablation.py --dataset data/dev.jsonl --n-items 25 --ceiling 1.20 --out results/ablation.md

# $0 -- replays 6 real, already-evaluated questions (data/demo_presets.json),
# no LLM calls. Needs the DB running (`make up`).
demo:
	uv run python -m t2sql.demo
