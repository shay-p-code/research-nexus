from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Question(Strict):
    question: str = Field(min_length=5, max_length=3000)


class Start(Strict):
    inspection_id: str
    focus: str = Field(min_length=5, max_length=3000, description="Unanswered questions and why more research is needed")
    max_rounds: int = Field(default=3, ge=1, le=5)
    refresh: bool = False


class Lease(Strict):
    lease_token: str = Field(min_length=32, max_length=128)


class LibraryNote(Strict):
    text: str = Field(min_length=10, max_length=800)
    kind: Literal["finding", "uncertainty", "contradiction"]
    confidence: Literal["low", "medium", "high"]
    source_urls: list[str] = Field(min_length=1, max_length=8)
    existing_note_id: str | None
    contradicts: list[str] = Field(max_length=5)


class LibraryResult(Strict):
    summary: str = Field(max_length=1600)
    notes: list[LibraryNote] = Field(max_length=12)
    gaps: list[str] = Field(max_length=5)
    sufficient: bool


class AnswerPart(Strict):
    text: str = Field(max_length=1500)
    note_ids: list[str] = Field(min_length=1, max_length=10)


class AnswerResult(Strict):
    parts: list[AnswerPart] = Field(max_length=8)
    gaps: list[str] = Field(max_length=5)
