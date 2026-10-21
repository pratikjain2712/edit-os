# Deploying Edit OS on AWS

Edit OS is two services:

| Service | Image | What it does |
|---|---|---|
| `api` | `Dockerfile` (repo root) | FastAPI + in-process worker threads + the built review screen (`web/dist`) |
| `languagetool` | `deploy/languagetool.Dockerfile` | Self-hosted LanguageTool 6.6 HTTP server (Java), private to the VPC |

Manuscripts are client-confidential: everything runs inside your VPC, LanguageTool is self-hosted,
and the only outside calls are PubMed / CrossRef (reference metadata, no manuscript text beyond the
reference being looked up) and the Anthropic API (when `EDITOS_ANTHROPIC_API_KEY` is set).

## Before anything else: rotate the dashboard's credentials

The existing dashboard repositories (`TE_BACK` / `te_back` and `TE_Frontend` / `te_frontend`) have
credentials committed to git: in te_back, `.env.production`, `service_account.json`,
`oauth_credentials.json`, `token.json` and `cookies.txt` (and possibly values in config files such as
`ce_config.py` and the diagnostic scripts); in te_frontend, `.env` and `.env.production`. Treat every
value in them as compromised:

1. Rotate them all: Google service-account keys and OAuth client secret, the Drive token, AWS access
   keys, database passwords, JWT/session secrets, SMTP/e-mail credentials.
2. Remove the files from the repositories, add them to `.gitignore`, and purge them from history
   (`git filter-repo`); revoking is what actually matters, since clones keep the old history.
3. Move the new values into AWS Secrets Manager and inject them as environment variables, the same way
   as Edit OS below. Prefer IAM task roles over AWS access keys altogether.
4. The frontend's `proxy` points at a bare public IP over plain HTTP; put the backend behind HTTPS (ALB + ACM).

Edit OS never reads, copies or reuses any of those credentials.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `EDITOS_API_KEYS` | yes | Comma-separated API keys accepted in `X-API-Key` (one per caller, e.g. dashboard, admin). Secrets Manager. With none set the API refuses every request. |
| `EDITOS_ANTHROPIC_API_KEY` | recommended | Claude Haiku for reference field splitting and front matter; the copy-edit Claude pass. Without it those steps are skipped (rules still run). Secrets Manager. |
| `NCBI_API_KEY` | optional | Raises the PubMed E-utilities rate limit (3 -> 10 requests/s). Secrets Manager. |
| `EDITOS_CALLBACK_SECRET` | with the dashboard | HMAC key for job-completion callbacks (`X-EditOS-Signature`). Secrets Manager; same value in te_back. |
| `EDITOS_S3_BUCKET` | on AWS | Job inputs/outputs are copied to `s3://$EDITOS_S3_BUCKET/$EDITOS_S3_PREFIX<job>/` (SSE-S3). |
| `EDITOS_S3_PREFIX` | no | Default `edit-os/`. |
| `AWS_REGION` | on AWS | e.g. `ap-south-1`. Credentials come from the task/instance IAM role, never from env keys. |
| `EDITOS_LANGUAGETOOL_URL` | yes | `http://languagetool.editos.local:8081` (Cloud Map) or `http://languagetool:8081` (compose). |
| `EDITOS_PUBLIC_URL` | yes | Public HTTPS base URL of the review screen, used in review links (`https://editos.example.com`). |
| `EDITOS_DATA_DIR` | no | Default `/data` in the image: SQLite job DB, working folders, caches. |
| `EDITOS_WORKERS` | no | Pipeline threads per API task (default 2). |
| `EDITOS_MAX_UPLOAD_MB` | no | Default 50. |
| `EDITOS_STAGES` | no | Default `style,copyedit`. |

## Quick test deployment: one command

    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=ap-south-1 \
    EDITOS_ANTHROPIC_API_KEY=... .venv/bin/python tools/deploy_aws.py

Creates (or reuses) a private S3 bucket, the `edit-os/env` secret (a generated API key, the Claude key, the
bucket), an instance role with SSM, a security group for HTTP and a `t3.medium` instance (4 GB, 20 GB disk,
2 GB swap) that builds and runs both containers (`deploy/ec2-deploy.sh`). It prints the URL and the API key.
Run it again to deploy the current `HEAD`; `--status` shows the instance. To scale up, rerun with e.g.
`--instance-type t3.large --disk-gb 30`: the same instance is stopped, resized and started (about a minute
of downtime; data is kept). Disks only grow, so start small. Plain HTTP on the instance's public IP is for testing only:
use real manuscripts only behind HTTPS (ALB + ACM, option A below).

## Option A (start here): one EC2 instance with docker compose

Good for a few thousand manuscripts a month and the simplest to operate.

1. VPC with a private subnet for the instance and public subnets for an Application Load Balancer.
2. EC2 `t3.large` (2 vCPU, 8 GB; LanguageTool wants ~2-3 GB) with a 30 GB gp3 volume, Amazon Linux 2023,
   Docker + compose plugin, SSM Session Manager instead of SSH.
