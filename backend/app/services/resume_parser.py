"""
services/resume_parser.py
-------------------------
Parse an uploaded résumé (PDF / DOCX / TXT / MD) into a structured profile:
skills, estimated years of experience, experience level, target roles, and
whether the candidate needs visa sponsorship. Powers the "Match My Résumé" page.

Fully offline / rule-based — no API needed.
"""
from __future__ import annotations

import io
import re
from typing import Any, Dict, List

from app.utils.text import normalize, term_in

# Broad tech-skill vocabulary so this works for ANY résumé, not just the seeded
# profile. Matched with word boundaries (term_in), so "r" / "go" / "c" etc. are
# intentionally omitted to avoid false positives.
SKILL_VOCAB: List[str] = [
    # languages
    "python", "java", "scala", "javascript", "typescript", "c++", "c#", "golang",
    "ruby", "rust", "kotlin", "swift", "php", "bash", "shell", "matlab",
    # sql / db
    "sql", "pl/sql", "t-sql", "postgresql", "postgres", "mysql", "sql server",
    "oracle", "mongodb", "dynamodb", "cassandra", "redis", "elasticsearch",
    "snowflake", "bigquery", "redshift", "synapse",
    # big data / pipelines
    "spark", "pyspark", "spark sql", "spark streaming", "hadoop", "hive", "hdfs",
    "kafka", "event hubs", "flink", "airflow", "dbt", "databricks", "delta lake",
    "etl", "elt", "data modeling", "dimensional modeling", "star schema", "scd",
    "data quality", "data governance", "data lineage", "data warehouse", "data lake",
    # cloud
    "aws", "azure", "gcp", "google cloud", "s3", "emr", "glue", "lambda", "athena",
    "kinesis", "step functions", "cloudwatch", "redshift", "ec2", "azure data factory",
    "adf", "adls", "synapse", "snowflake", "bigquery", "dataflow", "pub/sub",
    # devops / tools
    "docker", "kubernetes", "terraform", "jenkins", "git", "github", "gitlab",
    "azure devops", "ci/cd", "linux", "ansible", "airflow",
    # bi / analytics
    "power bi", "tableau", "looker", "qlik", "pandas", "numpy", "jupyter",
    "scikit-learn", "tensorflow", "pytorch", "machine learning", "deep learning",
    # formats
    "parquet", "avro", "json", "orc", "delta lake",
]

ROLE_VOCAB: List[str] = [
    "data engineer", "analytics engineer", "etl developer", "big data engineer",
    "data platform engineer", "cloud engineer", "software engineer", "backend engineer",
    "data analyst", "machine learning engineer", "platform engineer", "devops engineer",
]

JUNIOR_SIGNALS = ["intern", "internship", "new grad", "recent graduate", "entry level",
                  "entry-level", "junior", "associate"]
SPONSOR_SIGNALS = ["opt", "cpt", "f-1", "f1", "h-1b", "h1b", "stem opt",
                   "require sponsorship", "need sponsorship", "visa sponsorship"]


def extract_text(data: bytes, filename: str) -> str:
    """Pull plain text from an uploaded résumé file."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    if name.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in d.paragraphs)
    # txt / md / unknown -> best-effort decode
    return data.decode("utf-8", errors="ignore")


def _estimate_years(text: str) -> int:
    """Heuristic years of experience: the largest 'N years' figure, else infer
    from the widest 4-digit year range mentioned (capped at 15)."""
    nums = [int(n) for n in re.findall(r"(\d{1,2})\+?\s*years?", text)]
    if nums:
        return min(max(nums), 15)
    years = [int(y) for y in re.findall(r"\b(19[89]\d|20[0-3]\d)\b", text)]
    if len(years) >= 2:
        span = max(years) - min(years)
        return min(max(span, 0), 15)
    return 0


def _level(years: int, has_junior: bool) -> str:
    if has_junior or years <= 2:
        return "entry"
    if years <= 5:
        return "mid"
    return "senior"


def parse_resume(text: str) -> Dict[str, Any]:
    norm = normalize(text)
    skills = sorted({s for s in SKILL_VOCAB if term_in(norm, s)})
    roles = sorted({r for r in ROLE_VOCAB if r in norm})
    years = _estimate_years(norm)
    has_junior = any(j in norm for j in JUNIOR_SIGNALS)
    needs_sponsorship = any(s in norm for s in SPONSOR_SIGNALS)
    return {
        "skills": skills,
        "roles": roles,
        "years": years,
        "level": _level(years, has_junior),
        "needs_sponsorship": needs_sponsorship,
        "chars": len(text),
    }
