# Run from the repository root:  make -C deploy/k8s <target>
CLUSTER ?= ledgerops
NS := ledgerops
ROOT := $(abspath ../..)
KUBECTL ?= kubectl
NODE := $(CLUSTER)-control-plane
PLATFORM ?= linux/$(shell docker version --format '{{.Server.Arch}}' 2>/dev/null)
# Every image is pulled on the host and loaded into the node; the cluster never pulls
# (works behind proxies the kind node cannot reach). PostgreSQL stays pinned to the same
# digest as ops/linux and the tests; the node sees it as the local alias below.
POSTGRES_DIGEST := postgres@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722
POSTGRES_IMAGE := ledgerops-postgres:18.6
THIRD_PARTY := prom/prometheus:v3.5.0 grafana/grafana:12.1.1

# `kind load docker-image` imports with --all-platforms, which fails for multi-platform
# images under Docker's containerd image store ("content digest ... not found": only
# this platform's layers exist locally). Save and import the node's platform only.
define load
docker save --platform $(PLATFORM) $(1) | docker exec -i $(NODE) \
  ctr --namespace=k8s.io images import --digests --snapshotter=overlayfs --platform $(PLATFORM) -
endef

.PHONY: help cluster preload images secrets deploy bootstrap ready up status dashboards \
        alert-status fault-kill-worker fault-db-down fault-db-restore verify validate down

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1

cluster:
	kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config kind-config.yaml

preload:
	for image in $(POSTGRES_DIGEST) $(THIRD_PARTY); do \
	  docker image inspect $$image >/dev/null 2>&1 || docker pull $$image || exit 1; \
	done
	docker tag $(POSTGRES_DIGEST) $(POSTGRES_IMAGE)
	for image in $(POSTGRES_IMAGE) $(THIRD_PARTY); do $(call load,$$image) || exit 1; done

# Runtime and proxy targets of the unchanged ops/linux/Dockerfile, built from the
# context ops/linux/stack.py prepares. Behind a TLS-intercepting proxy, pass
# EXTRA_CA_CERT=/path/to/ca.pem (used only through a BuildKit secret mount).
EXTRA_CA_CERT ?=
images:
	python3 scripts/build_images.py $(if $(EXTRA_CA_CERT),--extra-ca-cert $(EXTRA_CA_CERT))
	for image in ledgerops-local:k8s ledgerops-local-proxy:k8s; do $(call load,$$image) || exit 1; done

secrets:
	$(KUBECTL) apply -f base/namespace.yaml
	cd $(ROOT) && uv run --locked python deploy/k8s/scripts/generate_secrets.py

deploy:
	$(KUBECTL) apply -k .

bootstrap:
	$(KUBECTL) -n $(NS) wait --for=condition=complete job/bootstrap --timeout=300s

ready:
	$(KUBECTL) -n $(NS) rollout status statefulset/postgres --timeout=180s
	for d in api dispatch synthetic-probe prometheus grafana; do \
	  $(KUBECTL) -n $(NS) rollout status deployment/$$d --timeout=180s || exit 1; \
	done

up: cluster preload images secrets deploy bootstrap ready status

status:
	$(KUBECTL) -n $(NS) get pods,svc,pvc -o wide

# Grafana: http://127.0.0.1:3000  Prometheus: http://127.0.0.1:9090 (loopback only)
dashboards:
	$(KUBECTL) -n $(NS) port-forward svc/grafana 3000:3000 & \
	$(KUBECTL) -n $(NS) port-forward svc/prometheus 9090:9090

alert-status:
	$(KUBECTL) -n $(NS) exec deploy/prometheus -- wget -qO- http://127.0.0.1:9090/api/v1/alerts

# SIGKILL only the worker (not the provider) while its PUT is in flight; checks that the
# payout still settles with exactly one provider effect. See scripts/fault-kill-worker.sh.
fault-kill-worker:
	NS=$(NS) NODE=$(NODE) KUBECTL=$(KUBECTL) ./scripts/fault-kill-worker.sh

# Disconnect the database: scale PostgreSQL to zero. The data PVC is kept.
fault-db-down:
	$(KUBECTL) -n $(NS) scale statefulset/postgres --replicas=0

fault-db-restore:
	$(KUBECTL) -n $(NS) scale statefulset/postgres --replicas=1
	$(KUBECTL) -n $(NS) rollout status statefulset/postgres --timeout=120s
	$(KUBECTL) -n $(NS) wait --for=condition=ready pod -l app=api --timeout=120s

# Independent check of the whole ledger (ops/linux/bootstrap.py inspect) as a one-off Job.
verify:
	$(KUBECTL) -n $(NS) delete job verify --ignore-not-found
	$(KUBECTL) apply -f jobs/verify.yaml
	$(KUBECTL) -n $(NS) wait --for=condition=complete job/verify --timeout=180s
	$(KUBECTL) -n $(NS) logs job/verify -c inspect --tail=1 | python3 -c 'import json,sys; \
	  r = json.load(sys.stdin)["report"]; \
	  print({k: r[k] for k in ("healthy", "verification_complete", "structure_ok", "projection_ok", "request_intent_ok", "issue_count")})'

validate:
	$(KUBECTL) kustomize . | kubeconform -strict -summary -ignore-missing-schemas
	kubeconform -strict -summary jobs/verify.yaml

down:
	kind delete cluster --name $(CLUSTER)
