#!/usr/bin/env bash
# Run only after exact-commit CI, image scan and staging infrastructure preparation.
set -euo pipefail

staging_image="${1:?Supply the immutable ECR image URI with sha256 digest}"
if [[ ! "$staging_image" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/ihweza-staging@sha256:[a-f0-9]{64}$ ]]; then
    echo 'Expected an immutable staging ECR image digest.' >&2
    exit 1
fi
staging_tmp="$(mktemp -d)"
staging_migration_task=''
staging_migration_stopped=false
staging_service_updated=false
staging_verified=false
cleanup() {
    if [[ "$staging_service_updated" == true && "$staging_verified" != true ]]; then
        echo 'Release interrupted or failed; restoring the previous service revision/count.' >&2
        aws ecs update-service --cluster "$staging_cluster" --service "$staging_service" \
            --task-definition "$staging_previous" --desired-count "$staging_previous_count" >/dev/null || true
    fi
    if [[ -n "$staging_migration_task" && "$staging_migration_stopped" != true ]]; then
        aws ecs stop-task --cluster "$staging_cluster" --task "$staging_migration_task" \
            --reason 'Release runner exited before confirming migration completion' >/dev/null || true
    fi
    rm -rf "$staging_tmp"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

aws cloudformation describe-stacks --stack-name ihweza-staging \
    --query 'Stacks[0].Outputs' --output json > "$staging_tmp/outputs.json"
jq 'map({key: .OutputKey, value: .OutputValue}) | from_entries' \
    "$staging_tmp/outputs.json" > "$staging_tmp/settings.json"
staging_cluster="$(jq -er '.ClusterName' "$staging_tmp/settings.json")"
staging_service="$(jq -er '.ServiceName' "$staging_tmp/settings.json")"
staging_subnets="$(jq -er '.PrivateSubnetIds' "$staging_tmp/settings.json")"
staging_group="$(jq -er '.ApplicationSecurityGroupId' "$staging_tmp/settings.json")"
staging_url="$(jq -er '.StagingUrl' "$staging_tmp/settings.json")"
aws ecs describe-services --cluster "$staging_cluster" --services "$staging_service" \
    --output json > "$staging_tmp/service.json"
jq -e '(.failures | length) == 0 and (.services | length) == 1 and .services[0].status == "ACTIVE"' \
    "$staging_tmp/service.json" >/dev/null
staging_previous="$(jq -er '.services[0].taskDefinition' "$staging_tmp/service.json")"
staging_previous_count="$(jq -er '.services[0].desiredCount' "$staging_tmp/service.json")"
aws ecs describe-task-definition --task-definition "$staging_previous" \
    --query taskDefinition --output json > "$staging_tmp/previous.json"
jq --arg image "$staging_image" '
    {family, taskRoleArn, executionRoleArn, networkMode, containerDefinitions,
     volumes, placementConstraints, requiresCompatibilities, cpu, memory, runtimePlatform}
    | with_entries(select(.value != null))
    | .containerDefinitions |= map(.image = $image)
' "$staging_tmp/previous.json" > "$staging_tmp/task.json"
aws ecs register-task-definition --cli-input-json "file://$staging_tmp/task.json" \
    --query taskDefinition.taskDefinitionArn --output text > "$staging_tmp/new-task.txt"
staging_new="$(cat "$staging_tmp/new-task.txt")"

# The one-off release task contains only web; it runs no HTTP readiness sidecar.
jq '.containerDefinitions |= map(select(.name == "web") | del(.healthCheck, .portMappings, .dependsOn))
    | .containerDefinitions[0].command = ["python", "manage.py", "migrate", "--noinput"]' \
    "$staging_tmp/task.json" > "$staging_tmp/migration.json"
aws ecs register-task-definition --cli-input-json "file://$staging_tmp/migration.json" \
    --query taskDefinition.taskDefinitionArn --output text > "$staging_tmp/migration-definition.txt"
staging_migration_definition="$(cat "$staging_tmp/migration-definition.txt")"
aws ecs run-task --cluster "$staging_cluster" --launch-type FARGATE --platform-version 1.4.0 \
    --task-definition "$staging_migration_definition" --count 1 \
    --network-configuration "awsvpcConfiguration={subnets=[$staging_subnets],securityGroups=[$staging_group],assignPublicIp=DISABLED}" \
    --output json > "$staging_tmp/migration-start.json"
jq -e '(.failures | length) == 0 and (.tasks | length) == 1' \
    "$staging_tmp/migration-start.json" >/dev/null
staging_migration_task="$(jq -er '.tasks[0].taskArn' "$staging_tmp/migration-start.json")"
aws ecs wait tasks-stopped --cluster "$staging_cluster" --tasks "$staging_migration_task"
staging_migration_stopped=true
aws ecs describe-tasks --cluster "$staging_cluster" --tasks "$staging_migration_task" \
    --output json > "$staging_tmp/migration-result.json"
jq -e '(.failures | length) == 0 and (.tasks | length) == 1
    and .tasks[0].stopCode == "EssentialContainerExited"
    and (.tasks[0].containers | length) == 1
    and .tasks[0].containers[0].name == "web"
    and .tasks[0].containers[0].exitCode == 0' "$staging_tmp/migration-result.json" >/dev/null

if [[ "$staging_previous_count" -lt 1 ]]; then
    staging_count=1
else
    staging_count="$staging_previous_count"
fi
aws ecs update-service --cluster "$staging_cluster" --service "$staging_service" \
    --task-definition "$staging_new" --desired-count "$staging_count" >/dev/null
staging_service_updated=true
staging_failed=false
if ! aws ecs wait services-stable --cluster "$staging_cluster" --services "$staging_service"; then
    staging_failed=true
fi
aws ecs describe-services --cluster "$staging_cluster" --services "$staging_service" \
    --output json > "$staging_tmp/deployed.json"
# A circuit-breaker rollback can also satisfy services-stable; require our revision.
if ! jq -e --arg task "$staging_new" --argjson count "$staging_count" '
    (.failures | length) == 0 and (.services | length) == 1
    and .services[0].taskDefinition == $task
    and .services[0].runningCount == $count
    and (.services[0].deployments | length) == 1
    and .services[0].deployments[0].rolloutState == "COMPLETED"
' "$staging_tmp/deployed.json" >/dev/null; then
    staging_failed=true
fi
if [[ "$staging_failed" != true ]]; then
    if ! curl --fail --silent --show-error --max-time 15 "$staging_url/health/ready/" \
        -o "$staging_tmp/readiness.json"; then
        staging_failed=true
    elif ! jq -e '.status == "ok" and .check == "ready"' "$staging_tmp/readiness.json" >/dev/null; then
        staging_failed=true
    elif ! curl --fail --silent --show-error --max-time 15 \
        "$staging_url/static/admin/css/base.css" -o "$staging_tmp/admin.css" \
        || [[ ! -s "$staging_tmp/admin.css" ]]; then
        staging_failed=true
    fi
fi
if [[ "$staging_failed" == true ]]; then
    echo 'Release verification failed; restoring the previous service revision/count.' >&2
    aws ecs update-service --cluster "$staging_cluster" --service "$staging_service" \
        --task-definition "$staging_previous" --desired-count "$staging_previous_count" >/dev/null
    staging_service_updated=false
    aws ecs wait services-stable --cluster "$staging_cluster" --services "$staging_service"
    # Migrations are not reversed; changes must remain backward compatible.
    exit 1
fi
staging_verified=true
printf 'Staging release passed: %s\nImage: %s\nTask: %s\nPrevious task: %s\n' \
    "$staging_url" "$staging_image" "$staging_new" "$staging_previous"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf 'Staging release passed: %s\n\nImage: `%s`\n\nTask: `%s`\n\nPrevious task: `%s`\n' \
        "$staging_url" "$staging_image" "$staging_new" "$staging_previous" >> "$GITHUB_STEP_SUMMARY"
fi
