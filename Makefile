IMAGE ?= oj-checker:dev
PLATFORM ?= linux/amd64

.PHONY: test typecheck lint check zipapp image container-smoke

test:
	uv run pytest

typecheck:
	uv run mypy src

lint:
	uv run ruff check src tests

check: test typecheck lint

zipapp:
	mkdir -p dist
	tmp=$$(mktemp -d); \
	trap 'rm -rf "$$tmp"' EXIT; \
	cp -R src/oj_checker "$$tmp/oj_checker"; \
	find "$$tmp" -type d -name __pycache__ -prune -exec rm -rf {} +; \
	python3 -m zipapp "$$tmp" -m 'oj_checker.cli:entrypoint' -o dist/oj-checker.pyz

image:
	docker build --platform $(PLATFORM) --tag $(IMAGE) .

container-smoke: image
	docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m $(IMAGE) --help
	docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
		--entrypoint python $(IMAGE) -c \
		'import os; import oj_checker; import psycopg; assert os.getuid() == 65532'
