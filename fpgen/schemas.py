"""HTTP request models for submission and validation.

Numeric limits that the generator already enforces are checked by
``check_generation_request`` so the API and the command line stay in step.
This module checks the shape of the JSON and the limits the generator does
not know about (finite numbers, DPI, title length).
"""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GenerateRequest(_Request):
    beds: int = 3
    baths: float = 2
    sqft: float = 1600
    seed: int = 1
    laundry: bool | None = None
    garage: int = 0
    shape: str = "rect"
    garage_side: str | None = None
    title: str | None = None
    dpi: int = 170

    @field_validator("beds", "garage", "seed", "dpi")
    @classmethod
    def integers(cls, value: int, info) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{info.field_name} must be an integer")
        return value

    @field_validator("baths", "sqft")
    @classmethod
    def finite_numbers(cls, value: float, info) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"{info.field_name} must be a finite number")
        return float(value)

    @field_validator("dpi")
    @classmethod
    def dpi_range(cls, value: int) -> int:
        if value < 72 or value > 300:
            raise ValueError("dpi must be between 72 and 300")
        return value

    @field_validator("title")
    @classmethod
    def title_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("title must be a non-empty string when provided")
        if len(value) > 200:
            raise ValueError("title must be 200 characters or fewer")
        return value


class RenderRequest(_Request):
    layout: dict[str, Any]
    title: str | None = None
    dpi: int = 170

    @field_validator("dpi")
    @classmethod
    def dpi_range(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("dpi must be an integer")
        if value < 72 or value > 300:
            raise ValueError("dpi must be between 72 and 300")
        return value

    @field_validator("title")
    @classmethod
    def title_text(cls, value: str | None) -> str | None:
        return GenerateRequest.title_text(value)

    @model_validator(mode="after")
    def finite_layout(self) -> "RenderRequest":
        _reject_nonfinite(self.layout, "layout")
        return self


class ValidateRequest(_Request):
    layout: dict[str, Any]

    @model_validator(mode="after")
    def finite_layout(self) -> "ValidateRequest":
        _reject_nonfinite(self.layout, "layout")
        return self


def _reject_nonfinite(value: object, where: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{where} must be a finite number")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{where}[{index}]")
