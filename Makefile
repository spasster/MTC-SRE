include versions.mk
.DEFAULT_GOAL := up-linux
CLUSTER ?= mtc
NODE_LOG_HOST ?= colima
TELEGRAM_VALUES := $(wildcard observability/telegram.local.yaml)

.PHONY: up up-linux stack ready context cluster cluster-linux images apps repos mesh observability dashboards blackbox loki logs logs-linux node-logs node-logs-linux alloy-host chaos llm

context:
	@sh scripts/require-context.sh $(CLUSTER)

images apps mesh observability dashboards blackbox loki chaos llm: context

up: cluster
	$(MAKE) stack
	$(MAKE) logs
	$(MAKE) chaos
	$(MAKE) ready

up-linux: cluster-linux
	$(MAKE) stack
	$(MAKE) logs-linux
	$(MAKE) chaos
	$(MAKE) ready

# Этапы выполняются последовательно, в том числе при make -j.
stack:
	$(MAKE) images
	$(MAKE) mesh
	$(MAKE) apps
	$(MAKE) observability
	$(MAKE) dashboards

ready:
	kubectl rollout status deployment/checkout -n demo --timeout=180s
	kubectl rollout status deployment/payment -n demo --timeout=180s
	kubectl rollout status deployment/redis -n demo --timeout=180s
	kubectl rollout status deployment/healer -n heal --timeout=180s
	kubectl rollout status deployment/tempo -n obs --timeout=180s
	kubectl rollout status deployment/blackbox -n obs --timeout=180s
	kubectl rollout status deployment/alloy -n obs --timeout=180s
	kubectl rollout status statefulset/loki -n obs --timeout=180s
	kubectl rollout status statefulset/prometheus-kube-prom-kube-prometheus-prometheus -n obs --timeout=180s
	kubectl rollout status statefulset/alertmanager-kube-prom-kube-prometheus-alertmanager -n obs --timeout=180s

cluster:
	colima status >/dev/null 2>&1 || colima start --cpu 4 --memory 8 --disk 40
	docker context use colima
	@if ! k3d cluster list | awk 'NR>1 {print $$1}' | grep -qx '$(CLUSTER)'; then \
		k3d cluster create $(CLUSTER) --agents 0 --image $(K3S_IMAGE) --wait --timeout 180s; \
	fi
	kubectl config use-context k3d-$(CLUSTER)

cluster-linux:
	@docker info >/dev/null 2>&1 || { echo "Docker не запущен. Пакеты и группа docker описаны в README, раздел Ubuntu."; exit 1; }
	@if ! k3d cluster list | awk 'NR>1 {print $$1}' | grep -qx '$(CLUSTER)'; then \
		k3d cluster create $(CLUSTER) --agents 0 --image $(K3S_IMAGE) --wait --timeout 180s; \
	fi
	kubectl config use-context k3d-$(CLUSTER)

images:
	docker build -t checkout:dev apps/checkout
	docker build -t payment:dev apps/payment
	docker build -t healer:dev healer
	k3d image import checkout:dev payment:dev healer:dev -c $(CLUSTER) --mode direct

apps:
	kubectl apply -f k8s/namespace.yaml -f k8s/redis.yaml -f k8s/payment.yaml -f k8s/checkout.yaml
	kubectl create configmap k6-scripts -n demo \
		--from-file=k6/ \
		--dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -f k8s/k6-background.yaml

repos:
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
	helm repo add grafana https://grafana.github.io/helm-charts --force-update
	helm repo add chaos-mesh https://charts.chaos-mesh.org --force-update
	helm repo add istio https://istio-release.storage.googleapis.com/charts --force-update
	helm repo update

mesh: repos
	helm upgrade --install istio-base istio/base -n istio-system --create-namespace \
		--version $(ISTIO_VERSION) --wait --timeout 5m
	helm upgrade --install istiod istio/istiod -n istio-system \
		--version $(ISTIO_VERSION) -f observability/istio-values.yaml --wait --timeout 5m
	kubectl apply -f k8s/namespace.yaml -f k8s/mtls.yaml

