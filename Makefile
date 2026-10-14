SHELL := /bin/sh
export PATH := $(HOME)/.local/bin:$(PATH)
UV := $(shell command -v uv 2>/dev/null || command -v $(HOME)/.local/bin/uv 2>/dev/null || printf uv)
VALIDATION_RUNNER := $(UV) run --script tools/validation_harness.py
TERRAFORM_VERSION := 1.15.9
ANSIBLE_VERSION := 2.21.3
ANSIBLE_PLAYBOOK := $(UV) tool run --from ansible-core==$(ANSIBLE_VERSION) ansible-playbook
ANSIBLE_CONFIG_PATH := $(abspath infrastructure/ansible/ansible.cfg)
HELM_VERSION := 4.2.4
GO_VERSION := 1.26.7
GO_SHA256 := ffb5f8de10c62550dfddab66b36b57030721e0a44a3218e9e1181d7b59f121ca
GO_LOCAL_DIR := .local-tools/go/$(GO_VERSION)
GO := $(abspath $(GO_LOCAL_DIR)/bin/go)
CONTROLLER_TOOLS_VERSION := v0.21.0
CONTROLLER_GEN := $(abspath .local-tools/bin/controller-gen)
ENVTEST_VERSION := 1.36.2
ENVTEST_SHA256 := 5e99f4eef3d6f9d4dd063730299f708c98da8801f2f14d8fc762cb354f30c332
ENVTEST_DIR := $(abspath .local-tools/envtest/$(ENVTEST_VERSION))
GATEWAY_API_ENVTEST_DIR = $(abspath .local-tools/gateway-api/$(GATEWAY_API_VERSION))
HELM_LOCAL_DIR := .local-tools/helm/$(HELM_VERSION)
HELM := $(abspath $(HELM_LOCAL_DIR)/helm)
CILIUM_CHART_DIR = .local-tools/charts/cilium/$(CILIUM_VERSION)
CILIUM_CHART = $(abspath $(CILIUM_CHART_DIR)/cilium-$(CILIUM_VERSION).tgz)
METALLB_CHART_DIR = .local-tools/charts/metallb/$(METALLB_VERSION)
METALLB_CHART = $(abspath $(METALLB_CHART_DIR)/metallb-$(METALLB_VERSION).tgz)
ENVOY_GATEWAY_CHART_DIR = .local-tools/charts/envoy-gateway/$(ENVOY_GATEWAY_VERSION)
ENVOY_GATEWAY_CHART = $(abspath $(ENVOY_GATEWAY_CHART_DIR)/gateway-helm-v$(ENVOY_GATEWAY_VERSION).tgz)
ENVOY_GATEWAY_CRDS_CHART = $(abspath $(ENVOY_GATEWAY_CHART_DIR)/gateway-crds-helm-v$(ENVOY_GATEWAY_VERSION).tgz)
CERT_MANAGER_CHART_DIR = .local-tools/charts/cert-manager/$(CERT_MANAGER_VERSION)
CERT_MANAGER_CHART = $(abspath $(CERT_MANAGER_CHART_DIR)/cert-manager-v$(CERT_MANAGER_VERSION).tgz)
LONGHORN_CHART_DIR = .local-tools/charts/longhorn/$(LONGHORN_VERSION)
LONGHORN_CHART = $(abspath $(LONGHORN_CHART_DIR)/longhorn-$(LONGHORN_VERSION).tgz)
KUBE_PROMETHEUS_STACK_CHART_DIR = .local-tools/charts/kube-prometheus-stack/$(KUBE_PROMETHEUS_STACK_VERSION)
KUBE_PROMETHEUS_STACK_CHART = $(abspath $(KUBE_PROMETHEUS_STACK_CHART_DIR)/kube-prometheus-stack-$(KUBE_PROMETHEUS_STACK_VERSION).tgz)
LOKI_CHART_DIR = .local-tools/charts/loki/$(LOKI_CHART_VERSION)
LOKI_CHART = $(abspath $(LOKI_CHART_DIR)/loki-$(LOKI_CHART_VERSION).tgz)
TEMPO_CHART_DIR = .local-tools/charts/tempo/$(TEMPO_CHART_VERSION)
TEMPO_CHART = $(abspath $(TEMPO_CHART_DIR)/tempo-$(TEMPO_CHART_VERSION).tgz)
GATEWAY_API_STANDARD_SHA256 := 751002b3b91a87f7ae3bd2517c79a47a8d7ed6702901808a1cf9bd97d284f9b8
KUBECTL := $(HOME)/.local/bin/kubectl
KUBECONFIG := $(HOME)/.kube/lap-admin.conf
POSTGRESQL_SECRET_DIR ?= $(HOME)/.local/share/local-application-platform/secrets
OBSERVABILITY_SECRET_DIR ?= $(HOME)/.local/share/local-application-platform/secrets
BUILD_HOST_SECRET_DIR ?= $(HOME)/.local/share/local-application-platform/secrets/build-host
BUILD_HOST_PKI_DIR := $(abspath .generated/pki)
PLATFORM_API_SECRET_DIR ?= $(HOME)/.local/share/local-application-platform/secrets
TERRAFORM_LOCAL_DIR := .local-tools/terraform/$(TERRAFORM_VERSION)
TERRAFORM_LOCAL_EXE := $(abspath $(TERRAFORM_LOCAL_DIR)/terraform.exe)
TERRAFORM_TFVARS := ../../.generated/terraform/lab.auto.tfvars.json
TERRAFORM ?= $(TERRAFORM_LOCAL_EXE)

-include .generated/make/generated.mk

