from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from lap_config.models import LabConfig, Versions


@dataclass(frozen=True)
class GeneratedFile:
    path: Path
    content: str


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load_config(repo_root: Path) -> tuple[LabConfig, Versions]:
    lab = LabConfig.model_validate(load_yaml(repo_root / "config" / "lab.yaml"))
    versions = Versions.model_validate(load_yaml(repo_root / "config" / "versions.yaml"))
    return lab, versions


def _json_default(value: object) -> str:
    return str(value)


def _to_plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _to_plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value


def render_files(lab: LabConfig, versions: Versions, repo_root: Path) -> list[GeneratedFile]:
    lab_dict = lab.model_dump(mode="json")
    versions_dict = versions.model_dump(mode="json")
    generated = repo_root / ".generated"

    terraform_vars = {
        "lab_name": lab.lab.name,
        "lab_domain": lab.lab.domain,
        "switch_name": lab.hyperv.switch_name,
        "nat_name": lab.hyperv.nat_name,
        "network_cidr": str(lab.network.vm_cidr),
        "network_gateway": str(lab.network.vm_gateway),
        "bootstrap_dns_servers": [str(ip) for ip in lab.network.bootstrap_dns_servers],
        "vhd_root_path": lab.hyperv.vhd_root_path,
        "seed_iso_root_path": lab.hyperv.seed_iso_root_path,
        "base_image_source": str(lab.hyperv.base_image_source),
        "base_image_name": lab.hyperv.base_image_name,
        "base_image_sha256": lab.hyperv.base_image_sha256,
        "base_image_capacity_gib": lab.hyperv.base_image_capacity_gib,
        "ssh_user": lab.hyperv.ssh_user,
        "ssh_public_key_path": lab.hyperv.ssh_public_key_path,
        "virtual_machines": [
            {
                "name": vm.name,
                "role": vm.role.value,
                "address": str(vm.address),
                "vcpu": vm.vcpu,
                "memory_mib": vm.memory_mib,
                "disk_gib": vm.disk_gib,
            }
            for vm in lab.virtual_machines
        ],
    }

    inventory_hosts = {
        vm.name: {
            "ansible_host": str(vm.address),
            "ansible_user": lab.hyperv.ssh_user,
            "lap_role": vm.role.value,
        }
        for vm in lab.virtual_machines
    }
    groups: dict[str, Any] = {
        "all": {"children": {}, "hosts": inventory_hosts},
        "load_balancers": {"hosts": {}},
        "control_planes": {"hosts": {}},
        "workers": {"hosts": {}},
        "build_machines": {"hosts": {}},
    }
    role_to_group = {
        "load_balancer": "load_balancers",
        "control_plane": "control_planes",
        "worker": "workers",
        "build": "build_machines",
    }
    for vm in lab.virtual_machines:
        groups[role_to_group[vm.role.value]]["hosts"][vm.name] = None
    groups["all"]["children"] = {group: None for group in role_to_group.values()}

    generated_vars = {"lab": lab_dict, "versions": versions_dict}
    env_lines = [
        f"LAB_DOMAIN={lab.lab.domain}",
        f"APPLICATION_DOMAIN={lab.lab.application_domain}",
        f"PLATFORM_API_HOST={lab.hostnames.platform_api}",
        f"REGISTRY_ENDPOINT={lab.registry.host}:{lab.registry.port}",
        f"BUILD_AGENT_URL={lab.build_agent.url}",
        f"POSTGRESQL_HOST={lab.postgresql.service_name}.{lab.postgresql.namespace}.svc.cluster.local",
        f"POSTGRESQL_DATABASE={lab.postgresql.database}",
        f"POSTGRESQL_APPLICATION_USER={lab.postgresql.application_user}",
        "OTEL_EXPORTER_OTLP_ENDPOINT="
        f"http://opentelemetry-collector.{lab.observability.namespace}.svc.cluster.local:4318",
    ]
    build_machine = next(vm for vm in lab.virtual_machines if vm.role.value == "build")
    make_lines = [
        f"LAB_NAME := {lab.lab.name}",
        f"LAB_NETWORK := {lab.hyperv.switch_name}",
        f"LAB_DOMAIN := {lab.lab.domain}",
        f"APPLICATION_DOMAIN := {lab.lab.application_domain}",
        f"API_VIRTUAL_IP := {lab.network.api_virtual_ip}",
        f"POD_CIDR := {lab.network.pod_cidr}",
        f"POD_NODE_CIDR_MASK := {lab.network.pod_node_cidr_mask}",
        f"CILIUM_VERSION := {versions.cilium}",
        f"METALLB_VERSION := {versions.metallb}",
        f"METALLB_POOL_START := {lab.network.metallb_pool_start}",
        f"METALLB_POOL_END := {lab.network.metallb_pool_end}",
        f"METALLB_TEST_IP := {lab.network.metallb_pool_start + 1}",
        f"ENVOY_GATEWAY_IP := {lab.network.envoy_gateway_ip}",
        f"ENVOY_GATEWAY_VERSION := {versions.envoy_gateway}",
        f"GATEWAY_API_VERSION := {versions.gateway_api}",
        f"CERT_MANAGER_VERSION := {versions.cert_manager}",
        f"LONGHORN_VERSION := {versions.longhorn}",
        f"POSTGRESQL_VERSION := {versions.postgresql}",
        f"REGISTRY_VERSION := {versions.registry}",
        f"REGISTRY_HOST := {lab.registry.host}",
        f"REGISTRY_ADDRESS := {build_machine.address}",
        f"REGISTRY_PORT := {lab.registry.port}",
        f"REGISTRY_NAMESPACE := {lab.registry.image_namespace}",
        f"BUILDKIT_VERSION := {versions.buildkit}",
        f"SKOPEO_VERSION := {versions.skopeo}",
        f"TRIVY_VERSION := {versions.trivy}",
        f"POSTGRESQL_NAMESPACE := {lab.postgresql.namespace}",
        f"POSTGRESQL_SERVICE := {lab.postgresql.service_name}",
        f"POSTGRESQL_DATABASE := {lab.postgresql.database}",
        f"POSTGRESQL_APPLICATION_USER := {lab.postgresql.application_user}",
        f"POSTGRESQL_STORAGE_CLASS := {lab.postgresql.storage_class}",
        f"POSTGRESQL_STORAGE_SIZE := {lab.postgresql.storage_size}",
        f"KUBE_PROMETHEUS_STACK_VERSION := {versions.kube_prometheus_stack_chart}",
        f"LOKI_VERSION := {versions.loki}",
        f"LOKI_CHART_VERSION := {versions.loki_chart}",
        f"TEMPO_VERSION := {versions.tempo}",
        f"TEMPO_CHART_VERSION := {versions.tempo_chart}",
        f"OTEL_COLLECTOR_VERSION := {versions.opentelemetry_collector_contrib}",
        f"OBSERVABILITY_NAMESPACE := {lab.observability.namespace}",
        f"GRAFANA_HOST := {lab.hostnames.grafana}",
        "LONGHORN_WORKERS := "
        + " ".join(vm.name for vm in lab.virtual_machines if vm.role.value == "worker"),
    ]

    helm_components = {
        "cilium": {
            "cluster": {"name": lab.lab.name},
            "enableIPv4Masquerade": True,
            "envoy": {"enabled": False},
            "hubble": {"enabled": False},
            "ipam": {"mode": "kubernetes"},
            "ipv4": {"enabled": True},
            "ipv6": {"enabled": False},
            "k8s": {"requireIPv4PodCIDR": True},
            "kubeProxyReplacement": False,
            "operator": {
                "prometheus": {"enabled": False},
                "replicas": 2,
            },
            "policyEnforcementMode": "default",
            "prometheus": {"enabled": False},
            "routingMode": "tunnel",
            "tunnelProtocol": "vxlan",
        },
        "metallb": {
            "controller": {"enabled": True, "logLevel": "info"},
            "crds": {"enabled": True, "validationFailurePolicy": "Fail"},
            "frrk8s": {"enabled": False},
            "prometheus": {
                "podMonitor": {"enabled": False},
                "prometheusRule": {"enabled": False},
                "rbacPrometheus": False,
                "scrapeAnnotations": False,
                "serviceMonitor": {"enabled": False},
            },
            "speaker": {
                "enabled": True,
                "frr": {"enabled": False},
                "ignoreExcludeLB": False,
                "logLevel": "info",
                "memberlist": {"enabled": True, "mlBindPort": 7946},
                "tolerateMaster": True,
            },
        },
        "envoy-gateway": {
            "config": {
                "envoyGateway": {
                    "extensionApis": {},
                    "gateway": {"controllerName": "gateway.envoyproxy.io/gatewayclass-controller"},
                    "logging": {"level": {"default": "info"}},
                    "provider": {"type": "Kubernetes"},
                }
            },
            "crds": {"enabled": False},
            "deployment": {"replicas": 1},
            "hpa": {"enabled": False},
            "service": {"type": "ClusterIP"},
        },
        "envoy-gateway-crds": {
            "crds": {
                "envoyGateway": {"enabled": True},
                "gatewayAPI": {"channel": "standard", "enabled": False},
            }
        },
        "cert-manager": {
            "crds": {"enabled": True, "keep": True},
            "prometheus": {"enabled": False},
            "replicaCount": 1,
            "webhook": {"replicaCount": 1},
            "cainjector": {"enabled": True, "replicaCount": 1},
            "startupapicheck": {"enabled": True},
        },
        "longhorn": {
            "defaultSettings": {
                "allowVolumeCreationWithDegradedAvailability": False,
                "createDefaultDiskLabeledNodes": True,
                "defaultDataPath": "/var/lib/longhorn",
                "defaultDataLocality": "best-effort",
                "defaultReplicaCount": '{"v1":"3","v2":"3"}',
                "replicaAutoBalance": "least-effort",
                "replicaSoftAntiAffinity": False,
                "storageMinimalAvailablePercentage": 25,
                "storageOverProvisioningPercentage": 100,
                "storageReservedPercentageForDefaultDisk": 30,
                "systemManagedComponentsNodeSelector": (
                    "node.longhorn.io/create-default-disk:true"
                ),
                "upgradeChecker": False,
                "v1DataEngine": True,
                "v2DataEngine": False,
            },
            "longhornDriver": {"nodeSelector": {"node.longhorn.io/create-default-disk": "true"}},
            "longhornManager": {"nodeSelector": {"node.longhorn.io/create-default-disk": "true"}},
            "networkPolicies": {
                "enabled": False,
                "restrictInternalTraffic": True,
            },
            "persistence": {
                "defaultClass": True,
                "defaultClassReplicaCount": 3,
                "defaultDataLocality": "best-effort",
                "defaultFsType": "ext4",
                "reclaimPolicy": "Delete",
                "volumeBindingMode": "WaitForFirstConsumer",
            },
        },
        "kube-prometheus-stack": {
            "alertmanager": {
                "alertmanagerSpec": {
                    "nodeSelector": {"node.longhorn.io/create-default-disk": "true"},
                    "replicas": 1,
                    "resources": {
                        "limits": {"cpu": "500m", "memory": "512Mi"},
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                    },
                    "retention": "120h",
                    "storage": {
                        "volumeClaimTemplate": {
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "resources": {
                                    "requests": {
                                        "storage": lab.observability.alertmanager_storage_size
                                    }
                                },
                                "storageClassName": "longhorn",
                            }
                        }
                    },
                },
                "enabled": True,
                "service": {"type": "ClusterIP"},
            },
            "crds": {"enabled": True, "upgradeJob": {"enabled": False}},
            "defaultRules": {"create": False},
            "grafana": {
                "additionalDataSources": [
                    {
                        "access": "proxy",
                        "isDefault": False,
                        "jsonData": {
                            "derivedFields": [
                                {
                                    "datasourceUid": "tempo",
                                    "matcherRegex": '"trace_id"[=: ]+"?([0-9a-f]{32})',
                                    "name": "TraceID",
                                    "url": "$${__value.raw}",
                                }
                            ],
                            "maxLines": 1000,
                        },
                        "name": "Loki",
                        "type": "loki",
                        "uid": "loki",
                        "url": "http://loki.monitoring.svc.cluster.local:3100",
                    },
                    {
                        "access": "proxy",
                        "isDefault": False,
                        "jsonData": {
                            "nodeGraph": {"enabled": True},
                            "tracesToLogsV2": {
                                "customQuery": True,
                                "datasourceUid": "loki",
                                "filterBySpanID": False,
                                "filterByTraceID": True,
                                "query": (
                                    '{service_name="$${__span.tags["service.name"]}"} '
                                    '| trace_id="$${__trace.traceId}"'
                                ),
                                "spanEndTimeShift": "1m",
                                "spanStartTimeShift": "-1m",
                            },
                        },
                        "name": "Tempo",
                        "type": "tempo",
                        "uid": "tempo",
                        "url": "http://tempo.monitoring.svc.cluster.local:3200",
                    },
                ],
                "admin": {
                    "existingSecret": "grafana-administrator",
                    "passwordKey": "admin-password",
                    "userKey": "admin-user",
                },
                "defaultDashboardsEnabled": False,
                "enabled": True,
                "grafana.ini": {
                    "analytics": {"check_for_updates": False, "reporting_enabled": False},
                    "auth.anonymous": {"enabled": False},
                    "server": {
                        "domain": lab.hostnames.grafana,
                        "root_url": f"https://{lab.hostnames.grafana}",
                    },
                },
                "ingress": {"enabled": False},
                "nodeSelector": {"node.longhorn.io/create-default-disk": "true"},
                "persistence": {
                    "accessModes": ["ReadWriteOnce"],
                    "enabled": True,
                    "size": lab.observability.grafana_storage_size,
                    "storageClassName": "longhorn",
                    "type": "sts",
                },
                "resources": {
                    "limits": {"cpu": "1", "memory": "1Gi"},
                    "requests": {"cpu": "100m", "memory": "256Mi"},
                },
                "service": {"type": "ClusterIP"},
                "sidecar": {
                    "dashboards": {
                        "enabled": True,
                        "label": "grafana_dashboard",
                        "labelValue": "1",
                        "searchNamespace": lab.observability.namespace,
                    },
                    "datasources": {"enabled": True},
                },
                "testFramework": {"enabled": False},
            },
            "kubeControllerManager": {"enabled": False},
            "kubeEtcd": {"enabled": False},
            "kubeProxy": {"enabled": False},
            "kubeScheduler": {"enabled": False},
            "kubeStateMetrics": {"enabled": True},
            "kube-state-metrics": {
                "resources": {
                    "limits": {"cpu": "500m", "memory": "256Mi"},
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                }
            },
            "nodeExporter": {"enabled": True},
            "prometheus": {
                "enabled": True,
                "prometheusSpec": {
                    "nodeSelector": {"node.longhorn.io/create-default-disk": "true"},
                    "podMonitorSelectorNilUsesHelmValues": False,
                    "replicas": 1,
                    "resources": {
                        "limits": {"cpu": "2", "memory": "3Gi"},
                        "requests": {"cpu": "500m", "memory": "1Gi"},
                    },
                    "retention": lab.observability.prometheus_retention,
                    "retentionSize": lab.observability.prometheus_retention_size,
                    "ruleSelectorNilUsesHelmValues": False,
                    "serviceMonitorSelectorNilUsesHelmValues": False,
                    "storageSpec": {
                        "volumeClaimTemplate": {
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "resources": {
                                    "requests": {
                                        "storage": lab.observability.prometheus_storage_size
                                    }
                                },
                                "storageClassName": "longhorn",
                            }
                        }
                    },
                },
                "service": {"type": "ClusterIP"},
            },
            "prometheus-node-exporter": {
                "resources": {
                    "limits": {"cpu": "250m", "memory": "128Mi"},
                    "requests": {"cpu": "50m", "memory": "32Mi"},
                }
            },
            "prometheusOperator": {
                "resources": {
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                }
            },
            "thanosRuler": {"enabled": False},
            "windowsMonitoring": {"enabled": False},
        },
        "loki": {
            "backend": {"replicas": 0},
            "chunksCache": {"enabled": False},
            "deploymentMode": "Monolithic",
            "gateway": {"enabled": False},
            "loki": {
                "auth_enabled": False,
                "commonConfig": {"path_prefix": "/var/loki", "replication_factor": 1},
                "compactor": {
                    "delete_request_store": "filesystem",
                    "retention_delete_delay": "2h",
                    "retention_delete_worker_count": 20,
                    "retention_enabled": True,
                    "working_directory": "/var/loki/compactor",
                },
                "distributor": {
                    "otlp_config": {
                        "default_resource_attributes_as_index_labels": [
                            "service.name",
                            "service.namespace",
                            "deployment.environment.name",
                            "k8s.cluster.name",
                            "k8s.namespace.name",
                            "k8s.container.name",
                            "k8s.deployment.name",
                            "k8s.statefulset.name",
                            "k8s.daemonset.name",
                            "k8s.job.name",
                        ]
                    }
                },
                "image": {"tag": versions.loki},
                "limits_config": {
                    "allow_structured_metadata": True,
                    "retention_period": lab.observability.loki_retention,
                },
                "schemaConfig": {
                    "configs": [
                        {
                            "from": "2024-04-01",
                            "index": {"period": "24h", "prefix": "index_"},
                            "object_store": "filesystem",
                            "schema": "v13",
                            "store": "tsdb",
                        }
                    ]
                },
                "storage": {"type": "filesystem"},
            },
            "lokiCanary": {"enabled": False},
            "monitoring": {"serviceMonitor": {"enabled": True, "interval": "30s"}},
            "read": {"replicas": 0},
            "resultsCache": {"enabled": False},
            "singleBinary": {
                "nodeSelector": {"node.longhorn.io/create-default-disk": "true"},
                "persistence": {
                    "accessModes": ["ReadWriteOnce"],
                    "enabled": True,
                    "size": lab.observability.loki_storage_size,
                    "storageClass": "longhorn",
                    "whenDeleted": "Retain",
                    "whenScaled": "Retain",
                },
                "podDisruptionBudget": {"enabled": False},
                "replicas": 1,
                "sidecar": False,
                "resources": {
                    "limits": {"cpu": "1", "memory": "1Gi"},
                    "requests": {"cpu": "100m", "memory": "256Mi"},
                },
            },
            "test": {"enabled": False},
            "write": {"replicas": 0},
        },
        "tempo": {
            "affinity": {},
            "config": (
                "stream_over_http_enabled: false\n"
                "memberlist:\n"
                "  cluster_label: tempo.monitoring\n"
                "multitenancy_enabled: false\n"
                "usage_report:\n"
                "  reporting_enabled: false\n"
                "compactor:\n"
                "  compaction:\n"
                f"    block_retention: {lab.observability.tempo_retention}\n"
                "distributor:\n"
                "  receivers:\n"
                "    otlp:\n"
                "      protocols:\n"
                "        grpc:\n"
                "          endpoint: 0.0.0.0:4317\n"
                "        http:\n"
                "          endpoint: 0.0.0.0:4318\n"
                "ingester: {}\n"
                "server:\n"
                "  http_listen_port: 3200\n"
                "storage:\n"
                "  trace:\n"
                "    backend: local\n"
                "    local:\n"
                "      path: /var/tempo/traces\n"
                "    wal:\n"
                "      path: /var/tempo/wal\n"
                "querier: {}\n"
                "query_frontend: {}\n"
                "overrides:\n"
                "  defaults: {}\n"
            ),
            "nodeSelector": {"node.longhorn.io/create-default-disk": "true"},
            "persistence": {
                "accessModes": ["ReadWriteOnce"],
                "enabled": True,
                "enableStatefulSetAutoDeletePVC": False,
                "size": lab.observability.tempo_storage_size,
                "storageClassName": "longhorn",
            },
            "replicas": 1,
            "resources": {
                "limits": {"cpu": "1", "memory": "1Gi"},
                "requests": {"cpu": "100m", "memory": "256Mi"},
            },
            "service": {"type": "ClusterIP"},
            "serviceMonitor": {"enabled": True, "interval": "30s"},
            "tempo": {
                "metricsGenerator": {"enabled": False},
                "receivers": {
                    "otlp": {
                        "protocols": {
                            "grpc": {"endpoint": "0.0.0.0:4317"},
                            "http": {"endpoint": "0.0.0.0:4318"},
                        }
                    }
                },
                "reportingEnabled": False,
                "retention": lab.observability.tempo_retention,
                "tag": versions.tempo,
            },
            "tempoQuery": {"enabled": False},
        },
    }

    files = [
        GeneratedFile(
            generated / "cluster" / "coredns.yaml",
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "coredns", "namespace": "kube-system"},
                    "data": {
                        "Corefile": (
                            f"{lab.lab.domain}:53 {{\n"
                            "    errors\n"
                            f"    forward . {lab.network.api_virtual_ip}\n"
                            "    cache 30\n"
                            "    reload\n"
                            "}\n"
                            ".:53 {\n"
                            "    errors\n"
                            "    health {\n"
                            "        lameduck 5s\n"
                            "    }\n"
                            "    ready\n"
                            "    kubernetes cluster.local in-addr.arpa ip6.arpa {\n"
                            "        pods insecure\n"
                            "        fallthrough in-addr.arpa ip6.arpa\n"
                            "        ttl 30\n"
                            "    }\n"
                            "    prometheus :9153\n"
                            "    forward . /etc/resolv.conf {\n"
                            "        max_concurrent 1000\n"
                            "    }\n"
                            "    cache 30 {\n"
                            "        disable success cluster.local\n"
                            "        disable denial cluster.local\n"
                            "    }\n"
                            "    loop\n"
                            "    reload\n"
                            "    loadbalance\n"
                            "}\n"
                        )
                    },
                },
                sort_keys=True,
            ),
        ),
        GeneratedFile(
            generated / "terraform" / "lab.auto.tfvars.json",
            json.dumps(terraform_vars, indent=2, sort_keys=True, default=_json_default) + "\n",
        ),
        GeneratedFile(
            generated / "ansible" / "inventory.yaml",
            yaml.safe_dump(_to_plain(groups), sort_keys=True),
        ),
        GeneratedFile(
            generated / "ansible" / "group_vars" / "all" / "generated.yaml",
            yaml.safe_dump(_to_plain(generated_vars), sort_keys=True),
        ),
        GeneratedFile(generated / "platform" / "platform.env", "\n".join(env_lines) + "\n"),
        GeneratedFile(generated / "make" / "generated.mk", "\n".join(make_lines) + "\n"),
    ]
    for component, values in helm_components.items():
        files.append(
            GeneratedFile(
                generated / "helm" / f"{component}.values.yaml",
                yaml.safe_dump(values, sort_keys=True),
            )
        )
    return files


def write_generated_files(repo_root: Path) -> list[Path]:
    lab, versions = load_config(repo_root)
    files = render_files(lab, versions, repo_root)
    for item in files:
        item.path.parent.mkdir(parents=True, exist_ok=True)
        item.path.write_text(item.content, encoding="utf-8")
    (repo_root / ".generated" / "pki").mkdir(parents=True, exist_ok=True)
    (repo_root / ".generated" / "secrets").mkdir(parents=True, exist_ok=True)
    return [item.path for item in files]
