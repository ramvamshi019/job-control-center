"""
services/match_report.py
------------------------
A deterministic, Jobscan-style match report between a job description and YOUR
résumé. No external API, no Jobscan dependency — Jobscan's "match rate" is, at
its core, hard-skill keyword coverage of the JD by the résumé, and that is fully
reproducible here.

analyze(jd_title, jd_text, resume_text) ->
  {
    "score":   53,                       # % of the JD's hard skills your résumé covers (+ title match)
    "matched": ["python", "sql", ...],   # skills the JD wants AND your résumé has  (✅ have)
    "missing": ["airflow", "dbt", ...],  # skills the JD wants that your résumé lacks (❌ missing)
    "jd_skills": [...],                  # every hard skill detected in the JD
    "title_match": True,                 # the role word (e.g. "data engineer") is in your résumé
    "counts": {"jd": 19, "have": 9, "missing": 10},
  }

It is honest by construction: it only *reports* the gap. It never edits the
résumé — that's resume_builder's job, and even there only truthful rephrasing is
allowed. Use this to (1) rank which jobs to spend effort on, and (2) tell the
tailoring step exactly which keywords to try to surface from real experience.
"""

from __future__ import annotations

import re
from typing import List

# --- Hard-skill gazetteer ----------------------------------------------------
# Canonical lowercase terms. Multi-word entries are matched whole, longest-first,
# so "spark sql" wins over "spark". Kept broad across data / backend / cloud /
# ML / general SWE so it works whatever role the JD targets. This is the
# vocabulary used to detect skills in BOTH the JD and the résumé.
_GAZETTEER = {
    # languages & scripting
    "python", "sql", "pl/sql", "t-sql", "scala", "java", "c++", "c#", "go",
    "golang", "rust", "javascript", "typescript", "r", "bash", "shell scripting",
    "shell", "powershell", "perl", "matlab", "kotlin", "swift", "php", "ruby",
    # big data / distributed
    "apache spark", "spark", "pyspark", "spark sql", "spark streaming",
    "structured streaming", "hadoop", "hive", "hdfs", "mapreduce", "presto",
    "trino", "flink", "beam", "dask", "ray",
    # aws
    "aws", "amazon web services", "s3", "emr", "glue", "lambda", "redshift",
    "athena", "kinesis", "step functions", "cloudwatch", "ec2", "iam", "sqs",
    "sns", "dynamodb", "rds", "sagemaker", "lake formation",
    # azure
    "azure", "azure data factory", "adf", "azure databricks", "synapse",
    "synapse analytics", "adls", "blob storage", "azure functions",
    "azure devops", "azure monitor",
    # gcp
    "gcp", "google cloud", "google cloud platform", "bigquery", "dataflow",
    "dataproc", "pub/sub", "cloud composer", "vertex ai", "cloud functions",
    "looker",
    # warehouse / lakehouse
    "snowflake", "databricks", "delta lake", "dimensional modeling",
    "star schema", "snowflake schema", "scd", "data vault", "data warehouse",
    "data warehousing", "data lake", "lakehouse", "olap", "kimball",
    # orchestration / etl
    "airflow", "apache airflow", "dbt", "dagster", "prefect", "luigi",
    "control-m", "informatica", "talend", "ssis", "fivetran", "stitch",
    "etl", "elt", "data pipelines", "data pipeline", "ingestion",
    "source-to-target mapping", "data ingestion",
    # streaming / messaging
    "kafka", "apache kafka", "kinesis", "event hubs", "pulsar", "rabbitmq",
    "kafka connect", "schema registry",
    # databases
    "postgresql", "postgres", "mysql", "sql server", "oracle", "mongodb",
    "cassandra", "redis", "elasticsearch", "neo4j", "cockroachdb", "pgvector",
    # bi / analytics
    "power bi", "tableau", "looker", "qlik", "mode", "superset", "metabase",
    "excel", "data visualization", "dashboards", "reporting",
    # formats
    "json", "xml", "parquet", "avro", "orc", "csv", "protobuf",
    # devops / infra
    "git", "github", "gitlab", "bitbucket", "jenkins", "docker", "kubernetes",
    "k8s", "terraform", "ansible", "ci/cd", "linux", "unix", "helm",
    "github actions", "argocd", "prometheus", "grafana",
    # data engineering concepts
    "data modeling", "data quality", "data governance", "data lineage",
    "metadata management", "master data management", "data catalog",
    "feature engineering", "data mesh", "data contracts",
    # ml / analytics support
    "pandas", "numpy", "jupyter", "scikit-learn", "tensorflow", "pytorch",
    "machine learning", "statistical analysis", "mlflow", "feature store",
    # general swe / backend (JDs vary — cover these so SWE roles score fairly)
    "rest", "rest api", "graphql", "grpc", "microservices",
    "microservices architecture", "distributed systems", "fastapi", "flask",
    "django", "spring", "spring boot", "node.js", "react", "api design",
    "system design", "object-oriented", "oop", "software development lifecycle",
    "sdlc", "agile", "scrum", "kanban", "tdd", "unit testing", "code review",
    "celery", "caddy", "nginx", "next.js",
}

