"""
services/cluster.py
-------------------
Assign every job to ONE of 7 role clusters. The cluster is used to:
  1. Pick the right MASTER-RESUME variant when auto-tailoring (Data Eng
     vs ML/AI vs Backend etc. — the same person, but the emphasized
     projects/skills differ).
  2. Group Best Matches by cluster in the UI so Ram can knock out one
     bucket at a time (context-switching across clusters is the hidden
     cost of "just apply to more").
  3. Feed the ranker prompt with cluster-specific context so the AI
     ranker doesn't judge a Data Eng resume against an ML-heavy JD.

Clusters (mutually exclusive; the FIRST match wins so order matters):
  ml_ai         — ML / AI / applied science / research eng / MLOps
  data_eng      — pipelines, warehouse, spark, airflow, dbt, streaming
  bi_analytics  — BI/analytics/reporting/tableau/looker/analytics eng
  cloud_devops  — DevOps / SRE / platform / infra / K8s
  security      — security / infosec / appsec / SOC
  backend       — backend/API/services/distributed (SWE default when a
                  posting isn't UI-heavy)
  fullstack     — anything explicitly full-stack, frontend, or web
  other         — didn't clearly fit (rare — kept so a job always has one)

Classifier is REGEX-ONLY: fast, deterministic, no LLM cost. Runs on
title + first ~800 chars of description; the tail is boilerplate.
"""

from __future__ import annotations

import re
from typing import Optional

# Each entry: (cluster, list-of-regex-patterns). First cluster with a match wins.
# Multi-word matches use \b to avoid substring false-positives ("sre" in "stress").
_RULES: list[tuple[str, list[re.Pattern]]] = [
    (
        "ml_ai",
        [re.compile(p, re.I) for p in [
            r"\b(machine learning|ml engineer|ml infra|mlops|ml platform)\b",
            r"\b(applied scientist|research (?:engineer|scientist))\b",
            r"\b(deep learning|nlp|computer vision|cv engineer)\b",
            r"\b(ai engineer|ai infra|ai platform|foundation model|llm engineer)\b",
            r"\b(gen ?ai|generative ai)\b",
            r"\b(recommender|reinforcement learning|prompt engineer)\b",
        ]],
    ),
    (
        "data_eng",
        [re.compile(p, re.I) for p in [
            r"\bdata (?:engineer|engineering|platform|infra|infrastructure)\b",
            r"\b(etl|elt) (?:engineer|developer)\b",
            r"\b(analytics engineer)\b",  # borderline, but tools overlap heavily w/ data_eng
            r"\bdata (?:pipeline|warehouse|lake|lakehouse)\b",
            r"\b(spark|airflow|dbt|kafka|flink|snowflake|databricks) (?:engineer|developer)\b",
            r"\b(pipeline|streaming) engineer\b",
        ]],
    ),
    (
        "bi_analytics",
        [re.compile(p, re.I) for p in [
            r"\b(business intelligence|bi (?:engineer|developer|analyst))\b",
            r"\b(data analyst|analytics (?:manager|analyst|lead))\b",
            r"\b(tableau|looker|power ?bi) (?:developer|engineer|analyst)\b",
            r"\breporting (?:analyst|engineer)\b",
            r"\bproduct analyst\b",
            r"\bfinancial analyst\b",
        ]],
    ),
    (
        "cloud_devops",
        [re.compile(p, re.I) for p in [
            r"\b(devops|sre|site reliability|platform engineer|infrastructure engineer)\b",
            r"\b(cloud engineer|cloud (?:infra|infrastructure|platform))\b",
            r"\b(kubernetes|k8s|terraform) engineer\b",
            r"\bproduction engineer\b",
            r"\b(release|deployment) engineer\b",
            r"\bobservability\b",
        ]],
    ),
    (
        "security",
        [re.compile(p, re.I) for p in [
            r"\b(security engineer|appsec|application security|infosec|information security)\b",
            r"\bcloud security\b",
            r"\bsecurity (?:analyst|architect|consultant|research)\b",
            r"\bsoc (?:analyst|engineer)\b",
            r"\b(threat|vulnerability) (?:analyst|research|engineer)\b",
            r"\bpenetration test|pentest|red team|blue team\b",
        ]],
    ),
    (
        "fullstack",
        [re.compile(p, re.I) for p in [
            r"\bfull ?stack (?:engineer|developer)\b",
            r"\bfrontend (?:engineer|developer)\b",
            r"\bfront[- ]end (?:engineer|developer)\b",
            r"\b(react|vue|angular|next\.?js) (?:engineer|developer)\b",
            r"\bweb (?:engineer|developer)\b",
            r"\bui (?:engineer|developer)\b",
        ]],
    ),
    (
        "backend",
        [re.compile(p, re.I) for p in [
            r"\bbackend (?:engineer|developer)\b",
            r"\bback[- ]end (?:engineer|developer)\b",
            r"\b(api|services|distributed systems) engineer\b",
            r"\bserver[- ]?side\b",
            r"\bmicroservices\b",
            # Bare "software engineer" defaults here — most SWE reqs are backend-shaped
            # once you strip out the UI-heavy ones caught by fullstack above.
            r"\b(software|sw) (?:development )?engineer\b(?! (?:intern|manager|iii?)\b)?",
            r"\bsde\b",
            r"\bsoftware developer\b",
        ]],
    ),
]


def classify(title: str, description: str = "") -> str:
    """Return the cluster slug for one job. Never raises; returns 'other'
    if nothing matches (rare — the backend fallback catches almost everything).
    """
    haystack = f"{title or ''}\n{(description or '')[:800]}"
    for cluster, patterns in _RULES:
        if any(p.search(haystack) for p in patterns):
            return cluster
    return "other"


CLUSTER_LABELS = {
    "ml_ai":        "🤖 ML / AI",
    "data_eng":     "🔬 Data Engineering",
    "bi_analytics": "📊 BI / Analytics",
    "cloud_devops": "☁️ Cloud / DevOps",
    "security":     "🔒 Security",
    "backend":      "⚙️ Backend / API",
    "fullstack":    "🖥️ Full-Stack / UI",
    "other":        "❓ Other",
}


def label(cluster: str) -> str:
    return CLUSTER_LABELS.get(cluster or "other", cluster or "other")
