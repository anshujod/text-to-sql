"""Typed models for the semantic layer (metrics/entities/joins/defaults)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Metric(BaseModel):
    sql_expression: str
    description: str
    synonyms: list[str] = Field(default_factory=list)
    default_filters: list[str] = Field(default_factory=list)
    grain: str
    requires_join: list[str] = Field(default_factory=list)


class ForeignKey(BaseModel):
    column: str
    references_table: str
    references_column: str


class Entity(BaseModel):
    description: str
    grain: str
    primary_key: str
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    soft_delete_column: str | None = None
    columns: dict[str, str]
    enum_values: dict[str, list[str]] = Field(default_factory=dict)


class JoinEdge(BaseModel):
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    on: str
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]


class ResultShapeDefaults(BaseModel):
    default_limit: int


class ScopeDefaults(BaseModel):
    exclude_internal_accounts: bool
    exclude_soft_deleted: bool


class MetricDefaults(BaseModel):
    revenue_default: str


class TemporalDefaults(BaseModel):
    anchor: Literal["max_order_created_at", "wall_clock_now"]
    last_month: Literal["previous_calendar_month", "trailing_30_days"]
    last_month_definition: str


class Defaults(BaseModel):
    result_shape: ResultShapeDefaults
    scope: ScopeDefaults
    metric: MetricDefaults
    temporal: TemporalDefaults
    always_disclose_defaults: bool = True


class SemanticLayer(BaseModel):
    metrics: dict[str, Metric]
    entities: dict[str, Entity]
    joins: list[JoinEdge]
    defaults: Defaults

    @model_validator(mode="after")
    def _check_internal_consistency(self) -> "SemanticLayer":
        errors: list[str] = []
        table_names = set(self.entities)

        if self.defaults.metric.revenue_default not in self.metrics:
            errors.append(
                f"defaults.metric.revenue_default={self.defaults.metric.revenue_default!r} "
                "is not a defined metric"
            )

        for metric_name, metric in self.metrics.items():
            for table in metric.requires_join:
                if table not in table_names:
                    errors.append(
                        f"metric {metric_name!r}: requires_join references unknown table {table!r}"
                    )

        for entity_name, entity in self.entities.items():
            if entity.primary_key not in entity.columns:
                errors.append(
                    f"entity {entity_name!r}: primary_key {entity.primary_key!r} not in columns"
                )
            if entity.soft_delete_column and entity.soft_delete_column not in entity.columns:
                errors.append(
                    f"entity {entity_name!r}: soft_delete_column "
                    f"{entity.soft_delete_column!r} not in columns"
                )
            for fk in entity.foreign_keys:
                if fk.column not in entity.columns:
                    errors.append(
                        f"entity {entity_name!r}: foreign key column {fk.column!r} not in columns"
                    )
                if fk.references_table not in table_names:
                    errors.append(
                        f"entity {entity_name!r}: foreign key references unknown table "
                        f"{fk.references_table!r}"
                    )
            for enum_col in entity.enum_values:
                if enum_col not in entity.columns:
                    errors.append(
                        f"entity {entity_name!r}: enum_values column {enum_col!r} not in columns"
                    )

        for edge in self.joins:
            if edge.from_table not in table_names:
                errors.append(f"join edge references unknown table {edge.from_table!r}")
            if edge.to_table not in table_names:
                errors.append(f"join edge references unknown table {edge.to_table!r}")

        if errors:
            raise ValueError(
                "Semantic layer is internally inconsistent:\n" + "\n".join(f"  - {e}" for e in errors)
            )
        return self
