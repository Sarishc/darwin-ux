"""HTTP error responses.

FastAPI's default 422 response echoes each invalid value back (`input`). For
telemetry that is harmful: a rejected payload may contain personal data, a
huge value makes the error as large as the request, and a pathologically
nested value can crash while being re-serialised (HTTP 500). This handler
returns the same error list without the echoed values.
"""

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


async def validation_error_without_input(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        {key: value for key, value in error.items() if key != "input"} for error in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": jsonable_encoder(errors)},
    )
