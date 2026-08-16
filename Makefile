.PHONY: test compile demo eval clean

PYTHON ?= python

compile:
	PYTHONPATH=src $(PYTHON) -m compileall -q src

test:
	PYTHONPATH=src pytest

demo:
	rm -rf /tmp/traceh-demo /tmp/traceh-data
	cp -R examples/demo_bug /tmp/traceh-demo
	PYTHONPATH=src $(PYTHON) -m traceh.cli.main run /tmp/traceh-demo \
	  "Fix the addition bug and run the tests" \
	  --script examples/demo_script.json \
	  --verify-command "python -m unittest -v" \
	  --data-dir /tmp/traceh-data

eval:
	PYTHONPATH=src $(PYTHON) -m traceh.cli.main eval benchmarks/basic --output /tmp/traceh-eval

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache build dist *.egg-info src/*.egg-info