.PHONY: help
help:
	@printf '%s\n' \
		'Local Application Platform targets:' \
		'  make config-validate    Validate authoritative configuration' \
		'  make config-generate    Generate tool configuration under .generated' \
		'  make terraform-install  Install project-local Windows Terraform' \
		'  make terraform-fmt      Format Terraform files' \
		'  make terraform-init     Initialize Terraform' \
		'  make terraform-plan     Plan Terraform changes' \
		'  make terraform-validate Validate Terraform configuration' \
		'  make ubuntu-image-fetch Fetch and verify Canonical Ubuntu VHD archive' \
		'  make ubuntu-image-prepare Prepare the reusable Hyper-V Ubuntu VHDX' \
		'  make ubuntu-image-verify Verify the immutable prepared parent (elevated)' \
		'  make lab-routing-prepare Configure direct WSL routing (elevated, after apply)' \
		'  make lab-start          Verify/recover WSL route to an existing lab (UAC if needed)' \
		'  make lab-backup         Quiesce writes and create a verified zstd state archive' \
		'  make lab-restore        Restore LAB_BACKUP_ARCHIVE into a compatible lab' \
		'  make lab-backup-check   Verify archive and isolated database/registry restore' \
		'  make lab-backup-restore-acceptance Run a disposable live recovery drill' \
		'  make host-services       Configure HAProxy, Keepalived, DNS and LB firewall' \
		'  make host-services-check Validate running load-balancer host services' \
		'  make host-services-failover Exercise and restore Keepalived VIP failover' \
		'  make node-prepare        Prepare six Kubernetes nodes without kubeadm' \
		'  make node-prepare-check  Validate Kubernetes pre-bootstrap node state' \
		'  make cluster-bootstrap   Bootstrap the three-control-plane kubeadm cluster' \
		'  make cluster-check       Validate kubeadm, etcd, HAProxy and WSL access' \
		'  make cluster-ha-test     Exercise one API-server failure and recovery' \
		'  make cilium-install      Install pinned Cilium with generated Helm values' \
		'  make cilium-check        Validate core Kubernetes networking' \
		'  make cilium-failure-test Restart one Cilium agent and prove recovery' \
		'  make metallb-install     Install pinned MetalLB and Layer-2 configuration' \
		'  make metallb-check       Validate MetalLB allocation and reachability' \
		'  make metallb-failure-test Exercise Layer-2 announcer failover and recovery' \
		'  make envoy-gateway-install Install Gateway API and pinned Envoy Gateway' \
		'  make envoy-gateway-check Validate shared HTTP Gateway routing' \
		'  make envoy-gateway-failure-test Replace one Envoy pod and prove recovery' \
		'  make cert-manager-install Install private PKI, cert-manager and Gateway TLS' \
		'  make cert-manager-check Validate trusted HTTPS and certificate hierarchy' \
		'  make cert-manager-failure-test Restart cert-manager and prove TLS continuity' \
		'  make longhorn-install    Install/reconcile Longhorn on the three workers' \
		'  make longhorn-check      Validate Longhorn and disposable persistent storage' \
		'  make longhorn-failure-test Validate persistent storage across a worker drain' \
		'  make postgresql-install  Install/reconcile PostgreSQL on Longhorn' \
		'  make postgresql-check    Validate PostgreSQL TLS, policy and persistence' \
		'  make postgresql-idempotence Prove PostgreSQL reconciliation is stable' \
		'  make observability-install Install/reconcile the metrics, logs and traces stack' \
		'  make observability-check Validate metrics, logs, traces, dashboards and alerts' \
		'  make observability-failure-test Prove Loki, Tempo and Collector recovery' \
		'  make observability-idempotence Prove full observability reconciliation is stable' \
		'  make build-host-install  Install/reconcile the registry and rootless build toolchain' \
		'  make build-host-check    Validate TLS, auth, isolation and exact artifact publication' \
		'  make build-host-idempotence Prove build-host reconciliation is stable' \
		'  make platform-api-install Deploy the two-replica API foundation' \
		'  make platform-api-runtime-check Validate live API health, auth, and metrics' \
		'  make platform-api-build-dispatch-check Validate scheduler build dispatch and Release persistence' \
		'  make platform-api-rollback-check Validate failed deployment rollback recovery' \
		'  make platform-controller-install Deploy the Application CRD and controller foundation' \
		'  make platform-controller-live-check Validate live controller reconciliation and recovery' \
		'  make static-checks      Check shell and PowerShell syntax' \
		'  make lab-up             Create the full lab' \
		'  make lab-destroy        Destroy the full lab' \
		'  make lab-status         Show lab status' \
		'  make demo-up            Create lightweight demo environment' \
		'  make demo-down          Destroy lightweight demo environment' \
		'  make test               Run unit/component tests' \
		'  make test-e2e           Run end-to-end tests' \
		'  make test-failures      Run failure tests'

.PHONY: config-validate
config-validate:
	@$(UV) run --package lap-config lap-config validate >/dev/null
	@printf '%s\n' 'PASS configuration'

.PHONY: config-generate
config-generate:
	$(UV) run --package lap-config lap-config generate

.PHONY: terraform-install
terraform-install:
	sh tools/install-terraform-windows.sh $(TERRAFORM_VERSION) $(TERRAFORM_LOCAL_DIR)

.PHONY: terraform-fmt
terraform-fmt: terraform-install
	$(TERRAFORM) -chdir=infrastructure/terraform fmt -recursive

.PHONY: terraform-init
terraform-init: terraform-install
	$(TERRAFORM) -chdir=infrastructure/terraform init

.PHONY: terraform-init-validate
terraform-init-validate: terraform-install
	$(TERRAFORM) -chdir=infrastructure/terraform init -backend=false

.PHONY: terraform-validate
terraform-validate: terraform-init-validate
	$(TERRAFORM) -chdir=infrastructure/terraform validate

.PHONY: terraform-plan
terraform-plan: config-generate terraform-init ubuntu-image-verify
	$(TERRAFORM) -chdir=infrastructure/terraform plan -input=false -var-file=$(TERRAFORM_TFVARS)

.PHONY: ubuntu-image-fetch
ubuntu-image-fetch:
	sh tools/fetch-ubuntu-vhd-archive.sh

.PHONY: ubuntu-image-prepare
ubuntu-image-prepare: config-generate
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/prepare-ubuntu-hyperv-image.ps1

.PHONY: ubuntu-image-verify
ubuntu-image-verify: config-generate
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/prepare-ubuntu-hyperv-image.ps1 -VerifyOnly

.PHONY: lab-routing-prepare
lab-routing-prepare: config-generate
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/prepare-lab-routing.ps1

.PHONY: lab-start
lab-start: config-generate
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/ensure-lab-routing.ps1

.PHONY: host-services
host-services: config-generate
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/host-services.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: host-services-check
host-services-check: config-validate
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/validate-host-services.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: host-services-failover
host-services-failover: config-validate
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/test-keepalived-failover.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: node-prepare
node-prepare: config-generate
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/kubernetes-node-prepare.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: node-prepare-check
node-prepare-check: config-validate
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/validate-kubernetes-node-preparation.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: cluster-bootstrap
cluster-bootstrap: config-generate
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/kubernetes-bootstrap.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: cluster-check
cluster-check: config-validate
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/validate-kubernetes-bootstrap.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: cluster-ha-test
cluster-ha-test: config-validate
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/test-kubernetes-control-plane-ha.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: helm-install
helm-install:
	sh tools/install-helm.sh $(HELM_VERSION) $(HELM)

.PHONY: cilium-host-firewall
cilium-host-firewall: config-generate
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/kubernetes-networking-host.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: cilium-render
cilium-render: config-generate helm-install
	mkdir -p $(CILIUM_CHART_DIR)
	$(HELM) pull oci://quay.io/cilium/charts/cilium --version $(CILIUM_VERSION) --destination $(CILIUM_CHART_DIR)
	$(HELM) lint $(CILIUM_CHART) --values .generated/helm/cilium.values.yaml
	$(HELM) template cilium $(CILIUM_CHART) --namespace kube-system --values .generated/helm/cilium.values.yaml >/dev/null

