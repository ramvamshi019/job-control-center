# Models package. Import order matters for SQLModel metadata registration.
from app.models.company import Company  # noqa: F401
from app.models.job import Job  # noqa: F401
from app.models.application import Application  # noqa: F401