3. Instance role: `s3:GetObject/PutObject/ListBucket` on the Edit OS bucket (and `s3:GetObject` on the
   dashboard bucket's article prefix if jobs are created from S3 keys), `secretsmanager:GetSecretValue`
   on the Edit OS secrets, CloudWatch Logs.
4. A small boot script writes `.env` from Secrets Manager (`aws secretsmanager get-secret-value ...`)
   and runs `docker compose up -d`. Images built in CI and pushed to ECR (`docker compose pull`).
5. ALB (HTTPS, ACM certificate) -> instance port 8000; health check `/api/health`. Optionally restrict the
   ALB security group to the office / VPN ranges.
6. Back up `/data/jobs.sqlite` nightly to S3 (job files are in S3 already when `EDITOS_S3_BUCKET` is set).

## Option B: ECS Fargate

- Cluster with two services in private subnets:
  - `editos-api`: 1 vCPU / 2 GB, desired count 1 (SQLite and in-process workers mean one task; see
    "Scaling" for more). Secrets from Secrets Manager through the task definition's `secrets` block;
    task role for S3. Behind an ALB (HTTPS, `/api/health`).
  - `editos-languagetool`: 1 vCPU / 3 GB (`JAVA_OPTS=-Xmx2g`), no load balancer, registered in Cloud Map
    as `languagetool.editos.local`; security group allows 8081 from the API's security group only.
- `/data` on EFS (one task only, because of SQLite) or ephemeral storage plus `EDITOS_S3_BUCKET` (job
  files restore from S3 on demand; the SQLite DB then needs EFS or a move to RDS).
- ECR repositories for both images; CloudWatch Logs; deployments from CI.

## Scaling later

The code is already split for it: the API only calls `queue.submit(job_id)`, and workers call
`server.worker.process(job_id)`.

1. Job state: move `server/db.py`'s two tables to RDS PostgreSQL (db.t4g.micro is enough).
2. Queue: replace `ThreadQueue` with an SQS-backed queue (`submit` = `SendMessage`); run worker tasks
   that receive messages and call `process` (scale on queue depth).
3. Files: set `EDITOS_S3_BUCKET` so any task can restore any job.
4. API tasks become stateless; run 2+ behind the ALB.

## S3 bucket

- Bucket `editink-editos-<env>`, Block Public Access on, default encryption SSE-S3 (or SSE-KMS),
  versioning on, lifecycle rule to expire `edit-os/` objects after e.g. 180 days (confidential data
  should not live forever), access logs to a separate bucket.
- Bucket policy: only the Edit OS role. For jobs created from the dashboard's S3 key, add a
  statement on the dashboard bucket allowing the Edit OS role `s3:GetObject` on the article prefix.

## Secrets Manager

One secret `editos/<env>` (JSON) holding `EDITOS_API_KEYS`, `EDITOS_ANTHROPIC_API_KEY`,
`NCBI_API_KEY`, `EDITOS_CALLBACK_SECRET`. ECS injects each key (`valueFrom: arn...:EDITOS_API_KEYS::`);
on EC2 the boot script renders `.env` with mode 600. Rotate API keys by adding the new key to the
comma-separated list, switching callers, then removing the old one.

## Monthly cost estimate (ap-south-1, on-demand, list prices as of 2026; check the AWS calculator)

Assumes ~1,000 manuscripts/month, ~5 MB in and ~5 MB out per manuscript.

| Item | Option A (EC2) | Option B (Fargate) |
|---|---|---|
| Compute | t3.large 24x7 ~ $65 | API 1 vCPU/2 GB ~ $40 + LanguageTool 1 vCPU/3 GB ~ $45 |
| Storage | gp3 30 GB ~ $3; EFS n/a | EFS a few GB ~ $1-3 |
| Application Load Balancer | ~ $20 | ~ $20 |
| S3 (10 GB/month, requests) | < $1 | < $1 |
| Secrets Manager (1-4 secrets) | $0.40-1.60 | $0.40-1.60 |
| CloudWatch Logs / metrics | ~ $3-5 | ~ $3-5 |
| NAT gateway (private subnets need outbound for PubMed/Anthropic) | ~ $35 + data | ~ $35 + data |
| **Infrastructure total** | **~ $125-130** | **~ $145-150** |

A NAT gateway is the largest fixed cost; a public-subnet instance with a locked-down security group, or a
NAT instance, brings Option A under ~$95.

Anthropic API usage is on top and scales with volume: Claude Haiku for reference splitting and front
matter is cents per manuscript (results are cached); the Claude copy-edit pass dominates and depends on
the model chosen and manuscript length (budget roughly $0.10-1.00 per manuscript; measure on real
volume). PubMed / CrossRef are free (`NCBI_API_KEY` is free too).

## Checklist

- [ ] Dashboard credentials rotated and removed from git (see top)
- [ ] Secrets in Secrets Manager; no keys in images, task definitions in plain text, or git
- [ ] HTTPS only (ALB + ACM); LanguageTool reachable only from the API
- [ ] S3 bucket private, encrypted, lifecycle set
- [ ] `EDITOS_PUBLIC_URL` set so review links work; `EDITOS_CALLBACK_SECRET` shared with te_back
- [ ] Nightly SQLite backup (Option A) or RDS (scaling)