.PHONY: cilium-install
cilium-install: cilium-host-firewall cilium-render
	sh tools/install-cilium.sh $(HELM) $(KUBECONFIG) $(CILIUM_VERSION) .generated/helm/cilium.values.yaml
	$(MAKE) lab-dns-install

.PHONY: lab-dns-install
lab-dns-install: config-generate
	@$(KUBECTL) --kubeconfig $(KUBECONFIG) apply --server-side --field-manager=lap-core-dns --force-conflicts -f .generated/cluster/coredns.yaml >/dev/null
	@$(KUBECTL) --kubeconfig $(KUBECONFIG) -n kube-system rollout status deployment/coredns --timeout=3m >/dev/null

.PHONY: cilium-check
cilium-check: config-validate helm-install
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/validate-kubernetes-networking-host.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519
	sh tools/validate-cilium-networking.sh $(HELM) $(KUBECONFIG) $(CILIUM_VERSION) $(POD_CIDR) $(POD_NODE_CIDR_MASK) $(API_VIRTUAL_IP)

.PHONY: cilium-failure-test
cilium-failure-test: config-validate helm-install
	sh tools/validate-cilium-networking.sh $(HELM) $(KUBECONFIG) $(CILIUM_VERSION) $(POD_CIDR) $(POD_NODE_CIDR_MASK) $(API_VIRTUAL_IP) --failure

.PHONY: metallb-host-firewall
metallb-host-firewall: config-generate
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/metallb-networking-host.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: metallb-render
metallb-render: config-generate helm-install
	mkdir -p $(METALLB_CHART_DIR)
	$(HELM) pull metallb --repo https://metallb.github.io/metallb --version $(METALLB_VERSION) --destination $(METALLB_CHART_DIR)
	$(HELM) lint $(METALLB_CHART) --values .generated/helm/metallb.values.yaml
	$(HELM) template metallb $(METALLB_CHART) --namespace metallb-system --values .generated/helm/metallb.values.yaml >/dev/null
	$(KUBECTL) kustomize cluster/metallb >/dev/null
	$(KUBECTL) kustomize cluster/validation/metallb >/dev/null

.PHONY: metallb-install
metallb-install: metallb-host-firewall metallb-render
	sh tools/install-metallb.sh $(HELM) $(KUBECONFIG) $(METALLB_VERSION) .generated/helm/metallb.values.yaml cluster/metallb

.PHONY: metallb-check
metallb-check: config-validate helm-install
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/validate-metallb-networking-host.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519
	$(KUBECTL) --kubeconfig $(KUBECONFIG) apply --server-side --field-manager=lap-kustomize --dry-run=server --kustomize cluster/metallb >/dev/null
	sh tools/validate-metallb.sh $(HELM) $(KUBECONFIG) $(METALLB_VERSION) $(METALLB_POOL_START) $(METALLB_POOL_END) $(ENVOY_GATEWAY_IP) $(METALLB_TEST_IP) $(API_VIRTUAL_IP)

.PHONY: metallb-failure-test
metallb-failure-test: config-validate helm-install
	sh tools/validate-metallb.sh $(HELM) $(KUBECONFIG) $(METALLB_VERSION) $(METALLB_POOL_START) $(METALLB_POOL_END) $(ENVOY_GATEWAY_IP) $(METALLB_TEST_IP) $(API_VIRTUAL_IP) --failure

.PHONY: gateway-api-install
gateway-api-install: config-generate
	sh tools/install-gateway-api.sh $(KUBECTL) $(KUBECONFIG) $(GATEWAY_API_VERSION) $(GATEWAY_API_STANDARD_SHA256)

.PHONY: envoy-gateway-render
envoy-gateway-render: config-generate helm-install
	mkdir -p $(ENVOY_GATEWAY_CHART_DIR)
	$(HELM) pull oci://docker.io/envoyproxy/gateway-crds-helm --version v$(ENVOY_GATEWAY_VERSION) --destination $(ENVOY_GATEWAY_CHART_DIR)
	$(HELM) pull oci://docker.io/envoyproxy/gateway-helm --version v$(ENVOY_GATEWAY_VERSION) --destination $(ENVOY_GATEWAY_CHART_DIR)
	$(HELM) lint $(ENVOY_GATEWAY_CRDS_CHART) --values .generated/helm/envoy-gateway-crds.values.yaml
	$(HELM) lint $(ENVOY_GATEWAY_CHART) --values .generated/helm/envoy-gateway.values.yaml
	$(HELM) template envoy-gateway-crds $(ENVOY_GATEWAY_CRDS_CHART) --namespace envoy-gateway-system --values .generated/helm/envoy-gateway-crds.values.yaml >/dev/null
	$(HELM) template envoy-gateway $(ENVOY_GATEWAY_CHART) --namespace envoy-gateway-system --values .generated/helm/envoy-gateway.values.yaml >/dev/null
	$(KUBECTL) kustomize cluster/gateway >/dev/null
	$(KUBECTL) kustomize cluster/validation/gateway >/dev/null

.PHONY: envoy-gateway-install
envoy-gateway-install: gateway-api-install envoy-gateway-render
	sh tools/install-envoy-gateway.sh $(HELM) $(KUBECTL) $(KUBECONFIG) $(ENVOY_GATEWAY_VERSION) .generated/helm/envoy-gateway-crds.values.yaml .generated/helm/envoy-gateway.values.yaml cluster/gateway

.PHONY: envoy-gateway-check
envoy-gateway-check: config-validate helm-install
	$(KUBECTL) --kubeconfig $(KUBECONFIG) apply --server-side --field-manager=lap-kustomize --dry-run=server --kustomize cluster/gateway >/dev/null
	sh tools/validate-gateway-routing.sh $(HELM) $(KUBECONFIG) $(ENVOY_GATEWAY_VERSION) $(GATEWAY_API_VERSION) $(ENVOY_GATEWAY_IP) $(API_VIRTUAL_IP) gateway-smoke.$(APPLICATION_DOMAIN)

.PHONY: envoy-gateway-failure-test
envoy-gateway-failure-test: config-validate helm-install
	sh tools/validate-gateway-routing.sh $(HELM) $(KUBECONFIG) $(ENVOY_GATEWAY_VERSION) $(GATEWAY_API_VERSION) $(ENVOY_GATEWAY_IP) $(API_VIRTUAL_IP) gateway-smoke.$(APPLICATION_DOMAIN) --failure

