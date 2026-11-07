# AWS staging — configuration and restart runbook

AWS was selected for staging preparation on 2026-11-06. Infrastructure is saved as
CloudFormation templates rather than console-only configuration. These files do
not establish that a hosted environment exists.

## Current state and missing inputs

The existing production container passed CI and actual Docker/runtime checks in
[PR #5](https://github.com/maihwe/matthy.app/pull/5), merged into `develop` at
`c018875064b2b8d715027e2419ce3e97b73485bf`. The AWS preparation builds on that work.

On 2026-11-07, AWS CLI 2.39.3 and the `matthy-staging` operator profile were
authenticated and the account was verified against the saved staging account ID.
The temporary login configuration/cache live outside the repository; recheck
access when resuming. Ireland (`eu-west-1`) is provisional for validation/pricing.
No non-deleted `ihweza-staging*` CloudFormation stacks were found there. This work
has not provisioned paid infrastructure or performed a hosted account/group
walkthrough.

A limited read-only check on 2026-11-07 also found no VPC with the project Name
tag, RDS instance, Redis replication group, ECS cluster or ECR repository named
`ihweza-staging` in Ireland. This checks the prepared project's identifiers in
one region; it does not establish the entire account's billing state.

The dated [cost estimate](AWS_STAGING_COST_ESTIMATE.md) records $135.32/month of
core hosting and a $167.39/month planning example with operations/usage allowances,
before tax and credits. On 2026-11-07, the owner reported no funds available: the
current hosting budget is **$0** and paid AWS provisioning is **deferred**. Keep
the configuration and estimate for later. Region, hostname and DNS-provider
choices can wait until funding is available; the estimate is not a spending cap.

Local verification passed CloudFormation schema/condition checks (`cfn-lint
1.60.0`), both workflow checks (`actionlint`), repository lint and types (156 source
files), 20 AWS regression cases and 16 existing loopback-probe cases. The database
case used a disposable PostgreSQL database and verified migration privileges,
denied role administration and refusal to replace an existing role/password.
Read-only AWS checks on 2026-11-07 accepted both CloudFormation templates and
reported PostgreSQL 16.15 as available in Ireland. Provisioning, real ALB routing,
Secrets Manager injection, ECR scanning and the hosted walkthrough remain to be
verified after deployment.

Inputs to revisit when funding is available:

1. Keep operator access current through AWS CLI/SSO. Account ID and operator
   access were verified on 2026-11-07; the account ID is recorded in GitHub's
   `staging` environment. An account ID is not a credential. Enter one-time
   authorization codes directly in the AWS terminal prompt, and use AWS's
   authentication flow for passwords and verification.
2. AWS region and a monthly staging budget, with an itemized regional estimate.
3. A staging hostname under a domain the owner controls, DNS access and an issued
   ACM certificate in the same region as the load balancer.
4. Real SMS/email providers and adapters for the complete account/group walkthrough.
   A healthy backend can be deployed first; phone-code delivery remains unavailable
   until the adapter is implemented and configured. No capture backend is enabled.

## Local work while paid hosting is deferred

Continue the foundation walkthrough locally with synthetic accounts. PostgreSQL,
Redis and Mailhog run on the development machine, and captured verification
messages need no paid SMS/email provider. Use the existing
[authentication walkthrough](AUTHENTICATION.md#register-verify-log-in-locally)
and [group guide](GROUPS.md); a local result does not verify hosted deployment.

Use a private, ignored `.env` based on [.env.example](../.env.example), with
development settings and independent Django, PostgreSQL, RSA JWT and Fernet
keys. Keep these local keys separate from any future staging secrets. The checked
workspace has a Python virtualenv, but lacks Docker Compose v2 and access to the
Docker daemon. On 2026-11-07, the current `student` account was refused sudo
access. Docker setup was not completed and this attempt started no local services.
The owner will continue on their own laptop after it returns from repair; its
operating system remains to be confirmed. Enable a working local Docker/Compose
installation on that laptop and prepare its private `.env` before running:

```sh
docker compose build web
docker compose up -d postgres redis mailhog
docker compose run --rm web python manage.py migrate --noinput
docker compose up -d web worker beat
```

Open `http://localhost:8000/api/docs/` for the API and
`http://localhost:8025` for captured verification messages. Preserve existing
database volumes. This path uses local services and creates no AWS resources.

## Proposed topology

| Component | Configuration |
|---|---|
| Network | Dedicated VPC, two public load-balancer subnets, two private app/data subnets; one staging NAT gateway for outbound HTTPS |
| Application | ECS Fargate, x86-64, 0.5 vCPU / 1 GiB per web task, two Gunicorn workers, no public task IP or ECS Exec |
| Database | RDS PostgreSQL 16, default patch 16.15, `db.t4g.small`, encrypted 20 GiB gp3 storage with 100 GiB autoscaling ceiling; forced TLS and 30-day automated backup retention |
| Cache/broker | Node-based ElastiCache Redis OSS 7.1, `cache.t4g.small`, authentication and encryption in transit/at rest, seven-day snapshots, `noeviction` |
| HTTPS | Application Load Balancer, TLS 1.2/1.3 certificate listener, HTTP redirect, explicit hostname routing |
| Secrets | Staging JSON secret in Secrets Manager; task execution role injects selected keys; application runtime role has no AWS permissions |
| Images | Immutable commit-SHA tags in ECR; tasks use the resolved image digest; scan rejects HIGH/CRITICAL findings |
| Logs | CloudWatch container logs retained 30 days; runtime role cannot edit/delete them |

`HighAvailability=true` adds an RDS standby and Redis replica. It does not make
the entire stack redundant: one NAT gateway and one initial web task remain
staging tradeoffs. Production needs multiple web tasks, outbound connectivity
per availability zone, failure/restore rehearsals and the operational controls
in [12_DEPLOYMENT_PLAN.md](12_DEPLOYMENT_PLAN.md).

No background jobs or schedules are currently defined in the backend. This first
AWS stack deploys the web role only. Worker services and an enforced singleton
beat service must be added when delivery/background tasks are introduced, before
calling staging a rehearsal for a production topology that includes them. The
existing image supports both roles.

The ALB checks an internal readiness container on port 8001, which uses the
existing verified loopback probe against Django on port 8000. ALB health checks
send an IP Host header and do not supply Django's HTTPS marker; the internal
probe supplies the configured hostname/marker. It exposes only readiness and
holds no application secrets. Security groups allow 8001 only from the ALB; the
public listener routes requests to 8000. Wildcard Django hosts are not required.

## Cost estimate before provisioning

The [2026-11-07 draft](AWS_STAGING_COST_ESTIMATE.md) prices the saved configuration
in Ireland using AWS's published unit rates; its [JSON snapshot](AWS_STAGING_COST_ESTIMATE.json)
records the rates, inputs and arithmetic. After the owner supplies a region and
budget, revise the inputs as needed and reproduce the final configuration in the
[AWS Pricing Calculator](https://calculator.aws/) at 730 hours/month. Include
every row below and save the dated calculator link and total here.

| Billable item | Estimate input |
|---|---|
| ECS Fargate | 1 running web task at 0.5 vCPU / 1 GiB; temporary migration/deployment overlap; x86 Linux |
| RDS | `db.t4g.small`, PostgreSQL, 20 GiB gp3; Single-AZ by default or Multi-AZ if selected; backups beyond included allowance |
| ElastiCache | One `cache.t4g.small` Redis node; two if redundancy is selected; snapshot storage beyond allowance |
| Networking | One NAT gateway and processing bytes, one ALB and capacity units, public IPv4 charges, cross-AZ and outbound data transfer |
| Operations | ECR image storage/scanning, CloudWatch logs/container insights, Secrets Manager secrets, retained snapshots and DNS hosted zone if used |
| External delivery | SMS/email provider usage and sender/domain requirements, estimated separately |

Budget alerts notify; they do not impose a hard spending cap. This is paid,
persistent infrastructure even with `WebDesiredCount=0`: RDS, Redis, NAT and ALB
continue accruing costs. No subscription or reserved-instance commitment is part
of these templates. A smaller cache class is configurable, but reducing resources
must be justified by measurements rather than claiming capacity in advance.

## Prepare the account and registry

The steps below are saved for a future funded deployment. Do not create billable
resources under the current zero-spend constraint.

Use AWS CLI v2 with an authenticated operator profile in the selected account.
Verify `aws sts get-caller-identity` and explicitly set the intended region for all
commands. Configure billing alerts and confirm the estimate before provisioning.
Templates target ordinary commercial AWS regions; China/GovCloud partitions and
cross-account/custom-KMS-key secret stores are not configured by this preparation.

For this workspace's temporary login, the identity check is:

```sh
env AWS_CONFIG_FILE=/tmp/matthy-aws-auth/config \
    AWS_SHARED_CREDENTIALS_FILE=/tmp/matthy-aws-auth/credentials \
    AWS_LOGIN_CACHE_DIRECTORY=/tmp/matthy-aws-auth/login-cache \
    AWS_PAGER= /tmp/matthy-aws-tools/bin/aws --profile matthy-staging \
    sts get-caller-identity --region eu-west-1
```

Keep the same configuration/cache settings and `--profile matthy-staging` on
subsequent local operator commands. These temporary paths may need rebuilding
after a workspace restart; other operators should use their own configured
profile. The GitHub workflow obtains separate temporary credentials through OIDC.

Before manual dispatch, merge this preparation through a PR into `develop` and
promote it through a release PR into the default branch `main`. GitHub requires
the workflow file on the default branch to register `workflow_dispatch`. Then
dispatch against `develop`, which is the only branch the release job accepts.
Promotion of configuration does not provision AWS or deploy the application.

1. Confirm whether `token.actions.githubusercontent.com` already has an IAM OIDC
   provider. If so, pass its ARN as `ExistingOidcProviderArn`; the template can
   create it only if absent. The default repository is `maihwe/matthy.app`.
2. Review and create the `ihweza-staging-access` stack from
   [bootstrap.yaml](../infra/aws/bootstrap.yaml) with `CAPABILITY_NAMED_IAM`.
   It creates an immutable ECR repository and the GitHub release role. It does
   not grant GitHub CloudFormation writes, Secrets Manager reads or database
   administrator access.
3. Set these **non-secret** GitHub `staging` environment variables from the account
   and stack outputs: `AWS_ACCOUNT_ID`, `AWS_REGION`, `AWS_ROLE_ARN` (the
   `DeploymentRoleArn` output), and `ECR_REPOSITORY_URI` (`ImageRepositoryUri`).
4. Verify `staging` still permits only protected branches. The role trusts only
   `repo:maihwe/matthy.app:environment:staging`, with the STS audience. Keep this
   environment restricted; any workflow admitted to it can request this role.
   The deployment workflow additionally runs only from `develop`.
5. After CI, Container and AWS infrastructure checks pass on the current `develop`
   commit, run the workflow's `publish` action:

   ```sh
   gh workflow run aws-staging.yml --ref develop -f action=publish
   ```

The workflow independently verifies all eight existing CI job names, `Staging
image` and `AWS templates` at its exact SHA before requesting AWS credentials.
The latter checks are supplemental to the current branch rules, but mandatory
for this deployment workflow. OIDC uses temporary credentials; no long-lived
AWS access key is placed in GitHub. Repeat publishing reuses the immutable image
for that SHA and still checks its vulnerability scan. A failed scan blocks release.
Copy the digest URI from the successful workflow summary for the infrastructure
parameters. Changes to infrastructure are reviewed/applied by the operator.

## Prepare stable staging secrets

Generate only when creating a new staging environment; keep the same secret
across image releases. The helper refuses to overwrite an existing file.

```sh
.venv/bin/python scripts/aws_staging_secrets.py create --file .env.aws-staging.json
aws secretsmanager create-secret --name ihweza/staging/application \
  --secret-string file://.env.aws-staging.json --query ARN --output text
```

The ignored local file is created with mode 0600. It contains independent Django,
database, Redis, matching RSA JWT and Fernet keys. Do not print its contents, put
them in command arguments, or upload it as a CI artifact. `REDIS_URL` and the
delivery adapter initially remain blank. Keep an encrypted recoverable copy in
the operator's secret-management process; a disposable workspace is not a backup.

## Create the application infrastructure

The exact application stack name is `ihweza-staging`; resource names and the
release role's permissions use this name. Create only one stack per account/region.

1. Check the selected region offers PostgreSQL 16.15 (or choose another supported
   16.x patch), the chosen database/cache classes and two availability zones.
2. Request and validate an ACM certificate for the staging hostname in that region.
3. Copy [staging.parameters.example.json](../infra/aws/staging.parameters.example.json)
   to `infra/aws/staging.parameters.json` and fill in the secret ARN, certificate
   ARN, real hostname and published image digest. The local parameter file is
   ignored. Keep `WebDesiredCount=0` for initial creation.
4. Validate [staging.yaml](../infra/aws/staging.yaml), review an AWS CloudFormation
   change set with `CAPABILITY_NAMED_IAM`, then apply the reviewed configuration.
   The web service initially has no running tasks so it cannot race migrations.
5. Create the hostname's DNS alias/CNAME to `LoadBalancerHostname` from the stack
   outputs. Route 53 alias configuration can use `LoadBalancerHostedZoneId`.
6. Fill in the Redis endpoint while preserving all existing keys:

   ```sh
   .venv/bin/python scripts/aws_staging_secrets.py configure-redis \
     --file .env.aws-staging.json --redis-host ACTUAL_CACHE_HOSTNAME
   aws secretsmanager put-secret-value --secret-id ACTUAL_APPLICATION_SECRET_ARN \
     --secret-string file://.env.aws-staging.json --query VersionId --output text
   ```

Do not rotate `REDIS_AUTH_TOKEN` or database passwords by regenerating the JSON:
the deployed database/cache configuration would retain the old values. Key/password
rotation needs a coordinated procedure and verification. Customer-managed KMS keys
would also require explicit decrypt grants; these templates use AWS-managed keys.

## Initialize the database role once

RDS generates/stores a separate administrator password. The web/release execution
role cannot read it. A separate operator-only bootstrap task can read that secret
and the application password to create `matthy_staging` with no superuser,
database-creation, role-creation or replication privileges. It grants schema
creation/usage and creates `pg_trgm`; migrations run as the application role.
The bootstrap refuses to overwrite an existing role/password.

Read `DatabaseBootstrapTaskArn`, `ClusterName`, `PrivateSubnetIds` and
`ApplicationSecurityGroupId` from stack outputs. Launch that task as the operator
with Fargate platform 1.4.0 and the private subnets/security group, public IP disabled:

```sh
aws ecs run-task --cluster ihweza-staging --launch-type FARGATE \
  --platform-version 1.4.0 --task-definition ACTUAL_BOOTSTRAP_TASK_ARN --count 1 \
  --network-configuration 'awsvpcConfiguration={subnets=[ACTUAL_SUBNET_A,ACTUAL_SUBNET_B],securityGroups=[ACTUAL_SECURITY_GROUP],assignPublicIp=DISABLED}' \
  --output json > /tmp/ihweza-database-bootstrap-result.json
```

Verify the response has one task and no `failures`; wait for that task to stop and
check `stopCode=EssentialContainerExited` and the `initialize-database` container's
exit code is **0**. A stopped task by itself does not prove initialization succeeded.
The task logs only success/failure status, not passwords or database error queries.
The GitHub deployment role cannot run/pass the administrator bootstrap task/role.
Keep this operator capability out of routine releases.

## First release and subsequent releases

Once DNS, stable secrets and database initialization are complete, run:

```sh
gh workflow run aws-staging.yml --ref develop -f action=release
```

The workflow publishes/reuses and scans the passed revision. The release script:

1. Captures the previous service task definition and desired count.
2. Registers web and migration task definitions using the same immutable digest.
3. Runs exactly one migration task without web health checks/readiness sidecar and
   checks both the task result and container exit code. Failure stops before the
   service changes; timeout/interruption requests the migration task be stopped.
4. Updates the service only after migrations succeed, starting one web task for
   the initial release or preserving the existing live count on later releases.
5. Waits for stability **and** confirms the new task definition completed rollout:
   ECS can report stable after an automatic rollback, so stability alone is insufficient.
6. Verifies readiness JSON and nonempty admin CSS through the actual HTTPS hostname.
   Failure restores the previous task definition/count; migrations are not reversed.
7. Records the image digest, URL, new task and previous task in the GitHub summary.

Release concurrency is serialized, and newer dispatches do not cancel migrations.
Graceful interruptions request rollback when the service changed. A killed runner
or interrupted AWS request can prevent cleanup; inspect running tasks/deployments
and use the saved previous task definition/count for operator recovery.

Before later infrastructure updates, set `ImageUri` to the current released image
and `WebDesiredCount` to the live count. CloudFormation does not automatically learn
the task definition/count changed by ECS release commands. Applying the initial
zero-count/old-image parameters can stop or revert the service.

Run `manage.py check --deploy --fail-level WARNING` against the configured image as
an explicit one-off release command before the walkthrough. The full synthetic
registration/MFA/groups acceptance cases remain in [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md).
Record the actual URL, commit/digest, migration and delivery status, acceptance
results and restore rehearsal after deployment. A CI image test is not that walkthrough.

RDS deletion protection, retained logs/ECR and database/cache snapshots are
deliberate. Deleting a stack does not remove every retained resource or stop all
charges. Review backup restoration and retained resources before teardown.

## Authoritative references

- [GitHub AWS OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
- [ECS secret JSON key injection](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html)
- [ALB health checks](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/target-group-health-checks.html)
- [RDS PostgreSQL versions](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-versions.html)
- [ElastiCache engine versions](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/engine-versions.html)
- [Fargate pricing](https://aws.amazon.com/fargate/pricing/)
