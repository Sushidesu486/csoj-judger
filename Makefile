.PHONY: test typecheck lint check zipapp

test:
	uv run pytest

typecheck:
	uv run mypy src

lint:
	uv run ruff check src tests

check: test typecheck lint

zipapp:
	mkdir -p dist
	python3 -m zipapp src -m 'oj_checker.cli:entrypoint' -o dist/oj-checker.pyz
