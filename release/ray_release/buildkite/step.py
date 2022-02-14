import copy
from typing import Optional, Dict

from ray_release.config import Test


DEFAULT_STEP_TEMPLATE = {
    "env": {
        "ANYSCALE_CLOUD_ID": "cld_4F7k8814aZzGG8TNUGPKnc",
        "ANYSCALE_PROJECT": "prj_2xR6uT6t7jJuu1aCwWMsle",
        "RELEASE_AWS_BUCKET": "ray-release-automation-results",
        "RELEASE_AWS_LOCATION": "dev",
        "RELEASE_AWS_DB_NAME": "ray_ci",
        "RELEASE_AWS_DB_TABLE": "release_test_result",
        "AWS_REGION": "us-west-2",
    },
    "agents": {"queue": "runner_queue_branch"},
    "plugins": [
        {
            "docker#v3.9.0": {
                "image": "rayproject/ray",
                "propagate-environment": True,
                "volumes": [
                    "/var/lib/buildkite/builds:/var/lib/buildkite/builds",
                    "/usr/local/bin/buildkite-agent:/usr/local/bin/buildkite-agent",
                    "/tmp/ray_release_test_artifacts:"
                    "/tmp/ray_release_test_artifacts",
                ],
                "environment": ["BUILDKITE_BUILD_PATH=/var/lib/buildkite/builds"],
            }
        }
    ],
    "artifact_paths": ["/tmp/ray_release_test_artifacts/**/*"],
}


def get_step(
    test: Test,
    smoke_test: bool = False,
    ray_wheels: Optional[str] = None,
    env: Optional[Dict] = None,
):
    env = env or {}

    step = copy.deepcopy(DEFAULT_STEP_TEMPLATE)

    cmd = f"./release/run_release_test.sh \"{test['name']}\" --report"
    if smoke_test:
        cmd += " --smoke-test"

    if ray_wheels:
        cmd += f" --ray-wheels {ray_wheels}"

    step["command"] = cmd
    step["env"].update(env)

    return step
