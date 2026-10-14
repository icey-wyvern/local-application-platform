from __future__ import annotations

from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class VmRole(StrEnum):
    LOAD_BALANCER = "load_balancer"
    CONTROL_PLANE = "control_plane"
    WORKER = "worker"
    BUILD = "build"


class Lab(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    domain: str
    application_domain: str
    timezone: str
    physical_host_operating_system: str
    host_virtualisation: str
    administration_environment: str
    guest_operating_system: str


class Network(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vm_cidr: IPv4Network
    vm_gateway: IPv4Address
    bootstrap_dns_servers: Annotated[list[IPv4Address], Field(min_length=1)]
    api_virtual_ip: IPv4Address
    pod_cidr: IPv4Network
    pod_node_cidr_mask: Annotated[int, Field(ge=16, le=30)]
    service_cidr: IPv4Network
    metallb_pool_start: IPv4Address
    metallb_pool_end: IPv4Address
    envoy_gateway_ip: IPv4Address

    @field_validator("vm_cidr", "pod_cidr", "service_cidr", mode="before")
    @classmethod
    def parse_networks(cls, value: object) -> IPv4Network:
        parsed = ip_network(value)  # type: ignore[arg-type]
        if not isinstance(parsed, IPv4Network):
            raise ValueError("IPv6 networks are not supported for the lab configuration")
        return parsed

    @field_validator(
        "vm_gateway",
        "api_virtual_ip",
        "metallb_pool_start",
        "metallb_pool_end",
        "envoy_gateway_ip",
        mode="before",
    )
    @classmethod
    def parse_addresses(cls, value: object) -> IPv4Address:
        parsed = ip_address(value)  # type: ignore[arg-type]
        if not isinstance(parsed, IPv4Address):
            raise ValueError("IPv6 addresses are not supported for the VM network")
        return parsed

    @model_validator(mode="after")
    def validate_ranges(self) -> Network:
        for name in ("vm_gateway", "api_virtual_ip", "metallb_pool_start", "metallb_pool_end"):
            address = getattr(self, name)
            if address not in self.vm_cidr:
                raise ValueError(f"{name} must be inside vm_cidr")
        if self.metallb_pool_start > self.metallb_pool_end:
            raise ValueError("metallb_pool_start must not be greater than metallb_pool_end")
        if (
            self.envoy_gateway_ip < self.metallb_pool_start
            or self.envoy_gateway_ip > self.metallb_pool_end
        ):
            raise ValueError("envoy_gateway_ip must be inside the MetalLB pool")
        if self.pod_cidr.overlaps(self.service_cidr):
            raise ValueError("pod_cidr and service_cidr must not overlap")
        return self


class Hostnames(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform_api: str
    registry: str
    build_agent: str
    grafana: str


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    implementation: str
    host: str
    port: Annotated[int, Field(ge=1, le=65535)]
    image_namespace: str
    data_path: str
    service_user: str
    publisher_identity: str
    pull_identity: str


class BuildAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    port: Annotated[int, Field(ge=1, le=65535)]
    build_timeout_minutes: Annotated[int, Field(ge=1, le=60)]
    service_user: str
    state_path: str


class BuildHost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    buildkit_user: str
    buildkit_state_path: str
    buildkit_socket: str
    trivy_cache_path: str
    artifact_path: str


class PostgreSQL(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: str
    service_name: str
    database: str
    application_user: str
    storage_class: str
    storage_size: str


class Observability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: str
    prometheus_retention: str
    prometheus_retention_size: str
    prometheus_storage_size: str
    grafana_storage_size: str
    alertmanager_storage_size: str
    loki_retention: str
    loki_storage_size: str
    tempo_retention: str
    tempo_storage_size: str


class ApplicationDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replicas: Annotated[int, Field(ge=1)]
    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str
    deployment_deadline_seconds: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def validate_contract_constants(self) -> ApplicationDefaults:
        if self.replicas != 2:
            raise ValueError("application replicas must be fixed at 2 for version one")
        return self


class Operations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_seconds: Annotated[int, Field(ge=1)]
    lease_renewal_seconds: Annotated[int, Field(ge=1)]
    max_operations_per_api_pod: Annotated[int, Field(ge=1)]
    max_concurrent_builds: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def validate_contract_constants(self) -> Operations:
        if self.lease_seconds != 45:
            raise ValueError("operation lease must be 45 seconds")
        if self.lease_renewal_seconds != 10:
            raise ValueError("operation lease renewal must be 10 seconds")
        if self.max_operations_per_api_pod != 2:
            raise ValueError("each API pod must run at most two operations")
        if self.max_concurrent_builds != 1:
            raise ValueError("build concurrency must be one")
        return self


class HyperV(BaseModel):
    model_config = ConfigDict(extra="forbid")

    switch_name: str
    nat_name: str
    vhd_root_path: str
    seed_iso_root_path: str
    base_image_source: HttpUrl
    base_image_name: str
    base_image_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    base_image_capacity_gib: Annotated[int, Field(ge=60, le=60)]
    ssh_user: str
    ssh_public_key_path: str


class VirtualMachine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    role: VmRole
    address: IPv4Address
    vcpu: Annotated[int, Field(ge=1)]
    memory_mib: Annotated[int, Field(ge=512)]
    disk_gib: Annotated[int, Field(ge=60, le=60)]

    @field_validator("address", mode="before")
    @classmethod
    def parse_address(cls, value: object) -> IPv4Address:
        parsed = ip_address(value)  # type: ignore[arg-type]
        if not isinstance(parsed, IPv4Address):
            raise ValueError("virtual-machine address must be IPv4")
        return parsed


class LabConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lab: Lab
    network: Network
    hostnames: Hostnames
    registry: Registry
    build_agent: BuildAgent
    build_host: BuildHost
    postgresql: PostgreSQL
    observability: Observability
    application_defaults: ApplicationDefaults
    operations: Operations
    hyperv: HyperV
    virtual_machines: list[VirtualMachine]

    @model_validator(mode="after")
    def validate_contract_topology(self) -> LabConfig:
        names = {vm.name for vm in self.virtual_machines}
        addresses = {vm.address for vm in self.virtual_machines}
        if len(names) != len(self.virtual_machines):
            raise ValueError("virtual-machine names must be unique")
        if len(addresses) != len(self.virtual_machines):
            raise ValueError("virtual-machine addresses must be unique")
        if len(self.virtual_machines) != 9:
            raise ValueError("the full lab must contain exactly nine virtual machines")
        expected_counts = {
            VmRole.LOAD_BALANCER: 2,
            VmRole.CONTROL_PLANE: 3,
            VmRole.WORKER: 3,
            VmRole.BUILD: 1,
        }
        for role, expected in expected_counts.items():
            actual = sum(1 for vm in self.virtual_machines if vm.role == role)
            if actual != expected:
                raise ValueError(f"expected {expected} {role.value} machines, found {actual}")
        for vm in self.virtual_machines:
            if vm.address not in self.network.vm_cidr:
                raise ValueError(f"{vm.name} address must be inside vm_cidr")
        return self


class Versions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kubernetes: str
    containerd: str
    runc: str
    cni_plugins: str
    cilium: str
    metallb: str
    envoy_gateway: str
    gateway_api: str
    cert_manager: str
    longhorn: str
    postgresql: str
    registry: str
    buildkit: str
    skopeo: str
    trivy: str
    kube_prometheus_stack_chart: str
    loki: str
    loki_chart: str
    tempo: str
    tempo_chart: str
    opentelemetry_collector_contrib: str
    terraform: str
    terraform_provider_hyperv: str
    ansible_core: str
    helm: str
    kustomize: str
    python: str
    go: str
    kind: str
