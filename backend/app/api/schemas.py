from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class SignupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=200)


class AssistantIn(BaseModel):
    job_id: str | None = Field(default=None, max_length=40)
    message: str = Field(min_length=1, max_length=8000)
    template: str | None = Field(default=None, max_length=8000)


class TemplateIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class ApprovalIn(BaseModel):
    action: Literal["approve", "changes", "reject"]
    channels: list[str] = Field(default_factory=list, max_length=5)
    edited_post: str | None = Field(default=None, max_length=8000)
    feedback: str | None = Field(default=None, max_length=1000)


class WeightsIn(BaseModel):
    weights: dict[str, float] = Field(min_length=1, max_length=10)


class InterviewIn(BaseModel):
    job_id: str = Field(min_length=1, max_length=40)
    candidate_id: str = Field(min_length=1, max_length=40)
    starts_at: datetime


class JoinIn(BaseModel):
    consent: bool


class ClientLogIn(BaseModel):
    entries: list[dict] = Field(max_length=200)