# Variant -> canonical, so "postgres"/"postgresql", "k8s"/"kubernetes", etc. all
# collapse to one skill (no double-counting, cleaner have/missing lists).
_ALIASES = {
    "amazon web services": "aws",
    "google cloud": "gcp",
    "google cloud platform": "gcp",
    "postgres": "postgresql",
    "k8s": "kubernetes",
    "golang": "go",
    "apache spark": "spark",
    "apache kafka": "kafka",
    "apache airflow": "airflow",
    "synapse analytics": "synapse",
    "azure databricks": "databricks",
    "azure data factory": "adf",
    "shell": "shell scripting",
    "unix": "linux",
    "oop": "object-oriented",
    "sdlc": "software development lifecycle",
    "rest api": "rest",
    "data pipeline": "data pipelines",
    "data ingestion": "ingestion",
    "microservices architecture": "microservices",
    "data warehousing": "data warehouse",
}

# Role words for the title-match bonus (Jobscan rewards having the job title).
_TITLE_ROLES = [
    "data engineer", "analytics engineer", "etl developer", "software engineer",
    "backend engineer", "back-end engineer", "platform engineer",
    "data platform engineer", "machine learning engineer", "ml engineer",
    "data analyst", "cloud engineer", "devops engineer",
]


def _canon(term: str) -> str:
    return _ALIASES.get(term, term)


# One combined word-boundary regex over the whole gazetteer, longest term first
# so multi-word skills win. Single pass per document instead of N searches.
_ALL_TERMS = sorted(_GAZETTEER, key=len, reverse=True)
_COMBINED = re.compile(
    r"(?<![a-z0-9+#./-])(" + "|".join(re.escape(t) for t in _ALL_TERMS) + r")(?![a-z0-9+#])"
)


def _find_skills(text: str) -> set[str]:
    """Canonical hard skills present in a block of text."""
    blob = " " + (text or "").lower() + " "
    return {_canon(m) for m in _COMBINED.findall(blob)}


def extract_skills(text: str) -> set[str]:
    """Public: the set of canonical hard skills named in `text`. Used by the
    résumé builder's anti-fabrication guard — any skill in a generated résumé that
    is NOT in this set for the base résumé is an invented skill and gets rejected."""
    return _find_skills(text)


def _title_in_resume(jd_title: str, resume_text: str) -> bool:
    rt = (resume_text or "").lower()
    jt = (jd_title or "").lower()
    for role in _TITLE_ROLES:
        if role in jt and role in rt:
            return True
    return False


def analyze(jd_title: str, jd_text: str, resume_text: str) -> dict:
    """Compute the skill-coverage match report (see module docstring)."""
    jd_skills = _find_skills((jd_title or "") + ". " + (jd_text or ""))
    resume_skills = _find_skills(resume_text)

    matched = sorted(jd_skills & resume_skills)
    missing = sorted(jd_skills - resume_skills)
    title_match = _title_in_resume(jd_title, resume_text)

    n_jd = len(jd_skills)
    coverage = (len(matched) / n_jd) if n_jd else 0.0
    # Skill coverage dominates (like Jobscan's hard-skill weighting); the title
    # match is a small bump. Clamp to a sane 0-100 integer.
    score = round(100 * (0.85 * coverage + (0.15 if title_match else 0.0)))
    score = max(0, min(100, score))

    return {
        "score": score,
        "matched": matched,
        "missing": missing,
        "jd_skills": sorted(jd_skills),
        "title_match": title_match,
        "counts": {"jd": n_jd, "have": len(matched), "missing": len(missing)},
    }


def missing_for_prompt(report: dict, limit: int = 20) -> List[str]:
    """The JD keywords the résumé is missing — handed to the tailoring step so it
    can try to surface any that the candidate's REAL experience supports."""
    return list(report.get("missing", []))[:limit]
