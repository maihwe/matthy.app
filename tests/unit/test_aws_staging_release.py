"""Exercise release failure ordering with fake AWS/curl executables, no cloud writes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

FAKE_AWS = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
scenario = os.environ['STAGING_SCENARIO']
trace = Path(os.environ['STAGING_TRACE'])
with trace.open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
def emit(value):
    print(json.dumps(value))
def value(name):
    return args[args.index(name) + 1]
command = args[:2]
if command == ['cloudformation', 'describe-stacks']:
    emit([{'OutputKey': k, 'OutputValue': v} for k,v in {
        'ClusterName': 'ihweza-staging', 'ServiceName': 'ihweza-staging-web',
        'PrivateSubnetIds': 'subnet-a,subnet-b', 'ApplicationSecurityGroupId': 'sg-staging',
        'StagingUrl': 'https://staging.example.test'}.items()])
elif command == ['ecs', 'describe-services']:
    updates = [json.loads(line) for line in trace.read_text().splitlines()
               if json.loads(line)[:2] == ['ecs', 'update-service']]
    task = 'old-task' if not updates or scenario == 'circuit-rollback' else 'new-task'
    emit({'failures': [], 'services': [{'status': 'ACTIVE', 'taskDefinition': task,
        'desiredCount': 0, 'runningCount': 1, 'deployments': [{'rolloutState': 'COMPLETED'}]}]})
elif command == ['ecs', 'describe-task-definition']:
    emit({'family': 'ihweza-staging-web', 'cpu': '512', 'memory': '1024',
        'containerDefinitions': [{'name': 'web', 'image': 'old-image', 'healthCheck': {},
         'portMappings': []}, {'name': 'readiness', 'image': 'old-image', 'dependsOn': []}]})
elif command == ['ecs', 'register-task-definition']:
    definition = json.loads(Path(value('--cli-input-json')[7:]).read_text())
    migration = definition['containerDefinitions'][0].get('command', [])
    if migration:
        assert len(definition['containerDefinitions']) == 1
        assert migration == ['python', 'manage.py', 'migrate', '--noinput']
        assert 'healthCheck' not in definition['containerDefinitions'][0]
    print('migration-definition' if migration else 'new-task')
elif command == ['ecs', 'run-task']:
    emit({'failures': [{'reason': 'Capacity unavailable'}], 'tasks': []} if scenario == 'run-failure'
        else {'failures': [], 'tasks': [{'taskArn': 'migration-task'}]})
elif command == ['ecs', 'describe-tasks']:
    emit({'failures': [], 'tasks': [{'stopCode': 'EssentialContainerExited',
        'containers': [{'name': 'web', 'exitCode': 1 if scenario == 'migration-failure' else 0}]}]})
elif command == ['ecs', 'wait']:
    if args[2] == 'tasks-stopped' and scenario == 'migration-timeout':
        sys.exit(1)
elif command in [['ecs', 'update-service'], ['ecs', 'stop-task']]:
    pass
else:
    raise AssertionError(args)
"""

FAKE_CURL = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
args = sys.argv[1:]
scenario = os.environ['STAGING_SCENARIO']
output = Path(args[args.index('-o') + 1])
if scenario == 'https-failure':
    sys.exit(22)
if any('/health/ready/' in arg for arg in args):
    output.write_text(json.dumps({'status': 'ok', 'check': 'ready'}))
else:
    output.write_text('' if scenario == 'empty-static' else 'body { color: black; }')
"""


@pytest.mark.parametrize(
    ("scenario", "success", "updates", "stopped"),
    [
        ("success", True, ["new-task"], False),
        ("run-failure", False, [], False),
        ("migration-failure", False, [], False),
        ("migration-timeout", False, [], True),
        ("circuit-rollback", False, ["new-task", "old-task"], False),
        ("https-failure", False, ["new-task", "old-task"], False),
        ("empty-static", False, ["new-task", "old-task"], False),
    ],
)
def test_release_never_deploys_failed_migrations_and_recognizes_rollback(
    tmp_path: Path, scenario: str, success: bool, updates: list[str], stopped: bool
) -> None:
    for name, source in [("aws", FAKE_AWS), ("curl", FAKE_CURL)]:
        executable = tmp_path / name
        executable.write_text(source)
        executable.chmod(0o700)
    trace = tmp_path / "trace.jsonl"
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "STAGING_SCENARIO": scenario,
        "STAGING_TRACE": str(trace),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }
    result = subprocess.run(  # noqa: S603 — fixed repository script and synthetic image
        [
            shutil.which("bash") or "/bin/bash",
            "scripts/aws_staging_release.sh",
            "123456789012.dkr.ecr.eu-west-1.amazonaws.com/ihweza-staging@sha256:" + "a" * 64,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert (result.returncode == 0) is success, result.stderr
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    changes = [call for call in calls if call[:2] == ["ecs", "update-service"]]
    assert [call[call.index("--task-definition") + 1] for call in changes] == updates
    assert any(call[:2] == ["ecs", "stop-task"] for call in calls) is stopped
    if not success:
        assert not (tmp_path / "summary.md").exists()
    if updates and updates[-1] == "old-task":
        assert changes[-1][changes[-1].index("--desired-count") + 1] == "0"
