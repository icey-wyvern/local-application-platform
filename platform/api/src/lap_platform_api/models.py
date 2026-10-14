from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from lap_platform_api.states import (
    BuildStatus,
    DeploymentStatus,
    DeploymentType,
    OperationStatus,
    OperationType,
)


def _values(values: Iterable[StrEnum]) -> str:
    return ", ".join(f"'{value.value}'" for value in values)


class Base(DeclarativeBase):
    pass


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        CheckConstraint(
            "name ~ '^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$'",
            name="ck_applications_name_format",
        ),
        UniqueConstraint("name", name="uq_applications_name"),
        UniqueConstraint(
            "normalized_repository_url", name="uq_applications_normalized_repository_url"
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    repository_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    normalized_repository_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    current_release_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "releases.id",
            name="fk_applications_current_release_id_releases",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    current_release: Mapped[Release | None] = relationship(
        foreign_keys=[current_release_id], post_update=True
    )
    releases: Mapped[list[Release]] = relationship(
        back_populates="application", foreign_keys="Release.application_id"
    )
    builds: Mapped[list[Build]] = relationship(back_populates="application")
    deployments: Mapped[list[Deployment]] = relationship(
        back_populates="application", foreign_keys="Deployment.application_id"
    )
    operations: Mapped[list[Operation]] = relationship(
        back_populates="application", foreign_keys="Operation.application_id"
    )


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (
        CheckConstraint(f"type IN ({_values(OperationType)})", name="ck_operations_type"),
        CheckConstraint(f"status IN ({_values(OperationStatus)})", name="ck_operations_status"),
        CheckConstraint("attempts >= 0", name="ck_operations_attempts_nonnegative"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_operations_request_hash"),
        CheckConstraint(
            "substring(idempotency_key::text, 15, 1) = '4'",
            name="ck_operations_idempotency_key_v4",
        ),
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
            name="ck_operations_lease_pair",
        ),
        CheckConstraint(
            "(status = 'running') = (lease_owner IS NOT NULL)",
            name="ck_operations_running_lease",
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (completed_at IS NOT NULL)",
            name="ck_operations_terminal_completion",
        ),
        UniqueConstraint("idempotency_key", name="uq_operations_idempotency_key"),
        Index(
            "ix_operations_claim_queued",
            "created_at",
            "id",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "ix_operations_claim_expired",
            "lease_expires_at",
            "created_at",
            "id",
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "uq_operations_active_application",
            "application_id",
            unique=True,
            postgresql_where=text("application_id IS NOT NULL AND status IN ('queued', 'running')"),
        ),
        Index(
            "uq_operations_active_deploy_repository",
            "normalized_repository_url",
            unique=True,
            postgresql_where=text(
                "type = 'deploy' AND normalized_repository_url IS NOT NULL "
                "AND status IN ('queued', 'running')"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=OperationStatus.QUEUED.value,
        server_default=OperationStatus.QUEUED.value,
    )
    idempotency_key: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, default=uuid4
    )
    repository_url: Mapped[str | None] = mapped_column(String(2048))
    normalized_repository_url: Mapped[str | None] = mapped_column(String(2048))
    git_revision: Mapped[str | None] = mapped_column(String(255))
    application_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="RESTRICT"),
    )
    build_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "builds.id",
            name="fk_operations_build_id_builds",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        unique=True,
    )
    deployment_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "deployments.id",
            name="fk_operations_deployment_id_deployments",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(Text)
    failure_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    application: Mapped[Application | None] = relationship(
        back_populates="operations", foreign_keys=[application_id]
    )
    build: Mapped[Build | None] = relationship(foreign_keys=[build_id], post_update=True)
    deployment: Mapped[Deployment | None] = relationship(
        foreign_keys=[deployment_id], post_update=True
    )
    builds: Mapped[list[Build]] = relationship(
        back_populates="operation", foreign_keys="Build.operation_id"
    )
    deployments: Mapped[list[Deployment]] = relationship(
        back_populates="operation", foreign_keys="Deployment.operation_id"
    )


