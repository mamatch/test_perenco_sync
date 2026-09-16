# Convenience targets. Docker is the recommended path for candidates; the "local-*"
# targets run the same services with uv on the host.

.PHONY: up down reset logs seeds answer-key archive test local-cmms local-mdm local-mdm-seed sync

up:            ## start the CMMS mock and the MDM (Docker)
	docker compose up -d --build

down:          ## stop everything and delete state (fresh start)
	docker compose down -v
	rm -rf mdm_data

reset:         ## reset the CMMS mock state and reload the MDM fixture (containers keep running)
	curl -s -X POST http://localhost:8080/_admin/PERENCO/reset
	docker compose exec mdm python manage.py scenario reset

logs:
	docker compose logs -f

VARIANT ?=

seeds:         ## regenerate every dataset from shared/world.py (maintainers only). make seeds VARIANT=<candidate>
	uv run --no-project tools/generate_seeds.py $(if $(VARIANT),--variant $(VARIANT),)

answer-key:    ## expected outcome of a correct sync (evaluators only). make answer-key VARIANT=<candidate>
	uv run --no-project tools/answer_key.py $(if $(VARIANT),--variant $(VARIANT),)

archive:       ## build ../data-engineering-test-<NAME>.zip for a candidate (evaluators only). make archive NAME=<candidate>
	uv run --no-project tools/make_archive.py --name $(or $(NAME),candidate)

test:          ## run the mock API test-suite
	cd mock_gmao && uv run pytest -q

local-cmms:    ## run the CMMS mock without Docker
	cd mock_gmao && uv sync && uv run uvicorn mock_gmao.main:app --port 8080 --reload

local-mdm-seed:
	cd systemref_lite && uv sync && uv run manage.py migrate && uv run manage.py seed_masterdata

local-mdm:     ## run the MDM without Docker (after local-mdm-seed)
	cd systemref_lite && uv run manage.py runserver 8000

sync:          ## candidate solution: run the MDM<->CMMS<->IoT sync against a running sandbox
	cd systemref_lite && uv sync && cd ..
	cd pipeline && uv sync && uv run python -m pipeline run-all
