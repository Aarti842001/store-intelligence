.PHONY: up down test cov seed replay
up:        ## build + run the full stack
	docker compose up --build
down:
	docker compose down -v
test:
	pytest
cov:
	pytest --cov=app --cov-report=term-missing
replay:    ## replay a file: make replay FILE=events.jsonl
	python scripts/replay.py $(FILE) --batch
