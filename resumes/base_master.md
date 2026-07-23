# Ram Vamshi Krishna Rudraram — Data Engineer

**Title:** Data Engineer | Analytics Engineer | ETL Developer
**Location:** MO, United States
**Phone:** 417-419-8066 · **Email:** rrudraram4@gmail.com · **LinkedIn:** linkedin.com/in/ramvamshikrishna · **GitHub:** github.com/ramvamshi019

## Professional Summary
Data Engineer with 2 years building production ETL/ELT pipelines on AWS across 5+ TB of regulated healthcare data, plus an MS in Computer Science. Currently builds and operates a job-market data platform tracking 444,000+ postings ingested from roughly 22,900 sources. Depth in Python, PySpark, SQL, Airflow, and Redshift, with hands-on distributed processing, data quality, and pipeline orchestration.

## Technical Skills
- **Languages:** Python, SQL, PL/SQL, TypeScript, JavaScript, Bash
- **Data Engineering:** Apache Spark, PySpark, Spark SQL & Streaming, Apache Airflow, dbt, Kafka, ETL/ELT, Dimensional Modeling, Star Schema, SCD Type 1/2, Data Quality, Data Governance, Schema Normalization
- **Cloud:** AWS (S3, EMR, Glue, Redshift, Lambda, Athena, Kinesis, Step Functions); Azure (Data Factory, Databricks, Synapse, ADLS Gen2); DigitalOcean
- **Data Stores:** Amazon Redshift, PostgreSQL (pgvector), MySQL, SQL Server, SQLite, Delta Lake, Parquet, Avro
- **Engineering & AI:** Docker, Docker Compose, CI/CD, Git, Linux, Caddy, REST APIs, FastAPI, SQLModel, Celery, Redis, Streamlit, pytest, TDD, Agile/Scrum; LLM Integration (OpenAI, Anthropic), RAG, Vector Embeddings, Pandas, NumPy, Power BI, Tableau

## Professional Experience
### Data Engineer — Johnson & Johnson · Feb 2022 – Dec 2023
*Hyderabad, India · Healthcare / Life Sciences*
- Processed 5+ TB of clinical-trial and patient healthcare data by building batch and streaming ETL/ELT pipelines in Python, PySpark, and SQL on AWS (EMR, Glue, S3, Redshift) with Kafka and Kinesis ingestion of HL7 and FHIR records, delivering analytics-ready datasets to research teams.
- Cut query execution time on multi-billion-row tables by 50% by redesigning Redshift physical layout (distribution keys, sort keys, column compression) across star-schema data marts serving clinical reporting, regulatory compliance, and 100+ analysts.
- Reduced pipeline job failures by 35% by developing and maintaining 25+ Apache Airflow DAGs with retry, alerting, and SLA monitoring, improving orchestration reliability and observability.
- Accelerated new pipeline development by 30% team-wide by designing reusable, modular Python libraries for ingestion, transformation, logging, and validation, enforced through TDD and peer code review.

## Projects
### Job Control Center — Job-market data platform, deployed and running 24/7 · 2026
*Python, FastAPI, SQLModel, SQLite (WAL), Streamlit, Docker Compose, Caddy, DigitalOcean*
- Built a continuously running ingestion platform that crawls roughly 22,900 company career pages through 29 source-specific modules (Greenhouse, Lever, Ashby, Workday, iCIMS, SmartRecruiters, BambooHR, Paylocity, UKG, Oracle HCM, and job boards), normalizing heterogeneous payloads into a single schema across 444,000+ tracked postings.
- Diagnosed and fixed a source-fidelity defect in which a vendor's last-modified timestamp was ingested as the publish date, causing a 778-day-old posting to surface as new; enforced true publish-date semantics at the source layer and added explicit handling for the 43% of records whose providers emit no publish date.
- Designed a tiered retention and staleness model (90-day retention for high-value employers versus 10-day for others, with expiry detected from last-seen crawl timestamps rather than posting age) after short uniform windows were found to be dropping entire viable employers.
- Implemented a profile-matching scorer ranking every posting on role fit and employer signals, with a calibrated score threshold that suppresses high-volume duplicate-listing spam, plus a re-evaluation job to rescore stored rows when filter logic changes.
- Deployed a three-service stack (API, crawler, dashboard) via Docker Compose on a 2 vCPU / 2 GB droplet behind Caddy with HTTPS, applying CPU and memory limits to isolate the crawler from the API and tuning SQLite WAL access patterns around its single-writer constraint.

### JobJarvis — Full-stack LLM job-search platform · github.com/ramvamshi019/jobjarvis · 2026
*FastAPI, Next.js, TypeScript, PostgreSQL/pgvector, Celery, Redis, dbt, Airflow, Docker*
- Built a full-stack LLM job-search platform: async FastAPI backend (SQLAlchemy 2.0, Alembic) with Celery workers over Postgres and Redis, pgvector semantic matching against candidate profiles, dbt models, Airflow DAGs, and a Chrome extension, deployed via Docker Compose behind Caddy.

### E-Commerce Support Classification · github.com/ramvamshi019/Ecommerce-ml-project · 2025
*PySpark, Python, Machine Learning, Distributed Computing (graduate coursework)*
- Built an ML pipeline classifying e-commerce support cases and benchmarked distributed PySpark against single-node execution, quantifying runtime and scaling behavior across data volumes.

## Education
- **Master of Science in Computer Science** — Missouri State University, Springfield, MO · Jan 2024 – Dec 2025
- **Bachelor of Technology in Computer Science** — Koneru Lakshmaiah Education Foundation, India · May 2023
