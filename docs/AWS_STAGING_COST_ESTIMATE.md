# AWS staging cost estimate — 2026-11-07

The saved staging configuration in Ireland (`eu-west-1`) has a **$135.32/month core hosting cost** and a **$167.39/month planning example** with the usage allowances below. Prices are in USD, before taxes and credits.

**Deployment deferred:** on 2026-11-07, the owner reported no funds available and a current hosting budget of **$0**. This estimate is preserved for future planning. No paid provisioning should proceed under that constraint; region, hostname and DNS-provider choices can wait until funding is available. Continue with the [local development path](AWS_STAGING.md#local-work-while-paid-hosting-is-deferred).

The estimate uses 730 hours/month, one x86-64 Linux web task at 0.5 vCPU / 1 GB, Single-AZ PostgreSQL `db.t4g.small` with 20 GB gp3 storage, one Redis OSS 7.1 `cache.t4g.small` node, one NAT gateway, and an ALB across two availability zones. Public IPv4 uses a minimum of three addresses: two for the ALB and one for NAT. The quantities for traffic, logs, metrics, images and backups are planning inputs, rather than measured usage.

Rates were retrieved from the AWS Price List Query API on 2026-11-07. The query endpoint is in `us-east-1`; the products being priced are in `eu-west-1`. Redis backup storage uses the [official ElastiCache pricing page](https://aws.amazon.com/elasticache/pricing/). The [JSON snapshot](AWS_STAGING_COST_ESTIMATE.json) records product SKUs, first-tier rates, filters and arithmetic.

| Component | Monthly input and unit rate | Monthly USD |
|---|---|---:|
| Fargate web task | 365 vCPU-hours at $0.04048 + 730 GB-hours at $0.004445 | $18.02 |
| PostgreSQL db.t4g.small | 730 hours at $0.035 | $25.55 |
| PostgreSQL gp3 storage | 20 GB-months at $0.127 | $2.54 |
| Redis cache.t4g.small | 730 hours at $0.034 | $24.82 |
| NAT gateway | 730 hours at $0.048 | $35.04 |
| Application Load Balancer | 730 hours at $0.0252 | $18.40 |
| Public IPv4 | 3 addresses x 730 hours at $0.005 | $10.95 |
| ALB capacity | 1 average LCU x 730 hours at $0.008 | $5.84 |
| Container Insights and logs | 50 metric-months at $0.30; 5 GB ingested at $0.57; 5 GB-months retained at $0.03 | $18.00 |
| Additional RDS backups | 40 billable GB-months at $0.095 | $3.80 |
| Redis backups | 10 GiB-months at $0.085 | $0.85 |
| NAT processing | 10 GB at $0.048 | $0.48 |
| Internet transfer | 10 billable GB above included allowance at $0.09 | $0.90 |
| Regional transfer | 20 billable GB units at $0.01 | $0.20 |
| ECR image storage | 5 GB-months at $0.10 | $0.50 |
| Secrets Manager | 2 secrets at $0.40; 10,000 API requests at $0.000005 | $0.85 |
| Optional Route 53 DNS zone | 1 zone at $0.50 | $0.50 |
| Release and migration overlap | 4 extra web task hours and 4 small bootstrap/migration task hours | $0.15 |
| **Planning total** | **Sum of rounded line items** | **$167.39** |

The first seven rows make up the $135.32 core hosting subtotal. The remaining rows add $32.07 of operations and usage allowances. Removing only the web task leaves about $117.30/month of those core services running; `WebDesiredCount=0` does not stop database, cache, NAT, ALB or address charges.

The monitoring allowance covers 50 standard Container Insights metric-months and the stated log volume. The actual metric count depends on emitted cluster, service and task-family dimensions; observe it after deployment and update this model. Redis 7.1 is outside the versions listed in the currently published [Redis OSS Extended Support schedule](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/engine-versions.html). Reprice if the engine version or support status changes.

The RDS backup and internet-transfer inputs count billable usage beyond their included allocations. Redis backup storage counts the stated retained volume. Route 53 is optional if an existing DNS provider manages the staging record; ALB alias queries do not need a standard DNS-query allowance. Three IPv4 addresses are a minimum for this ALB/NAT layout; scaling can add addresses.

The example excludes SMS/email provider charges, domain registration, RDS surplus CPU credits, storage growth above 20 GB, additional tasks/replicas, custom monitoring dashboards/queries/alarms, and paid support. Public ACM certificates used by the ALB and basic ECR image scanning have no separate line in this model. No reservation or Savings Plan commitment is assumed. Budget alerts notify rather than impose a hard spending cap.

Read-only AWS checks on 2026-11-07 accepted both CloudFormation templates and reported PostgreSQL 16.15 as available in Ireland. No non-deleted `ihweza-staging*` CloudFormation stacks were found there. These checks did not provision infrastructure or establish a hosted application.

When funds become available and the owner supplies a region and budget, revise the quantities if needed and record the final AWS Pricing Calculator estimate link and total in [AWS_STAGING.md](AWS_STAGING.md). The hostname/DNS choice is then needed for the certificate and actual deployment parameters.