class Build(Base):
    __tablename__ = "builds"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(BuildStatus)})", name="ck_builds_status"),
        CheckConstraint(
            "resolved_commit IS NULL OR resolved_commit ~ '^[0-9a-f]{40}$'",
            name="ck_builds_resolved_commit",
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'failed', 'rejected')) = (completed_at IS NOT NULL)",
            name="ck_builds_terminal_completion",
        ),
        UniqueConstraint("operation_id", name="uq_builds_operation_id"),
        Index("ix_builds_status_created_at", "status", "created_at", "id"),
        Index("ix_builds_application_created_at", "application_id", "created_at"),
        Index("ix_builds_resolved_commit", "resolved_commit"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    operation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operations.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("applications.id", ondelete="RESTRICT")
    )
    repository_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    normalized_repository_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    requested_revision: Mapped[str] = mapped_column(String(255), nullable=False)
    resolved_commit: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=BuildStatus.PENDING.value,
        server_default=BuildStatus.PENDING.value,
    )
    vulnerability_counts: Mapped[dict[str, int] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(Text)

    operation: Mapped[Operation] = relationship(
        back_populates="builds", foreign_keys=[operation_id]
    )
    application: Mapped[Application | None] = relationship(back_populates="builds")
    release: Mapped[Release | None] = relationship(back_populates="build", uselist=False)


class Release(Base):
    __tablename__ = "releases"
    __table_args__ = (
        CheckConstraint("git_commit ~ '^[0-9a-f]{40}$'", name="ck_releases_git_commit"),
        CheckConstraint("image_digest ~ '^sha256:[0-9a-f]{64}$'", name="ck_releases_image_digest"),
        CheckConstraint(
            "image_reference ~ '@sha256:[0-9a-f]{64}$'",
            name="ck_releases_image_reference_digest",
        ),
        CheckConstraint(
            "right(image_reference, 71) = image_digest",
            name="ck_releases_reference_matches_digest",
        ),
        CheckConstraint("port BETWEEN 1024 AND 65535", name="ck_releases_port"),
        CheckConstraint(
            "health_path LIKE '/%' AND health_path NOT LIKE '//%'",
            name="ck_releases_health_path",
        ),
        UniqueConstraint("build_id", name="uq_releases_build_id"),
        UniqueConstraint(
            "application_id", "image_digest", name="uq_releases_application_image_digest"
        ),
        Index("ix_releases_application_created_at", "application_id", "created_at"),
        Index("ix_releases_git_commit", "git_commit"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="RESTRICT"),
        nullable=False,
    )
    build_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("builds.id", ondelete="RESTRICT"), nullable=False
    )
    git_commit: Mapped[str] = mapped_column(String(40), nullable=False)
    image_reference: Mapped[str] = mapped_column(String(2048), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    health_path: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    application: Mapped[Application] = relationship(
        back_populates="releases", foreign_keys=[application_id]
    )
    build: Mapped[Build] = relationship(back_populates="release")


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint(f"type IN ({_values(DeploymentType)})", name="ck_deployments_type"),
        CheckConstraint(f"status IN ({_values(DeploymentStatus)})", name="ck_deployments_status"),
        CheckConstraint(
            "(status IN ('healthy', 'failed')) = (completed_at IS NOT NULL)",
            name="ck_deployments_terminal_completion",
        ),
        CheckConstraint(
            "previous_release_id IS NULL OR previous_release_id <> desired_release_id",
            name="ck_deployments_distinct_releases",
        ),
        CheckConstraint(
            "(type = 'automatic_rollback') = (rollback_of_deployment_id IS NOT NULL)",
            name="ck_deployments_automatic_rollback_link",
        ),
        Index(
            "uq_deployments_active_application",
            "application_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'applying', 'reconciling', 'verifying')"),
        ),
        Index("ix_deployments_application_created_at", "application_id", "created_at"),
        Index("ix_deployments_status", "status"),
        Index("ix_deployments_desired_release_id", "desired_release_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    operation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operations.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False
    )
    desired_release_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("releases.id", ondelete="RESTRICT"), nullable=False
    )
    previous_release_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("releases.id", ondelete="RESTRICT")
    )
    rollback_of_deployment_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deployments.id", ondelete="RESTRICT"), unique=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DeploymentStatus.PENDING.value,
        server_default=DeploymentStatus.PENDING.value,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(Text)

    operation: Mapped[Operation] = relationship(
        back_populates="deployments", foreign_keys=[operation_id]
    )
    application: Mapped[Application] = relationship(
        back_populates="deployments", foreign_keys=[application_id]
    )
    desired_release: Mapped[Release] = relationship(foreign_keys=[desired_release_id])
    previous_release: Mapped[Release | None] = relationship(foreign_keys=[previous_release_id])
    rollback_of: Mapped[Deployment | None] = relationship(
        remote_side=[id], foreign_keys=[rollback_of_deployment_id], back_populates="rollback"
    )
    rollback: Mapped[Deployment | None] = relationship(
        foreign_keys=[rollback_of_deployment_id], back_populates="rollback_of", uselist=False
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_created_at", "created_at"),
        Index("ix_audit_events_correlation_id", "correlation_id"),
        Index("ix_audit_events_application_id", "application_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    application_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("applications.id", ondelete="RESTRICT")
    )
    deployment_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deployments.id", ondelete="RESTRICT")
    )
    source_address: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
