PYTHON ?= $(shell if [ -x /opt/anaconda3/bin/python3 ]; then echo /opt/anaconda3/bin/python3; else command -v python3; fi)

.PHONY: dev predict pipeline worker dataset train migrate lint typecheck test test-ui test-e2e check

dev:
	$(PYTHON) apps/api/flask_app.py

predict:
	$(PYTHON) apps/cli/predict.py $(ARGS)

pipeline:
	$(PYTHON) apps/cli/daily_pipeline.py

worker:
	$(PYTHON) apps/cli/daily_pipeline.py --watch $(ARGS)

dataset:
	$(PYTHON) apps/cli/predict.py --training-dataset $(ARGS)

train:
	$(PYTHON) apps/cli/predict.py --train-baseline $(ARGS)

train-weekly:
	$(PYTHON) apps/cli/weekly_training.py

migrate:
	$(PYTHON) apps/cli/migrate.py

lint:
	$(PYTHON) -m compileall apps services database tests

typecheck:
	$(PYTHON) -m compileall apps services database tests

test:
	$(PYTHON) -m pytest

test-ui:
	npm run test:ui

test-e2e:
	$(PYTHON) -m pytest tests/test_e2e_flow.py

check: lint typecheck test test-ui
