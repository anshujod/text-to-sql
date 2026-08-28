from enum import Enum

from pydantic import BaseModel, Field


class ValidationErrorType(str, Enum):
    PARSE_ERROR = "parse_error"
    MULTIPLE_STATEMENTS = "multiple_statements"
    NOT_A_SELECT = "not_a_select"
    FORBIDDEN_STATEMENT = "forbidden_statement"
    CATALOG_ACCESS = "catalog_access"
    UNKNOWN_TABLE = "unknown_table"
    UNKNOWN_COLUMN = "unknown_column"
    WIDE_SELECT_STAR = "wide_select_star"
    CARTESIAN_PRODUCT = "cartesian_product"


class ValidationError(BaseModel):
    type: ValidationErrorType
    message: str


class ValidationResult(BaseModel):
    ok: bool
    errors: list[ValidationError] = Field(default_factory=list)
    rewritten_sql: str | None = None