observability: repos
	sh scripts/configure-secrets.sh
	helm upgrade --install kube-prom prometheus-community/kube-prometheus-stack \
		--version $(PROM_STACK_VERSION) \
		-n obs --create-namespace \
		--wait --timeout 10m \
		-f observability/kube-prom-values.yaml \
		-f .runtime/notification-values.yaml \
		$(if $(TELEGRAM_VALUES),-f $(TELEGRAM_VALUES),)
	kubectl wait --for=condition=Established --timeout=120s \
		crd/servicemonitors.monitoring.coreos.com crd/prometheusrules.monitoring.coreos.com
	kubectl apply -f k8s/checkout-servicemonitor.yaml -f k8s/payment-servicemonitor.yaml -f observability/rules.yaml -f k8s/healer.yaml
	kubectl apply -f k8s/tempo.yaml -f observability/tempo-datasource.yaml -f observability/loki-datasource.yaml
	$(MAKE) blackbox

blackbox:
	helm upgrade --install blackbox prometheus-community/prometheus-blackbox-exporter \
		--version $(BLACKBOX_CHART_VERSION) -n obs -f observability/blackbox-values.yaml --wait --timeout 5m

dashboards:
	kubectl create configmap mtc-dashboards -n obs \
		--from-file=observability/dashboards/ \
		--dry-run=client -o yaml | kubectl apply -f -
	kubectl label configmap mtc-dashboards -n obs grafana_dashboard=1 --overwrite

loki: repos
	helm upgrade --install loki grafana/loki --version $(LOKI_CHART_VERSION) -n obs -f observability/loki-values.yaml --wait --timeout 5m
	helm upgrade --install alloy grafana/alloy --version $(ALLOY_CHART_VERSION) -n obs -f observability/alloy-values.yaml --wait --timeout 5m
	kubectl apply -f k8s/loki-nodeport.yaml -f observability/loki-datasource.yaml

logs:
	$(MAKE) loki
	$(MAKE) node-logs
	$(MAKE) alloy-host NODE_LOG_HOST=colima

logs-linux:
	$(MAKE) loki
	$(MAKE) node-logs-linux
	$(MAKE) alloy-host NODE_LOG_HOST=linux

node-logs:
	sh ansible/colima-inventory.sh
	cd ansible && ANSIBLE_CONFIG=ansible.cfg ansible-playbook playbooks/node-logs.yaml

node-logs-linux:
	cd ansible && ANSIBLE_CONFIG=ansible.cfg ansible-playbook -i inventory/linux.ini playbooks/node-logs.yaml

alloy-host:
	-docker rm -f $(CLUSTER)-alloy-host
	docker run -d --name $(CLUSTER)-alloy-host --restart unless-stopped --user 0 \
		--network container:k3d-$(CLUSTER)-server-0 \
		-e NODE_LOG_HOST=$(NODE_LOG_HOST) \
		-v /var/log:/var/log:ro \
		-v $(CLUSTER)-alloy-state:/var/lib/alloy \
		-v "$(CURDIR)/observability/alloy-host.alloy":/etc/alloy/config.alloy:ro \
		$(ALLOY_IMAGE) run --storage.path=/var/lib/alloy --server.http.listen-addr=127.0.0.1:12345 /etc/alloy/config.alloy

chaos: repos
	helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh -n chaos-mesh --create-namespace \
		--version $(CHAOS_CHART_VERSION) --wait --timeout 5m \
		--set chaosDaemon.runtime=containerd \
		--set chaosDaemon.socketPath=/run/k3s/containerd/containerd.sock \
		--set dashboard.securityMode=false

llm:
	kubectl apply -f k8s/ollama.yaml -f k8s/assistant.yaml
	kubectl rollout status deployment/ollama -n assist --timeout=600s
	kubectl exec -n assist deployment/ollama -- ollama pull qwen2.5:1.5b
	kubectl rollout status deployment/sre-assistant -n heal --timeout=180s
