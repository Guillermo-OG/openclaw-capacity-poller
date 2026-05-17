#!/usr/bin/env python3
"""Poll Oracle Cloud for VM.Standard.A1.Flex capacity across all ADs.

On success: launch instance, ping Telegram with public IP, write capacity_found.flag.
On capacity-out: exit 0 so GitHub Actions stays green.
"""

from __future__ import annotations

import base64
import os
import pathlib
import sys
import tempfile
import time

import oci
import requests
from oci.core.models import (
    CreateVnicDetails,
    InstanceSourceViaImageDetails,
    LaunchInstanceAgentConfigDetails,
    LaunchInstanceDetails,
    LaunchInstanceShapeConfigDetails,
)
from oci.exceptions import ServiceError

SHAPE = "VM.Standard.A1.Flex"
OCPUS = 4
MEMORY_GB = 24
BOOT_VOLUME_GB = 100
DISPLAY_NAME = "openclaw-gateway"

CLOUD_INIT = """#cloud-config
hostname: openclaw-gateway
timezone: Etc/UTC

users:
  - name: openclaw
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    groups: [sudo, docker]
    ssh_authorized_keys:
      - {ssh_key}

package_update: true
package_upgrade: true
packages:
  - curl
  - ufw
  - fail2ban
  - docker.io
  - git
  - jq

swap:
  filename: /swapfile
  size: 2G
  maxsize: 2G

runcmd:
  - systemctl enable --now docker
  - usermod -aG docker openclaw
  - ufw default deny incoming
  - ufw default allow outgoing
  - ufw allow 22/tcp
  - ufw --force enable
  - sysctl -w fs.file-max=1048576
  - sysctl -w net.core.somaxconn=4096
  - echo 'fs.file-max=1048576' >> /etc/sysctl.conf
  - echo 'net.core.somaxconn=4096' >> /etc/sysctl.conf
"""


def env(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "")
    if required and not val:
        sys.exit(f"FATAL: missing env var {name}")
    return val


def telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (token and chat):
        print("telegram creds missing, skipping send", file=sys.stderr)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"telegram send failed: {e}", file=sys.stderr)


def build_clients() -> tuple[oci.core.ComputeClient, oci.identity.IdentityClient, oci.core.VirtualNetworkClient]:
    key_pem = env("OCI_PRIVATE_KEY_PEM")
    if not key_pem.endswith("\n"):
        key_pem += "\n"
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".pem", delete=False, prefix="oci_key_"
    )
    tmp.write(key_pem)
    tmp.close()
    os.chmod(tmp.name, 0o600)

    config = {
        "user": env("OCI_USER_OCID"),
        "fingerprint": env("OCI_FINGERPRINT"),
        "tenancy": env("OCI_TENANCY_OCID"),
        "region": env("OCI_REGION"),
        "key_file": tmp.name,
    }
    oci.config.validate_config(config)
    return (
        oci.core.ComputeClient(config),
        oci.identity.IdentityClient(config),
        oci.core.VirtualNetworkClient(config),
    )


def latest_ubuntu_arm_image(compute: oci.core.ComputeClient, compartment_id: str) -> str:
    images = compute.list_images(
        compartment_id=compartment_id,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
        lifecycle_state="AVAILABLE",
    ).data
    if not images:
        sys.exit("FATAL: no Ubuntu 22.04 ARM image found")
    return images[0].id


def fetch_public_ip(
    compute: oci.core.ComputeClient,
    vnet: oci.core.VirtualNetworkClient,
    compartment_id: str,
    instance_id: str,
    timeout_s: int = 180,
) -> str | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            atts = compute.list_vnic_attachments(
                compartment_id=compartment_id, instance_id=instance_id
            ).data
            if atts:
                vnic = vnet.get_vnic(atts[0].vnic_id).data
                if vnic.public_ip:
                    return vnic.public_ip
        except ServiceError:
            pass
        time.sleep(5)
    return None


def try_launch(
    compute: oci.core.ComputeClient,
    ad_name: str,
    compartment_id: str,
    subnet_id: str,
    image_id: str,
    ssh_pub_key: str,
) -> str | None:
    user_data = base64.b64encode(
        CLOUD_INIT.format(ssh_key=ssh_pub_key.strip()).encode()
    ).decode()

    details = LaunchInstanceDetails(
        availability_domain=ad_name,
        compartment_id=compartment_id,
        shape=SHAPE,
        shape_config=LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS, memory_in_gbs=MEMORY_GB
        ),
        display_name=DISPLAY_NAME,
        source_details=InstanceSourceViaImageDetails(
            image_id=image_id,
            boot_volume_size_in_gbs=BOOT_VOLUME_GB,
        ),
        create_vnic_details=CreateVnicDetails(
            subnet_id=subnet_id,
            assign_public_ip=True,
        ),
        metadata={
            "ssh_authorized_keys": ssh_pub_key.strip(),
            "user_data": user_data,
        },
        is_pv_encryption_in_transit_enabled=True,
        agent_config=LaunchInstanceAgentConfigDetails(
            is_management_disabled=False,
            is_monitoring_disabled=False,
        ),
    )

    try:
        instance = compute.launch_instance(details).data
        return instance.id
    except ServiceError as e:
        msg = (e.message or "").lower()
        code = (e.code or "").lower()
        if "out of host capacity" in msg or "out of capacity" in msg:
            print(f"[{ad_name}] out of capacity")
            return None
        if "limitexceeded" in code:
            print(f"[{ad_name}] LimitExceeded — quota hit: {e.message}")
            telegram(f"OCI quota hit in `{ad_name}`: {e.message}")
            return None
        raise


def main() -> int:
    compute, identity, vnet = build_clients()

    tenancy_id = env("OCI_TENANCY_OCID")
    compartment_id = env("OCI_COMPARTMENT_OCID")
    subnet_id = env("OCI_SUBNET_OCID")
    ssh_pub = env("SSH_PUBLIC_KEY")
    event = os.environ.get("GITHUB_EVENT_NAME", "manual")
    region = os.environ.get("OCI_REGION", "?")

    print(f"event={event} region={region}")

    image_id = latest_ubuntu_arm_image(compute, compartment_id)
    print(f"image={image_id}")

    ads = identity.list_availability_domains(compartment_id=tenancy_id).data
    ad_names = [a.name for a in ads]
    print(f"ads={ad_names}")

    if event == "workflow_dispatch":
        telegram(
            "OCI poller *smoke test*: auth OK, "
            f"trying {len(ad_names)} AD(s) for `{SHAPE}` "
            f"{OCPUS} OCPU / {MEMORY_GB} GB in `{region}`."
        )

    for ad in ads:
        instance_id = try_launch(
            compute, ad.name, compartment_id, subnet_id, image_id, ssh_pub
        )
        if instance_id:
            print(f"LAUNCHED in {ad.name}: {instance_id}")
            ip = fetch_public_ip(compute, vnet, compartment_id, instance_id)
            telegram(
                "*OPENCLAW VM CREATED*\n"
                f"AD: `{ad.name}`\n"
                f"Instance: `{instance_id}`\n"
                f"Public IP: `{ip or 'pending'}`\n"
                f"SSH: `ssh openclaw@{ip or '<ip>'}`\n"
                "Workflow auto-disabled."
            )
            pathlib.Path("capacity_found.flag").write_text(
                f"{instance_id}\n{ip or ''}\n"
            )
            return 0

    print("All ADs out of capacity. Will retry next cron.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
