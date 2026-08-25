"""Default Sandbox runtime configuration contracts."""

import pytest
from opensandbox.models.sandboxes import PVC, Volume
from pydantic import ValidationError

from tinkerfin_sandbox.models import OpenSandboxConfig


def test_default_sandbox_uses_immutable_standard_runtime() -> None:
    config = OpenSandboxConfig()

    assert config.image == (
        "ghcr.io/tinkerfin-ai/sandbox-runtime@"
        "sha256:d940061954b3b7f5a068f2cab1ff483669d58c4cb8af9c2753deab600d479590"
    )
    assert config.entrypoint == ["/opt/sandbox-runtime/bin/entrypoint.sh"]
    assert config.env == {}
    assert config.workspace_root == "/workspace"


def test_workspace_root_normalizes_absolute_posix_path() -> None:
    config = OpenSandboxConfig(workspace_root="/workspace/./projects/")

    assert config.workspace_root == "/workspace/projects"


@pytest.mark.parametrize(
    "workspace_root",
    [
        "workspace",
        "/",
        "//workspace",
        "/workspace/../etc",
        "/workspace\x00private",
    ],
)
def test_workspace_root_rejects_unsafe_boundaries(workspace_root: str) -> None:
    with pytest.raises(ValidationError):
        OpenSandboxConfig(workspace_root=workspace_root)


def test_workspace_root_allows_explicit_opt_out() -> None:
    assert OpenSandboxConfig(workspace_root=None).workspace_root is None


def test_config_accepts_pvc_volume_mounts_without_sharing_mutable_input() -> None:
    volume = Volume(
        name="workspace-data",
        pvc=PVC(
            claimName="tinkerfin-workspaces",
            createIfNotExists=False,
        ),
        mountPath="/workspace/data",
    )

    config = OpenSandboxConfig.model_validate({"volumes": [volume]})
    volume.mount_path = "/mutated"

    assert isinstance(config.volumes, tuple)
    assert config.volumes[0].mount_path == "/workspace/data"
    assert config.volumes[0].pvc is not None
    assert config.volumes[0].pvc.claim_name == "tinkerfin-workspaces"


def test_config_rejects_framework_reserved_metadata_keys() -> None:
    with pytest.raises(ValidationError, match="tinkerfin.ai/"):
        OpenSandboxConfig(
            metadata={
                "team": "agents",
                "tinkerfin.ai/owner": "forged-owner",
            }
        )


@pytest.mark.parametrize("field", ("command_timeout", "warm_pool_size"))
def test_integer_capacity_and_timeout_fields_reject_booleans(field: str) -> None:
    with pytest.raises(ValidationError):
        OpenSandboxConfig(**{field: True})
