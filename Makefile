# start, restart and stop the reader app in the background
#   make start READERS=r1        make stop        make restart        make status
# variables: DATA (study folder), IMAGES, READERS, PORT, ARGS (extra server options)
DATA    ?= study
IMAGES  ?= $(DATA)/images
READERS ?= r1,r2
PORT    ?= 8765
ARGS    ?=
PY      ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
PID     := .server-$(PORT).pid
LOG     := .server-$(PORT).log
URL     := http://127.0.0.1:$(PORT)

.PHONY: start stop restart status log

start:
	@if [ -f $(PID) ] && kill -0 $$(cat $(PID)) 2>/dev/null; then echo "already running at $(URL) (pid $$(cat $(PID)))"; exit 0; fi; \
	nohup $(PY) -m goldkp.server --data $(DATA) --images $(IMAGES) --readers $(READERS) --port $(PORT) $(ARGS) > $(LOG) 2>&1 & \
	echo $$! > $(PID); \
	for i in $$(seq 1 40); do \
	  if ! kill -0 $$(cat $(PID)) 2>/dev/null; then echo "server did not start:"; tail -n 20 $(LOG); rm -f $(PID); exit 1; fi; \
	  if curl -s -o /dev/null $(URL); then echo "running at $(URL) (pid $$(cat $(PID)), log $(LOG))"; exit 0; fi; \
	  sleep 0.25; \
	done; echo "still starting, see $(LOG)"

stop:
	@if [ -f $(PID) ] && kill -0 $$(cat $(PID)) 2>/dev/null; then kill $$(cat $(PID)) && echo "stopped (pid $$(cat $(PID)))"; \
	else echo "not running on port $(PORT)"; fi; rm -f $(PID)

restart: stop start

status:
	@if [ -f $(PID) ] && kill -0 $$(cat $(PID)) 2>/dev/null; then echo "running at $(URL) (pid $$(cat $(PID)))"; else echo "not running on port $(PORT)"; fi

log:
	@tail -n 40 $(LOG)
