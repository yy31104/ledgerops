# Run from the repository root:  make -C deploy/k8s <target>
CLUSTER ?= ledgerops
NS := ledgerops
ROOT := $(abspath ../..)
KUBECTL ?= kubectl
NODE := $(CLUSTER)-control-plane
STATE := $(CURDIR)/.generated/stack.json

.PHONY: help cluster images secrets deploy bootstrap up status dashboards alert-status \
        fault-kill-worker fault-db-down fault-db-restore validate down

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1

cluster:
	kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config kind-config.yaml

# Reuses ops/linux/stack.py + Dockerfile unchanged (runtime and proxy targets), then loads into kind.
images:
	mkdir -p .generated
	python3 $(ROOT)/ops/linux/stack.py --project ledgerops-k8s --state $(STATE) \
	  build --source $(ROOT) --context $(CURDIR)/.generated/context --allow-dirty
	docker tag "$$(python3 -c "import json;print(json.load(open('$(STATE)'))['environment']['LEDGEROPS_IMAGE'])")" ledgerops-local:k8s
	docker tag "$$(python3 -c "import json;print(json.load(open('$(STATE)'))['environment']['LEDGEROPS_PROXY_IMAGE'])")" ledgerops-local-proxy:k8s
	kind load docker-image --name $(CLUSTER) ledgerops-local:k8s ledgerops-local-proxy:k8s

secrets:
	$(KUBECTL) apply -f base/namespace.yaml
	cd $(ROOT) && uv run --locked python deploy/k8s/scripts/secrets.py

deploy:
	$(KUBECTL) apply -k .

bootstrap:
	$(KUBECTL) -n $(NS) wait --for=condition=complete job/bootstrap --timeout=300s

up: cluster images secrets deploy bootstrap status

status:
	$(KUBECTL) -n $(NS) get pods,svc,pvc -o wide

# Grafana: http://127.0.0.1:3000  Prometheus: http://127.0.0.1:9090 (loopback only)
dashboards:
	$(KUBECTL) -n $(NS) port-forward svc/grafana 3000:3000 & \
	$(KUBECTL) -n $(NS) port-forward svc/prometheus 9090:9090

alert-status:
	$(KUBECTL) -n $(NS) exec deploy/prometheus -- wget -qO- http://127.0.0.1:9090/api/v1/alerts

# SIGKILL only the worker process (not the provider) from the kind node's PID view.
fault-kill-worker:
	docker exec $(NODE) pkill -KILL -f 'ledgerops.worker'
	sleep 5
	$(KUBECTL) -n $(NS) get pod -l app=dispatch \
	  -o jsonpath='{range .items[0].status.containerStatuses[*]}{.name}={.restartCount}{"\n"}{end}'

# Disconnect the database: scale PostgreSQL to zero. The data PVC is kept.
fault-db-down:
	$(KUBECTL) -n $(NS) scale statefulset/postgres --replicas=0

fault-db-restore:
	$(KUBECTL) -n $(NS) scale statefulset/postgres --replicas=1
	$(KUBECTL) -n $(NS) rollout status statefulset/postgres --timeout=120s
	$(KUBECTL) -n $(NS) wait --for=condition=ready pod -l app=api --timeout=120s

validate:
	$(KUBECTL) kustomize . | kubeconform -strict -summary -ignore-missing-schemas

down:
	kind delete cluster --name $(CLUSTER)