.PHONY: lab-root-trust
lab-root-trust: config-generate
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/prepare-lab-pki.ps1 -EnsureRoot -TrustRoot
	wsl.exe -d Ubuntu-24.04 -u root -- sh $(abspath tools/install-root-trust-wsl.sh) $(abspath .generated/pki/lab-root-ca.crt)
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/lab-root-trust.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: cert-manager-render
cert-manager-render: config-generate helm-install
	mkdir -p $(CERT_MANAGER_CHART_DIR)
	$(HELM) pull oci://quay.io/jetstack/charts/cert-manager --version v$(CERT_MANAGER_VERSION) --destination $(CERT_MANAGER_CHART_DIR)
	$(HELM) lint $(CERT_MANAGER_CHART) --values .generated/helm/cert-manager.values.yaml
	$(HELM) template cert-manager $(CERT_MANAGER_CHART) --namespace cert-manager --values .generated/helm/cert-manager.values.yaml >/dev/null
	$(KUBECTL) kustomize cluster/pki >/dev/null
	$(KUBECTL) kustomize cluster/gateway >/dev/null
	$(KUBECTL) kustomize cluster/validation/tls >/dev/null

.PHONY: cert-manager-install
cert-manager-install: lab-root-trust cert-manager-render
	sh tools/install-cert-manager.sh $(HELM) $(KUBECONFIG) $(CERT_MANAGER_VERSION) .generated/helm/cert-manager.values.yaml $(CERT_MANAGER_CHART)
	sh tools/install-cluster-pki.sh $(KUBECTL) $(KUBECONFIG) cluster/pki
	$(KUBECTL) --kubeconfig $(KUBECONFIG) apply --server-side --field-manager=lap-kustomize --kustomize cluster/gateway
	$(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace envoy-gateway-system wait --for=condition=Programmed gateway/platform --timeout=5m

.PHONY: cert-manager-check
cert-manager-check: config-validate helm-install
	@$(VALIDATION_RUNNER) --stage 'private PKI server-side validation' --timeout 120 --kubectl $(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace cert-manager -- $(KUBECTL) --kubeconfig $(KUBECONFIG) apply --server-side --field-manager=lap-kustomize --dry-run=server --kustomize cluster/pki
	@$(VALIDATION_RUNNER) --stage 'Gateway TLS server-side validation' --timeout 120 --kubectl $(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace envoy-gateway-system -- $(KUBECTL) --kubeconfig $(KUBECONFIG) apply --server-side --field-manager=lap-kustomize --dry-run=server --kustomize cluster/gateway
	@$(VALIDATION_RUNNER) --stage 'trusted HTTPS and certificate chain' --timeout 900 --kubectl $(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace cert-manager -- sh tools/validate-cert-manager-tls.sh $(HELM) $(KUBECONFIG) $(CERT_MANAGER_VERSION) $(ENVOY_GATEWAY_IP) $(API_VIRTUAL_IP) tls-smoke.$(APPLICATION_DOMAIN) .generated/pki/lab-root-ca.crt

.PHONY: cert-manager-failure-test
cert-manager-failure-test: config-validate helm-install
	sh tools/validate-cert-manager-tls.sh $(HELM) $(KUBECONFIG) $(CERT_MANAGER_VERSION) $(ENVOY_GATEWAY_IP) $(API_VIRTUAL_IP) tls-smoke.$(APPLICATION_DOMAIN) .generated/pki/lab-root-ca.crt --failure

.PHONY: longhorn-host-prepare
longhorn-host-prepare: config-generate
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/longhorn-host-prepare.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: longhorn-render
longhorn-render: config-generate helm-install
	mkdir -p $(LONGHORN_CHART_DIR)
	$(HELM) pull longhorn --repo https://charts.longhorn.io --version $(LONGHORN_VERSION) --destination $(LONGHORN_CHART_DIR)
	$(HELM) lint $(LONGHORN_CHART) --values .generated/helm/longhorn.values.yaml
	$(HELM) template longhorn $(LONGHORN_CHART) --namespace longhorn-system --values .generated/helm/longhorn.values.yaml >/dev/null
	$(KUBECTL) kustomize cluster/longhorn >/dev/null
	$(KUBECTL) kustomize cluster/validation/longhorn >/dev/null

.PHONY: longhorn-install
longhorn-install: longhorn-host-prepare longhorn-render
	sh tools/install-longhorn.sh $(HELM) $(KUBECTL) $(KUBECONFIG) $(LONGHORN_VERSION) .generated/helm/longhorn.values.yaml cluster/longhorn $(LONGHORN_WORKERS)

.PHONY: longhorn-check
longhorn-check: config-validate longhorn-render
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/validate-longhorn-host-preparation.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519
	$(KUBECTL) --kubeconfig $(KUBECONFIG) apply --server-side --field-manager=lap-kustomize --dry-run=server --kustomize cluster/longhorn >/dev/null
	sh tools/validate-longhorn-storage.sh $(HELM) $(KUBECONFIG) $(LONGHORN_VERSION) cluster/validation/longhorn normal $(LONGHORN_WORKERS)

.PHONY: longhorn-failure-test
longhorn-failure-test: config-validate longhorn-render
	sh tools/validate-longhorn-storage.sh $(HELM) $(KUBECONFIG) $(LONGHORN_VERSION) cluster/validation/longhorn --failure $(LONGHORN_WORKERS)

.PHONY: postgresql-render
postgresql-render: config-generate
	$(KUBECTL) kustomize cluster/postgresql >/dev/null
	$(KUBECTL) kustomize cluster/validation/postgresql >/dev/null

.PHONY: postgresql-install
postgresql-install: postgresql-render
	sh tools/install-postgresql.sh $(KUBECTL) $(KUBECONFIG) cluster/postgresql $(POSTGRESQL_NAMESPACE) $(POSTGRESQL_DATABASE) $(POSTGRESQL_APPLICATION_USER) $(POSTGRESQL_VERSION) $(POSTGRESQL_SECRET_DIR)/postgresql-administrative-password $(POSTGRESQL_SECRET_DIR)/postgresql-application-password

.PHONY: postgresql-check
postgresql-check: config-validate postgresql-render
	$(KUBECTL) --kubeconfig $(KUBECONFIG) apply --server-side --field-manager=lap-kustomize --dry-run=server --kustomize cluster/postgresql >/dev/null
	sh tools/validate-postgresql.sh $(KUBECTL) $(KUBECONFIG) $(POSTGRESQL_VERSION) cluster/validation/postgresql .generated/pki/lab-root-ca.crt $(POSTGRESQL_SECRET_DIR)/postgresql-application-password $(POSTGRESQL_DATABASE) $(POSTGRESQL_APPLICATION_USER) $(POSTGRESQL_NAMESPACE) $(POSTGRESQL_SERVICE)

.PHONY: postgresql-idempotence
postgresql-idempotence: config-generate
	sh tools/validate-postgresql-idempotence.sh $(KUBECTL) $(KUBECONFIG) cluster/postgresql $(POSTGRESQL_NAMESPACE) $(POSTGRESQL_DATABASE) $(POSTGRESQL_APPLICATION_USER) $(POSTGRESQL_SECRET_DIR)/postgresql-administrative-password $(POSTGRESQL_SECRET_DIR)/postgresql-application-password $(POSTGRESQL_VERSION)

.PHONY: observability-render
observability-render: config-generate helm-install
	mkdir -p $(KUBE_PROMETHEUS_STACK_CHART_DIR)
	$(HELM) pull oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack --version $(KUBE_PROMETHEUS_STACK_VERSION) --destination $(KUBE_PROMETHEUS_STACK_CHART_DIR)
	$(HELM) lint $(KUBE_PROMETHEUS_STACK_CHART) --values .generated/helm/kube-prometheus-stack.values.yaml
	$(HELM) template kube-prometheus-stack $(KUBE_PROMETHEUS_STACK_CHART) --namespace $(OBSERVABILITY_NAMESPACE) --values .generated/helm/kube-prometheus-stack.values.yaml >/dev/null
	mkdir -p $(LOKI_CHART_DIR) $(TEMPO_CHART_DIR)
	$(HELM) pull oci://ghcr.io/grafana-community/helm-charts/loki --version $(LOKI_CHART_VERSION) --destination $(LOKI_CHART_DIR)
	$(HELM) pull oci://ghcr.io/grafana-community/helm-charts/tempo --version $(TEMPO_CHART_VERSION) --destination $(TEMPO_CHART_DIR)
	$(HELM) lint $(LOKI_CHART) --values .generated/helm/loki.values.yaml
	$(HELM) lint $(TEMPO_CHART) --values .generated/helm/tempo.values.yaml
	$(HELM) template loki $(LOKI_CHART) --namespace $(OBSERVABILITY_NAMESPACE) --values .generated/helm/loki.values.yaml >/dev/null
	$(HELM) template tempo $(TEMPO_CHART) --namespace $(OBSERVABILITY_NAMESPACE) --values .generated/helm/tempo.values.yaml >/dev/null
	$(KUBECTL) kustomize cluster/observability >/dev/null
	$(KUBECTL) kustomize cluster/validation/observability >/dev/null
	$(KUBECTL) kustomize cluster/validation/observability-part-two >/dev/null
	$(UV) run python -m json.tool cluster/observability/dashboards/cluster-health.json >/dev/null
	$(UV) run python -m json.tool cluster/observability/dashboards/platform-health.json >/dev/null
	$(UV) run python -m json.tool cluster/observability/dashboards/telemetry-pipelines.json >/dev/null

.PHONY: observability-host-firewall
observability-host-firewall: config-generate
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/observability-host.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: observability-install
observability-install: observability-host-firewall observability-render
	sh tools/install-observability.sh $(HELM) $(KUBECTL) $(KUBECONFIG) $(KUBE_PROMETHEUS_STACK_VERSION) .generated/helm/kube-prometheus-stack.values.yaml $(KUBE_PROMETHEUS_STACK_CHART) cluster/observability cluster/gateway $(OBSERVABILITY_NAMESPACE) $(OBSERVABILITY_SECRET_DIR)/grafana-administrator-password
	sh tools/install-observability-part-two.sh $(HELM) $(KUBECTL) $(KUBECONFIG) $(LOKI_VERSION) $(LOKI_CHART_VERSION) .generated/helm/loki.values.yaml $(LOKI_CHART) $(TEMPO_VERSION) $(TEMPO_CHART_VERSION) .generated/helm/tempo.values.yaml $(TEMPO_CHART) cluster/observability $(OBSERVABILITY_NAMESPACE)

.PHONY: observability-check
observability-check: config-validate
	@$(VALIDATION_RUNNER) --stage 'observability chart rendering' --timeout 1800 -- $(MAKE) --no-print-directory observability-render
	@$(VALIDATION_RUNNER) --stage 'observability host readiness' --timeout 900 -- sh -c 'cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/validate-observability-host.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519'
	@$(VALIDATION_RUNNER) --stage 'observability server-side validation' --timeout 120 --kubectl $(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace $(OBSERVABILITY_NAMESPACE) -- $(KUBECTL) --kubeconfig $(KUBECONFIG) apply --server-side --field-manager=lap-kustomize --dry-run=server --kustomize cluster/observability
	@$(VALIDATION_RUNNER) --stage 'observability metrics and dashboards' --timeout 1800 --kubectl $(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace $(OBSERVABILITY_NAMESPACE) -- sh tools/validate-observability.sh $(HELM) $(KUBECTL) $(KUBECONFIG) $(KUBE_PROMETHEUS_STACK_VERSION) .generated/pki/lab-root-ca.crt $(ENVOY_GATEWAY_IP) $(API_VIRTUAL_IP) $(GRAFANA_HOST) $(OBSERVABILITY_SECRET_DIR)/grafana-administrator-password cluster/validation/observability normal
	@$(VALIDATION_RUNNER) --stage 'observability logs and traces' --timeout 1800 --kubectl $(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace $(OBSERVABILITY_NAMESPACE) -- sh tools/validate-observability-part-two.sh $(HELM) $(KUBECTL) $(KUBECONFIG) $(LOKI_VERSION) $(LOKI_CHART_VERSION) $(TEMPO_VERSION) $(TEMPO_CHART_VERSION) $(OTEL_COLLECTOR_VERSION) $(OBSERVABILITY_NAMESPACE) $(OBSERVABILITY_SECRET_DIR)/grafana-administrator-password cluster/validation/observability-part-two normal

.PHONY: observability-failure-test
observability-failure-test: config-validate
	@$(VALIDATION_RUNNER) --stage 'observability chart rendering' --timeout 1800 -- $(MAKE) --no-print-directory observability-render
	@$(VALIDATION_RUNNER) --stage 'observability collector recovery' --timeout 2400 --kubectl $(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace $(OBSERVABILITY_NAMESPACE) -- sh tools/validate-observability-part-two.sh $(HELM) $(KUBECTL) $(KUBECONFIG) $(LOKI_VERSION) $(LOKI_CHART_VERSION) $(TEMPO_VERSION) $(TEMPO_CHART_VERSION) $(OTEL_COLLECTOR_VERSION) $(OBSERVABILITY_NAMESPACE) $(OBSERVABILITY_SECRET_DIR)/grafana-administrator-password cluster/validation/observability-part-two --failure

.PHONY: observability-idempotence
observability-idempotence: config-generate helm-install
	sh tools/validate-observability-idempotence.sh $(HELM) $(KUBECTL) $(KUBECONFIG) $(KUBE_PROMETHEUS_STACK_VERSION) .generated/helm/kube-prometheus-stack.values.yaml $(KUBE_PROMETHEUS_STACK_CHART) cluster/observability cluster/gateway $(OBSERVABILITY_NAMESPACE) $(OBSERVABILITY_SECRET_DIR)/grafana-administrator-password
	sh tools/validate-observability-part-two-idempotence.sh $(HELM) $(KUBECTL) $(KUBECONFIG) $(LOKI_VERSION) $(LOKI_CHART_VERSION) .generated/helm/loki.values.yaml $(LOKI_CHART) $(TEMPO_VERSION) $(TEMPO_CHART_VERSION) .generated/helm/tempo.values.yaml $(TEMPO_CHART) cluster/observability $(OBSERVABILITY_NAMESPACE)

.PHONY: build-host-pki
build-host-pki: config-generate
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/prepare-lab-pki.ps1 -EnsureRoot
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/prepare-build-host-pki.ps1 -Hostname $(REGISTRY_HOST)
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/prepare-build-agent-pki.ps1

.PHONY: build-host-install
build-host-install: build-host-pki
	mkdir -p $(BUILD_HOST_SECRET_DIR)
	chmod 0700 $(BUILD_HOST_SECRET_DIR)
	cd infrastructure/ansible && BUILD_HOST_SECRET_DIR=$(BUILD_HOST_SECRET_DIR) BUILD_HOST_PKI_DIR=$(BUILD_HOST_PKI_DIR) ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/build-host.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519

.PHONY: build-agent-telemetry-install
build-agent-telemetry-install: config-generate
	cd infrastructure/ansible && ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/kubernetes-networking-host.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519
	$(KUBECTL) --kubeconfig $(KUBECONFIG) apply --server-side --field-manager=lap-kustomize --kustomize cluster/observability
	$(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace $(OBSERVABILITY_NAMESPACE) rollout status daemonset/opentelemetry-collector --timeout=5m

.PHONY: build-agent-install
build-agent-install: build-agent-telemetry-install build-host-install

.PHONY: build-agent-check
build-agent-check: config-validate
	@$(VALIDATION_RUNNER) --stage 'build-agent host configuration' --timeout 1200 -- sh -c 'cd infrastructure/ansible && BUILD_HOST_SECRET_DIR=$(BUILD_HOST_SECRET_DIR) BUILD_HOST_PKI_DIR=$(BUILD_HOST_PKI_DIR) ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/validate-build-agent.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519'
	@$(VALIDATION_RUNNER) --stage 'build-agent mTLS and URL policy' --timeout 120 -- sh tools/validate-build-agent.sh build-agent.lab.test $(REGISTRY_ADDRESS) 8443 .generated/pki/lab-root-ca.crt .generated/pki/platform-api-build-agent-client.crt .generated/pki/platform-api-build-agent-client.key "$${BUILD_AGENT_TEST_REPOSITORY:-}" "$${BUILD_AGENT_TEST_REVISION:-main}"
	@if test -n "$${BUILD_AGENT_TEST_REPOSITORY:-}"; then $(VALIDATION_RUNNER) --stage 'build-agent artifact pipeline' --timeout 1800 -- sh tools/validate-build-agent-pipeline.sh build-agent.lab.test $(REGISTRY_ADDRESS) 8443 .generated/pki/lab-root-ca.crt .generated/pki/platform-api-build-agent-client.crt .generated/pki/platform-api-build-agent-client.key "$${BUILD_AGENT_TEST_REPOSITORY}" $(HOME)/.ssh/lap_lab_ed25519; fi
	@$(VALIDATION_RUNNER) --stage 'build-agent concurrency' --timeout 1200 -- sh tools/validate-build-agent-concurrency.sh $(HOME)/.ssh/lap_lab_ed25519 ubuntu $(REGISTRY_ADDRESS) build-agent.lab.test 8443 .generated/pki/lab-root-ca.crt .generated/pki/platform-api-build-agent-client.crt .generated/pki/platform-api-build-agent-client.key
	@$(VALIDATION_RUNNER) --stage 'build-agent telemetry' --timeout 180 --kubectl $(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace $(OBSERVABILITY_NAMESPACE) -- sh tools/validate-build-agent-telemetry.sh $(KUBECTL) $(KUBECONFIG) $(OBSERVABILITY_NAMESPACE)

.PHONY: build-host-check
build-host-check: config-validate
	@$(VALIDATION_RUNNER) --stage 'registry host security configuration' --timeout 1200 -- sh -c 'cd infrastructure/ansible && BUILD_HOST_SECRET_DIR=$(BUILD_HOST_SECRET_DIR) BUILD_HOST_PKI_DIR=$(BUILD_HOST_PKI_DIR) ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/validate-build-host.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519'
	@$(VALIDATION_RUNNER) --stage 'registry unauthenticated access policy' --timeout 60 -- sh -c 'status=$$(curl --noproxy "*" --connect-timeout 5 --silent --output /dev/null --write-out "%{http_code}" --resolve $(REGISTRY_HOST):$(REGISTRY_PORT):$(REGISTRY_ADDRESS) https://$(REGISTRY_HOST):$(REGISTRY_PORT)/v2/); test "$$status" = 401'
	@$(VALIDATION_RUNNER) --stage 'Windows registry TLS trust' --timeout 180 -- powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/validate-windows-registry-tls.ps1 -Hostname $(REGISTRY_HOST) -Address $(REGISTRY_ADDRESS) -DnsServer $(API_VIRTUAL_IP) -Port $(REGISTRY_PORT)

.PHONY: build-host-idempotence
build-host-idempotence: config-validate
	bash tools/validate-build-host-idempotence.sh $(BUILD_HOST_SECRET_DIR) $(BUILD_HOST_PKI_DIR) $(ANSIBLE_VERSION) $(HOME)/.ssh/lap_lab_ed25519

.PHONY: static-checks
static-checks: config-generate
	sh -n tools/fetch-ubuntu-vhd-archive.sh
	sh -n tools/install-helm.sh
	sh -n tools/install-cilium.sh
	sh -n tools/install-metallb.sh
	sh -n tools/install-gateway-api.sh
	sh -n tools/install-envoy-gateway.sh
	sh -n tools/install-cert-manager.sh
	sh -n tools/install-cluster-pki.sh
	sh -n tools/install-longhorn.sh
	sh -n tools/validate-longhorn-storage.sh
	sh -n tools/install-postgresql.sh
	sh -n tools/validate-postgresql.sh
	sh -n tools/rotate-postgresql-credentials.sh
	sh -n tools/validate-postgresql-idempotence.sh
	sh -n tools/install-observability.sh
	sh -n tools/validate-observability.sh
	sh -n tools/validate-observability-idempotence.sh
	sh -n tools/install-observability-part-two.sh
	sh -n tools/validate-observability-part-two.sh
	sh -n tools/validate-observability-part-two-idempotence.sh
	sh -n tools/validate-build-host-idempotence.sh
	sh -n tools/validate-build-agent.sh
	sh -n tools/validate-build-agent-concurrency.sh
	sh -n tools/validate-build-agent-pipeline.sh
	sh -n tools/validate-build-agent-telemetry.sh
	bash -n tools/build-platform-api-image.sh
	sh -n tools/install-platform-api.sh
	sh -n tools/validate-platform-api.sh
	sh -n tools/validate-platform-api-telemetry.sh
	sh -n tools/validate-platform-api-database.sh
	sh -n tools/validate-platform-api-build-dispatch.sh
	sh -n tools/install-go.sh
	sh -n tools/install-controller-envtest.sh
	sh -n tools/install-gateway-api-envtest-crds.sh
	bash -n tools/build-platform-controller-image.sh
	bash -n tools/build-platform-controller-runtime-fixture.sh
	bash -n tools/install-platform-controller.sh
	bash -n tools/validate-platform-controller.sh
	sh -n tools/validate-platform-controller-telemetry.sh
	sh -n tools/install-root-trust-wsl.sh
	sh -n tools/install-terraform-windows.sh
	sh -n tools/validate-cilium-networking.sh
	sh -n tools/validate-metallb.sh
	sh -n tools/validate-gateway-routing.sh
	sh -n tools/validate-cert-manager-tls.sh
	bash -n tools/prepare-nocloud-root.sh
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/check-powershell.ps1
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/test-image-preparation.ps1
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/test-image-workflow.ps1
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/test-lab-routing.ps1
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/test-ensure-lab-routing.ps1
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/validate-windows-registry-tls.ps1 -ParseOnly

.PHONY: terraform-test
terraform-test: config-generate terraform-init-validate
	$(TERRAFORM) -chdir=infrastructure/terraform test -var-file=$(TERRAFORM_TFVARS)

.PHONY: lab-up
lab-up: config-generate terraform-init ubuntu-image-verify
	$(TERRAFORM) -chdir=infrastructure/terraform apply -auto-approve -var-file=$(TERRAFORM_TFVARS)

.PHONY: lab-backup
lab-backup: config-validate config-generate
	@$(VALIDATION_RUNNER) --stage lab-backup --timeout 1800 -- $(UV) run --all-packages python tools/lab_backup.py backup

.PHONY: lab-restore
lab-restore: config-validate config-generate
	@test -n "$(LAB_BACKUP_ARCHIVE)" || { printf '%s\n' 'LAB_BACKUP_ARCHIVE is required' >&2; exit 2; }
	@$(VALIDATION_RUNNER) --stage lab-restore --timeout 1800 -- $(UV) run --all-packages python tools/lab_backup.py restore "$(LAB_BACKUP_ARCHIVE)" $(LAB_RESTORE_CONFIRM) $(LAB_RESTORE_RESUME)

.PHONY: lab-backup-check
lab-backup-check:
	@test -n "$(LAB_BACKUP_ARCHIVE)" || { printf '%s\n' 'LAB_BACKUP_ARCHIVE is required' >&2; exit 2; }
	@$(VALIDATION_RUNNER) --stage lab-backup-verify --timeout 300 -- $(UV) run --all-packages python tools/lab_backup.py verify "$(LAB_BACKUP_ARCHIVE)"
	@$(VALIDATION_RUNNER) --stage lab-backup-restore-isolated --timeout 900 -- $(UV) run --all-packages python tools/lab_backup.py acceptance "$(LAB_BACKUP_ARCHIVE)"

.PHONY: lab-backup-restore-acceptance
lab-backup-restore-acceptance:
	@test -n "$(LAB_BACKUP_TEST_REPOSITORY)" -a -n "$(LAB_BACKUP_TEST_APPLICATION)" || { printf '%s\n' 'LAB_BACKUP_TEST_REPOSITORY and LAB_BACKUP_TEST_APPLICATION are required' >&2; exit 2; }
	@$(VALIDATION_RUNNER) --stage lab-backup-restore-acceptance --timeout 2400 -- $(UV) run --all-packages python tools/validate_lab_backup_restore.py "$(LAB_BACKUP_TEST_REPOSITORY)" "$(LAB_BACKUP_TEST_APPLICATION)" $(LAB_RESTORE_CONFIRM)

.PHONY: v1-acceptance
v1-acceptance:
	@test -n "$(V1_TEST_REPOSITORY)" -a -n "$(V1_TEST_APPLICATION)" || { printf '%s\n' 'V1_TEST_REPOSITORY and V1_TEST_APPLICATION are required' >&2; exit 2; }
	@$(VALIDATION_RUNNER) --stage v1-end-to-end-acceptance --timeout 3600 -- $(UV) run --all-packages python tools/validate_v1_acceptance.py "$(V1_TEST_REPOSITORY)" "$(V1_TEST_APPLICATION)"

.PHONY: lab-destroy
lab-destroy: config-generate terraform-init
	# Provider 0.3.1 cannot refresh a Gen-2 VM after guest UEFI adds its
	# null-device file boot entry. Destroy from the last applied state.
	$(TERRAFORM) -chdir=infrastructure/terraform destroy -refresh=false -auto-approve -var-file=$(TERRAFORM_TFVARS)

.PHONY: lab-status
lab-status: config-generate
	$(TERRAFORM) -chdir=infrastructure/terraform show

.PHONY: demo-up
demo-up:
	@printf '%s\n' 'demo-up is reserved for the Kind milestone.'

.PHONY: demo-down
demo-down:
	@printf '%s\n' 'demo-down is reserved for the Kind milestone.'

.PHONY: test
test: platform-controller-test
	$(UV) run pytest -m 'not kind and not full_lab and not destructive'

.PHONY: go-install
go-install:
	sh tools/install-go.sh $(GO_VERSION) $(GO_LOCAL_DIR) $(GO_SHA256)

.PHONY: controller-gen-install
controller-gen-install: go-install
	mkdir -p .local-tools/bin
	cd platform/controller && GOTOOLCHAIN=local GOBIN=$(abspath .local-tools/bin) $(GO) install sigs.k8s.io/controller-tools/cmd/controller-gen@$(CONTROLLER_TOOLS_VERSION)

.PHONY: platform-controller-generate
platform-controller-generate: controller-gen-install
	cd platform/controller && PATH=$(abspath $(GO_LOCAL_DIR)/bin):$$PATH $(CONTROLLER_GEN) object:headerFile=/dev/null paths=./api/...
	cd platform/controller && PATH=$(abspath $(GO_LOCAL_DIR)/bin):$$PATH $(CONTROLLER_GEN) crd paths=./api/... output:crd:artifacts:config=../../cluster/platform-controller

.PHONY: controller-envtest-install
controller-envtest-install:
	sh tools/install-controller-envtest.sh $(ENVTEST_VERSION) $(ENVTEST_DIR) $(ENVTEST_SHA256)

.PHONY: gateway-api-envtest-install
gateway-api-envtest-install:
	sh tools/install-gateway-api-envtest-crds.sh $(GATEWAY_API_VERSION) $(GATEWAY_API_ENVTEST_DIR) $(GATEWAY_API_STANDARD_SHA256)

.PHONY: platform-controller-test
platform-controller-test: go-install controller-envtest-install gateway-api-envtest-install
	cd platform/controller && GOTOOLCHAIN=local KUBEBUILDER_ASSETS=$(ENVTEST_DIR) GATEWAY_API_CRD_DIRECTORY=$(GATEWAY_API_ENVTEST_DIR) $(GO) test ./... -count=1

.PHONY: platform-controller-check
platform-controller-check: platform-controller-generate controller-envtest-install gateway-api-envtest-install
	test -z "$$($(abspath $(GO_LOCAL_DIR)/bin/gofmt) -l platform/controller)"
	cd platform/controller && GOTOOLCHAIN=local $(GO) vet ./...
	cd platform/controller && GOTOOLCHAIN=local KUBEBUILDER_ASSETS=$(ENVTEST_DIR) GATEWAY_API_CRD_DIRECTORY=$(GATEWAY_API_ENVTEST_DIR) $(GO) test ./... -count=1

.PHONY: platform-controller-image
platform-controller-image:
	bash tools/build-platform-controller-image.sh 10.20.0.51 $(HOME)/.ssh/lap_lab_ed25519

.PHONY: platform-controller-runtime-fixture-image
platform-controller-runtime-fixture-image:
	bash tools/build-platform-controller-runtime-fixture.sh 10.20.0.51 $(HOME)/.ssh/lap_lab_ed25519

.PHONY: platform-controller-render
platform-controller-render: config-generate
	$(KUBECTL) kustomize cluster/platform-controller >/dev/null

.PHONY: platform-controller-install
platform-controller-install: platform-controller-render
	bash tools/install-platform-controller.sh $(KUBECTL) $(KUBECONFIG) cluster/platform-controller $(BUILD_HOST_SECRET_DIR)/registry-puller-password $(BUILD_HOST_PKI_DIR)/lab-root-ca.crt

.PHONY: platform-controller-live-check
platform-controller-live-check:
	@$(VALIDATION_RUNNER) --stage 'controller runtime and recovery' --timeout 1800 --kubectl $(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace platform-system --deployment platform-controller --selector app.kubernetes.io/name=platform-controller --logs-selector app.kubernetes.io/name=platform-controller -- bash -c 'image=$$(bash tools/build-platform-controller-runtime-fixture.sh 10.20.0.51 $(HOME)/.ssh/lap_lab_ed25519); bash tools/validate-platform-controller.sh $(KUBECTL) $(KUBECONFIG) "$$image" $(BUILD_HOST_PKI_DIR)/lab-root-ca.crt; sh tools/validate-platform-controller-telemetry.sh $(KUBECTL) $(KUBECONFIG)'

.PHONY: platform-api-check
platform-api-check:
	$(UV) run --all-packages ruff check platform/api
	$(UV) run --all-packages mypy platform/api/src platform/api/tests
	$(UV) run --package platform-api pytest platform/api/tests -m unit

.PHONY: platform-api-image
platform-api-image:
	bash tools/build-platform-api-image.sh 10.20.0.51 $(HOME)/.ssh/lap_lab_ed25519

.PHONY: platform-api-render
platform-api-render: config-generate
	$(KUBECTL) kustomize cluster/platform-api >/dev/null

.PHONY: platform-api-install
platform-api-install: platform-api-render
	cd infrastructure/ansible && PLATFORM_API_PKI_DIR=$(BUILD_HOST_PKI_DIR) ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) $(ANSIBLE_PLAYBOOK) playbooks/platform-api-host.yaml --private-key $(HOME)/.ssh/lap_lab_ed25519
	bash tools/install-platform-api.sh $(KUBECTL) $(KUBECONFIG) cluster/platform-api $(PLATFORM_API_SECRET_DIR) .generated/pki $(BUILD_HOST_SECRET_DIR)

.PHONY: platform-api-migrate
platform-api-migrate:
	bash tools/migrate-platform-api.sh $(KUBECTL) $(KUBECONFIG) $(PLATFORM_API_SECRET_DIR) .generated/pki/lab-root-ca.crt

.PHONY: platform-api-runtime-check
platform-api-runtime-check:
	@$(VALIDATION_RUNNER) --stage 'API readiness' --timeout 180 --kubectl $(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace platform-system --deployment platform-api --selector app.kubernetes.io/name=platform-api --logs-selector app.kubernetes.io/name=platform-api -- bash tools/validate-platform-api.sh $(KUBECTL) $(KUBECONFIG) $(PLATFORM_API_SECRET_DIR)/platform-api-token

.PHONY: platform-api-scheduler-check
platform-api-scheduler-check:
	@LAP_VALIDATE_POD_RECOVERY=1 $(VALIDATION_RUNNER) --stage 'scheduler Pod-loss recovery' --timeout 900 --kubectl $(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace platform-system --deployment platform-api --selector app.kubernetes.io/name=platform-api --logs-selector app.kubernetes.io/name=platform-api -- sh tools/validate-platform-api-build-dispatch.sh $(KUBECTL) $(KUBECONFIG) $(PLATFORM_API_SECRET_DIR)/platform-api-token

.PHONY: platform-api-build-dispatch-check
platform-api-build-dispatch-check:
	@$(VALIDATION_RUNNER) --stage 'build dispatch and runtime acceptance' --timeout 1800 --kubectl $(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace platform-system --deployment platform-api --selector app.kubernetes.io/name=platform-api --logs-selector app.kubernetes.io/name=platform-api -- bash tools/validate-platform-api-build-dispatch.sh $(KUBECTL) $(KUBECONFIG) $(PLATFORM_API_SECRET_DIR)/platform-api-token "$${BUILD_AGENT_TEST_REPOSITORY:-}"

.PHONY: platform-api-rollback-check
platform-api-rollback-check:
	@LAP_VALIDATE_DEPLOYMENT_FAILURE=1 LAP_VALIDATE_ROLLBACK_EXPECTED=succeeded $(VALIDATION_RUNNER) --stage 'automatic rollback recovery' --timeout 1800 --kubectl $(KUBECTL) --kubeconfig $(KUBECONFIG) --namespace platform-system --deployment platform-api --selector app.kubernetes.io/name=platform-api --logs-selector app.kubernetes.io/name=platform-api -- bash tools/validate-platform-api-build-dispatch.sh $(KUBECTL) $(KUBECONFIG) $(PLATFORM_API_SECRET_DIR)/platform-api-token "$${BUILD_AGENT_TEST_REPOSITORY:?set BUILD_AGENT_TEST_REPOSITORY to a disposable build-agent fixture}"

.PHONY: test-e2e
test-e2e:
	$(UV) run pytest -m 'kind'

.PHONY: test-failures
test-failures:
	$(UV) run pytest -m 'full_lab and destructive'
