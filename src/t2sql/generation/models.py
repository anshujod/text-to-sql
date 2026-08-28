from pydantic import BaseModel, Field


class GeneratedSQL(BaseModel):
    sql: str
    tables_used: list[str]
    assumptions: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
