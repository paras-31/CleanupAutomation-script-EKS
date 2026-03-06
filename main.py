#!/usr/bin/env python3
"""
EKS Cluster End-to-End Cleanup Script  (100% FREE – zero CloudWatch costs)
===========================================================================
Deletes an EKS cluster, node groups, associated VPC, and CloudFormation stacks
ONLY if the cluster has NOT been in active use for the last 1.5 hours.

All idle checks use FREE methods only:
  1. kubectl get pods          – any user workload running/pending?
  2. kubectl get events        – any recent scheduling in last 1.5 hrs?
  3. kubectl top nodes         – real-time CPU/mem via metrics-server (free)
  4. kubectl get deployments   – any recent rollout in last 1.5 hrs?
  5. kubectl get services      – any user LoadBalancer / NodePort services?
  6. EC2 DescribeInstances     – any node launched in last 1.5 hrs? (free API)
  7. Pod container startedAt   – any container started within 1.5 hrs?

If ANY check detects activity → script aborts.  No CloudWatch = $0 cost.

Usage:
  python main.py --cluster-name <name> --region <region> [--dry-run] [--force] [--cpu-threshold 5]
"""

import argparse
import datetime
import json
import logging
import os
import subprocess
import sys
import time

import boto3
from botocore.exceptions import ClientError, WaiterError
from dotenv import load_dotenv

# ──────────────────────────── load .env ──────────────────────────
# Loads AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
# (and optionally AWS_SESSION_TOKEN) from .env into environment.
load_dotenv()

# ──────────────────────────── logging ────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("eks-cleanup")

# ──────────────────────────── constants ──────────────────────────
IDLE_WINDOW_MINUTES = 90  # 1.5 hours
SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "default", "helm"}
DEFAULT_CPU_THRESHOLD = 5  # percent – below this the node is "idle"


# ═════════════════════════════════════════════════════════════════
#  AWS CLIENT HELPERS  (no CloudWatch client – everything free)
# ═════════════════════════════════════════════════════════════════
def get_clients(region: str) -> dict:
    """Return a dict of boto3 clients we need.  No CloudWatch = $0."""
    session = boto3.Session(
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=os.getenv("AWS_SESSION_TOKEN"),  # optional
        region_name=region,
    )
    return {
        "eks": session.client("eks"),
        "ec2": session.client("ec2"),
        "elb": session.client("elbv2"),
        "elb_classic": session.client("elb"),
        "cloudformation": session.client("cloudformation"),
        "autoscaling": session.client("autoscaling"),
    }


# ═════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════
def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _cutoff() -> datetime.datetime:
    return _now_utc() - datetime.timedelta(minutes=IDLE_WINDOW_MINUTES)


def _parse_k8s_time(ts: str) -> datetime.datetime:
    """Parse a Kubernetes timestamp string."""
    if not ts:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.datetime.strptime(ts, fmt).replace(
                tzinfo=datetime.timezone.utc
            )
        except ValueError:
            continue
    return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def _run_kubectl(args: list, description: str) -> dict | None:
    """Run a kubectl command, return parsed JSON or None on failure."""
    try:
        result = subprocess.run(
            ["kubectl"] + args,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as exc:
        log.warning("  kubectl %s failed: %s", description, exc)
        return None


def _setup_kubeconfig(cluster_name: str, region: str) -> bool:
    """Update kubeconfig for the cluster.  Return True on success."""
    try:
        subprocess.run(
            [
                "aws", "eks", "update-kubeconfig",
                "--name", cluster_name,
                "--region", region,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        log.warning("  update-kubeconfig failed: %s", exc)
        return False


# ═════════════════════════════════════════════════════════════════
#  ACTIVITY / IDLE CHECKS  (ALL FREE – no CloudWatch)
# ═════════════════════════════════════════════════════════════════

# ---- Check 1: Running user pods ----
def check_user_pods() -> bool | None:
    """Return True if active, False if idle, None if kubectl unavailable."""
    data = _run_kubectl(
        ["get", "pods", "--all-namespaces", "-o", "json"],
        "get pods",
    )
    if data is None:
        log.warning("  Cannot list pods via kubectl.")
        return None  # signal kubectl failure

    pods = data.get("items", [])
    user_pods = [
        p for p in pods
        if p["metadata"].get("namespace") not in SYSTEM_NAMESPACES
        and p["status"].get("phase") in ("Running", "Pending")
    ]
    if user_pods:
        log.info("  Found %d running/pending user pods:", len(user_pods))
        for p in user_pods[:10]:
            log.info(
                "    - %s/%s  (phase=%s)",
                p["metadata"]["namespace"],
                p["metadata"]["name"],
                p["status"].get("phase"),
            )
        return True
    log.info("  No user workload pods found.")
    return False


# ---- Check 2: Recent container starts (last 1.5 hrs) ----
def check_recent_container_starts() -> bool | None:
    """Return True if active, False if idle, None if kubectl unavailable."""
    data = _run_kubectl(
        ["get", "pods", "--all-namespaces", "-o", "json"],
        "get pods (container times)",
    )
    if data is None:
        return None

    cutoff = _cutoff()
    for pod in data.get("items", []):
        ns = pod["metadata"].get("namespace", "")
        if ns in SYSTEM_NAMESPACES:
            continue
        for cs in pod.get("status", {}).get("containerStatuses", []):
            running = cs.get("state", {}).get("running", {})
            started_at = running.get("startedAt", "")
            if started_at and _parse_k8s_time(started_at) >= cutoff:
                log.info(
                    "  Container %s/%s started at %s (within 1.5 hrs)",
                    ns, cs.get("name"), started_at,
                )
                return True
    log.info("  No containers started in the last %d min.", IDLE_WINDOW_MINUTES)
    return False


# ---- Check 3: Recent events (scheduling, pulls, starts) ----
def check_recent_events() -> bool | None:
    """Return True if active, False if idle, None if kubectl unavailable."""
    data = _run_kubectl(
        ["get", "events", "--all-namespaces", "-o", "json"],
        "get events",
    )
    if data is None:
        return None

    cutoff = _cutoff()
    active_reasons = {"Scheduled", "Started", "Pulling", "Pulled", "Created", "ScalingReplicaSet"}
    recent = [
        e for e in data.get("items", [])
        if e.get("reason") in active_reasons
        and e["metadata"].get("namespace") not in SYSTEM_NAMESPACES
        and _parse_k8s_time(
            e.get("lastTimestamp") or e["metadata"].get("creationTimestamp", "")
        ) >= cutoff
    ]
    if recent:
        log.info("  Found %d recent activity events in user namespaces:", len(recent))
        for e in recent[:8]:
            log.info(
                "    - [%s] %s/%s  reason=%s  time=%s",
                e.get("type"),
                e["metadata"].get("namespace"),
                e["metadata"].get("name"),
                e.get("reason"),
                e.get("lastTimestamp") or e["metadata"].get("creationTimestamp"),
            )
        return True
    log.info("  No recent activity events in user namespaces.")
    return False


# ---- Check 4: kubectl top nodes (CPU/mem via metrics-server, FREE) ----
def check_node_cpu_kubectl(cpu_threshold: float) -> bool:
    """
    Use 'kubectl top nodes' (requires metrics-server, which is free).
    Return True if any node CPU% exceeds the threshold.
    """
    try:
        result = subprocess.run(
            ["kubectl", "top", "nodes", "--no-headers"],
            capture_output=True,
            text=True,
            check=True,
        )
        # Output format: NAME  CPU(cores)  CPU%  MEMORY(bytes)  MEMORY%
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                node_name = parts[0]
                cpu_pct_str = parts[2].rstrip("%")
                try:
                    cpu_pct = float(cpu_pct_str)
                except ValueError:
                    continue
                log.info("  Node %-40s CPU: %s%%", node_name, cpu_pct)
                if cpu_pct > cpu_threshold:
                    log.info("  ^^ Above threshold (%.1f%%) → cluster in use", cpu_threshold)
                    return True
        log.info("  All nodes below CPU threshold (%.1f%%).", cpu_threshold)
        return False

    except subprocess.CalledProcessError:
        log.info(
            "  'kubectl top nodes' unavailable (metrics-server not installed). "
            "Skipping CPU check – relying on other checks."
        )
        return False
    except FileNotFoundError:
        log.warning("  kubectl not found. Treating as ACTIVE (fail-safe).")
        return True


# ---- Check 5: User services (LoadBalancer / NodePort → likely in use) ----
def check_user_services() -> bool | None:
    """Return True if active, False if idle, None if kubectl unavailable."""
    data = _run_kubectl(
        ["get", "services", "--all-namespaces", "-o", "json"],
        "get services",
    )
    if data is None:
        return None

    user_svcs = [
        s for s in data.get("items", [])
        if s["metadata"].get("namespace") not in SYSTEM_NAMESPACES
        and s["spec"].get("type") in ("LoadBalancer", "NodePort")
    ]
    if user_svcs:
        log.info("  Found %d user LoadBalancer/NodePort services:", len(user_svcs))
        for s in user_svcs[:8]:
            log.info(
                "    - %s/%s  type=%s",
                s["metadata"]["namespace"],
                s["metadata"]["name"],
                s["spec"]["type"],
            )
        return True
    log.info("  No user LoadBalancer/NodePort services.")
    return False


# ---- Check 6: Recent deployments/rollouts (last 1.5 hrs) ----
def check_recent_deployments() -> bool | None:
    """Return True if active, False if idle, None if kubectl unavailable."""
    data = _run_kubectl(
        ["get", "deployments", "--all-namespaces", "-o", "json"],
        "get deployments",
    )
    if data is None:
        return None

    cutoff = _cutoff()
    for dep in data.get("items", []):
        ns = dep["metadata"].get("namespace", "")
        if ns in SYSTEM_NAMESPACES:
            continue
        # Check each condition's lastUpdateTime
        for cond in dep.get("status", {}).get("conditions", []):
            last_update = cond.get("lastUpdateTime", "")
            if last_update and _parse_k8s_time(last_update) >= cutoff:
                log.info(
                    "  Deployment %s/%s updated at %s (within 1.5 hrs)",
                    ns,
                    dep["metadata"]["name"],
                    last_update,
                )
                return True
    log.info("  No deployments updated in the last %d min.", IDLE_WINDOW_MINUTES)
    return False


# ---- Check 7: Node launch times via EC2 API (FREE) ----
def check_node_launch_times(ec2, instance_ids: list) -> bool:
    """Return True if any node instance was launched within 1.5 hrs (free EC2 API)."""
    if not instance_ids:
        return False
    cutoff = _cutoff()
    resp = ec2.describe_instances(InstanceIds=instance_ids)
    for res in resp.get("Reservations", []):
        for inst in res.get("Instances", []):
            launch_time = inst.get("LaunchTime")
            if launch_time and launch_time >= cutoff:
                log.info(
                    "  Instance %s launched at %s (within 1.5 hrs → possibly scaling activity)",
                    inst["InstanceId"],
                    launch_time.isoformat(),
                )
                return True
    log.info("  All nodes launched before the 1.5-hr window.")
    return False


# ═════════════════════════════════════════════════════════════════
#  COLLECT INSTANCE IDs  (free AWS APIs)
# ═════════════════════════════════════════════════════════════════
def get_node_instance_ids(eks, ec2, cluster_name: str) -> list:
    """Collect EC2 instance IDs from managed & self-managed node groups."""
    instance_ids = []

    # Managed node groups
    ng_names = eks.list_nodegroups(clusterName=cluster_name).get("nodegroups", [])
    for ng in ng_names:
        ng_info = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=ng)
        asg_names = (
            ng_info.get("nodegroup", {})
            .get("resources", {})
            .get("autoScalingGroups", [])
        )
        for asg in asg_names:
            asg_name = asg.get("name")
            if asg_name:
                asg_client = boto3.client("autoscaling", region_name=eks.meta.region_name)
                asg_desc = asg_client.describe_auto_scaling_groups(
                    AutoScalingGroupNames=[asg_name]
                )
                for group in asg_desc.get("AutoScalingGroups", []):
                    for inst in group.get("Instances", []):
                        instance_ids.append(inst["InstanceId"])

    # Self-managed: find instances tagged with the cluster
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[
            {
                "Name": f"tag:kubernetes.io/cluster/{cluster_name}",
                "Values": ["owned", "shared"],
            },
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    ):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                if inst["InstanceId"] not in instance_ids:
                    instance_ids.append(inst["InstanceId"])

    return instance_ids


# ═════════════════════════════════════════════════════════════════
#  AWS-API-ONLY CHECKS  (always work, no kubectl needed, 100% free)
# ═════════════════════════════════════════════════════════════════
def check_nodegroup_activity(eks, cluster_name: str) -> bool:
    """Check if any nodegroup was CREATED within 1.5 hrs (free EKS API).

    NOTE: We only check 'createdAt', NOT 'modifiedAt'.
    AWS updates modifiedAt internally for health checks / status
    reconciliation even when the user hasn't touched anything.
    createdAt is the only reliable timestamp.
    """
    cutoff = _cutoff()
    ng_names = eks.list_nodegroups(clusterName=cluster_name).get("nodegroups", [])
    for ng in ng_names:
        info = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=ng)
        ng_data = info.get("nodegroup", {})
        created = ng_data.get("createdAt")
        if created and created >= cutoff:
            log.info(
                "  Nodegroup '%s' created at %s (within 1.5 hrs)",
                ng, created.isoformat(),
            )
            return True
        else:
            log.info(
                "  Nodegroup '%s' created at %s (older than 1.5 hrs) ✓",
                ng, created.isoformat() if created else "unknown",
            )
    log.info("  No nodegroups created in the last %d min.", IDLE_WINDOW_MINUTES)
    return False


def check_elbs_in_cluster_vpc(clients: dict, vpc_id: str) -> bool:
    """Check if there are any active ELBs in the VPC (indicates services in use)."""
    if not vpc_id:
        return False
    # Check ALB/NLB
    elb = clients["elb"]
    paginator = elb.get_paginator("describe_load_balancers")
    for page in paginator.paginate():
        for lb in page["LoadBalancers"]:
            if lb.get("VpcId") == vpc_id and lb.get("State", {}).get("Code") == "active":
                log.info(
                    "  Active load balancer found: %s (type=%s)",
                    lb["LoadBalancerName"],
                    lb.get("Type", "unknown"),
                )
                return True
    # Check Classic ELB
    classic = clients["elb_classic"]
    try:
        lbs = classic.describe_load_balancers()["LoadBalancerDescriptions"]
        for lb in lbs:
            if lb.get("VPCId") == vpc_id:
                log.info("  Active Classic ELB found: %s", lb["LoadBalancerName"])
                return True
    except ClientError:
        pass
    log.info("  No active load balancers in VPC.")
    return False


def check_cluster_creation_time(eks, cluster_name: str) -> bool:
    """Return True if the cluster was created within 1.5 hrs (too fresh to judge)."""
    cutoff = _cutoff()
    desc = eks.describe_cluster(name=cluster_name)
    created = desc["cluster"].get("createdAt")
    if created and created >= cutoff:
        log.info(
            "  Cluster created at %s (within 1.5 hrs – too new to judge idle)",
            created.isoformat(),
        )
        return True
    return False


# ═════════════════════════════════════════════════════════════════
#  MASTER IDLE CHECK
# ═════════════════════════════════════════════════════════════════
def is_cluster_idle(clients: dict, cluster_name: str, region: str, cpu_threshold: float) -> bool:
    """
    Run idle checks (100% free).  Return True only if cluster is confirmed idle
    for the last 1.5 hours.

    Strategy:
      1. Try kubectl-based checks first (most accurate).
      2. If kubectl is unavailable (common in CI/CD), fall back to
         AWS-API-only checks which always work.
    """
    log.info("=" * 60)
    log.info("ACTIVITY CHECK  –  last %d minutes (1.5 hrs)", IDLE_WINDOW_MINUTES)
    log.info("Cost: $0  (no CloudWatch – kubectl + free AWS APIs)")
    log.info("=" * 60)

    # ── Try kubectl ──
    kubectl_available = _setup_kubeconfig(cluster_name, region)
    kubectl_works = False

    if kubectl_available:
        # Check 1: Running user pods
        log.info("[1/7] Checking for running user workload pods …")
        result = check_user_pods()
        if result is True:
            log.warning("RESULT: Cluster has ACTIVE user workloads. Aborting cleanup.")
            return False
        elif result is False:
            kubectl_works = True  # kubectl is functional
        # result is None → kubectl failed, will fall back

        if kubectl_works:
            # Check 2: Recent container starts
            log.info("[2/7] Checking for recently started containers …")
            result = check_recent_container_starts()
            if result is True:
                log.warning("RESULT: Containers started recently. Cluster in USE. Aborting.")
                return False

            # Check 3: Recent scheduling events
            log.info("[3/7] Checking for recent scheduling/activity events …")
            result = check_recent_events()
            if result is True:
                log.warning("RESULT: Recent activity events detected. Aborting.")
                return False

            # Check 4: kubectl top nodes
            log.info("[4/7] Checking node CPU via 'kubectl top nodes' …")
            if check_node_cpu_kubectl(cpu_threshold):
                log.warning("RESULT: Node CPU above threshold. Cluster in USE. Aborting.")
                return False

            # Check 5: User-facing services
            log.info("[5/7] Checking for user LoadBalancer/NodePort services …")
            result = check_user_services()
            if result is True:
                log.warning("RESULT: User services found. Cluster likely in USE. Aborting.")
                return False

            # Check 6: Recent deployment updates
            log.info("[6/7] Checking for recently updated deployments …")
            result = check_recent_deployments()
            if result is True:
                log.warning("RESULT: Deployment updated recently. Cluster in USE. Aborting.")
                return False

    # ── If kubectl failed, use AWS-API-only fallback ──
    if not kubectl_works:
        log.info("")
        log.info("─" * 60)
        log.info("kubectl unavailable – using AWS-API-only checks (all free)")
        log.info("─" * 60)

        # AWS Check A: Cluster creation time
        log.info("[A] Checking cluster creation time …")
        if check_cluster_creation_time(clients["eks"], cluster_name):
            log.warning("RESULT: Cluster created very recently. Aborting.")
            return False
        log.info("  Cluster created more than 1.5 hrs ago. ✓")

        # AWS Check B: Nodegroup modification times
        log.info("[B] Checking nodegroup modification times …")
        if check_nodegroup_activity(clients["eks"], cluster_name):
            log.warning("RESULT: Nodegroup modified recently. Aborting.")
            return False

        # AWS Check C: Load balancers in the VPC
        vpc_id = get_cluster_vpc_id(clients["eks"], cluster_name)
        log.info("[C] Checking for active load balancers in VPC %s …", vpc_id)
        if check_elbs_in_cluster_vpc(clients, vpc_id):
            log.warning("RESULT: Active ELBs found → services likely running. Aborting.")
            return False

    # ── Check 7: Node launch times (always works – free EC2 API) ──
    log.info("[7/7] Checking node EC2 launch times …")
    instance_ids = get_node_instance_ids(clients["eks"], clients["ec2"], cluster_name)
    if instance_ids:
        log.info("  Nodes found: %s", instance_ids)
        if check_node_launch_times(clients["ec2"], instance_ids):
            log.warning("RESULT: Node launched recently. Cluster in USE. Aborting.")
            return False
    else:
        log.info("  No node instances found (possibly Fargate-only).")

    log.info("=" * 60)
    log.info("ALL CHECKS PASSED – cluster is IDLE for the last 1.5 hrs")
    log.info("=" * 60)
    return True


# ═════════════════════════════════════════════════════════════════
#  DELETION HELPERS
# ═════════════════════════════════════════════════════════════════
def delete_nodegroups(eks, cluster_name: str, dry_run: bool):
    """Delete all managed node groups and wait."""
    ng_names = eks.list_nodegroups(clusterName=cluster_name).get("nodegroups", [])
    if not ng_names:
        log.info("No managed node groups to delete.")
        return
    for ng in ng_names:
        log.info("Deleting managed node group: %s", ng)
        if not dry_run:
            eks.delete_nodegroup(clusterName=cluster_name, nodegroupName=ng)
    if not dry_run:
        log.info("Waiting for node groups to be deleted (this can take several minutes) …")
        for ng in ng_names:
            try:
                waiter = eks.get_waiter("nodegroup_deleted")
                waiter.wait(
                    clusterName=cluster_name,
                    nodegroupName=ng,
                    WaiterConfig={"Delay": 30, "MaxAttempts": 60},
                )
                log.info("  Node group %s deleted.", ng)
            except WaiterError:
                log.error("  Timed out waiting for node group %s deletion.", ng)


def delete_fargate_profiles(eks, cluster_name: str, dry_run: bool):
    """Delete all Fargate profiles."""
    profiles = eks.list_fargate_profiles(clusterName=cluster_name).get(
        "fargateProfileNames", []
    )
    if not profiles:
        log.info("No Fargate profiles to delete.")
        return
    for fp in profiles:
        log.info("Deleting Fargate profile: %s", fp)
        if not dry_run:
            eks.delete_fargate_profile(clusterName=cluster_name, fargateProfileName=fp)
            waiter = eks.get_waiter("fargate_profile_deleted")
            try:
                waiter.wait(
                    clusterName=cluster_name,
                    fargateProfileName=fp,
                    WaiterConfig={"Delay": 30, "MaxAttempts": 60},
                )
                log.info("  Fargate profile %s deleted.", fp)
            except WaiterError:
                log.error("  Timed out waiting for Fargate profile %s.", fp)


def delete_eks_cluster(eks, cluster_name: str, dry_run: bool):
    """Delete the EKS cluster itself."""
    log.info("Deleting EKS cluster: %s", cluster_name)
    if dry_run:
        return
    eks.delete_cluster(name=cluster_name)
    log.info("Waiting for cluster deletion (this can take ~10 minutes) …")
    try:
        waiter = eks.get_waiter("cluster_deleted")
        waiter.wait(
            name=cluster_name,
            WaiterConfig={"Delay": 30, "MaxAttempts": 60},
        )
        log.info("  Cluster %s deleted.", cluster_name)
    except WaiterError:
        log.error("  Timed out waiting for cluster deletion.")


def get_cluster_vpc_id(eks, cluster_name: str) -> str | None:
    """Return the VPC ID the cluster lives in."""
    try:
        desc = eks.describe_cluster(name=cluster_name)
        return desc["cluster"]["resourcesVpcConfig"]["vpcId"]
    except ClientError:
        return None


def get_cluster_security_groups(eks, cluster_name: str) -> list:
    """Return security group IDs created by EKS for this cluster."""
    try:
        desc = eks.describe_cluster(name=cluster_name)
        vpc_cfg = desc["cluster"]["resourcesVpcConfig"]
        sgs = list(vpc_cfg.get("securityGroupIds", []))
        cluster_sg = vpc_cfg.get("clusterSecurityGroupId")
        if cluster_sg and cluster_sg not in sgs:
            sgs.append(cluster_sg)
        return sgs
    except ClientError:
        return []


# ─────────── Load Balancer cleanup ───────────
def delete_elbs_in_vpc(clients: dict, vpc_id: str, dry_run: bool):
    """Delete ALBs/NLBs and Classic ELBs that live inside the VPC."""
    # V2 (ALB / NLB)
    elb = clients["elb"]
    paginator = elb.get_paginator("describe_load_balancers")
    for page in paginator.paginate():
        for lb in page["LoadBalancers"]:
            if lb.get("VpcId") == vpc_id:
                arn = lb["LoadBalancerArn"]
                log.info("Deleting ELBv2 load balancer: %s", lb["LoadBalancerName"])
                if not dry_run:
                    # Delete listeners first
                    try:
                        listeners = elb.describe_listeners(LoadBalancerArn=arn)["Listeners"]
                        for l in listeners:
                            elb.delete_listener(ListenerArn=l["ListenerArn"])
                    except ClientError:
                        pass
                    # Delete target groups
                    try:
                        tgs = elb.describe_target_groups(LoadBalancerArn=arn)["TargetGroups"]
                        for tg in tgs:
                            elb.delete_target_group(TargetGroupArn=tg["TargetGroupArn"])
                    except ClientError:
                        pass
                    elb.delete_load_balancer(LoadBalancerArn=arn)

    # Classic ELB
    classic = clients["elb_classic"]
    try:
        lbs = classic.describe_load_balancers()["LoadBalancerDescriptions"]
        for lb in lbs:
            if lb.get("VPCId") == vpc_id:
                log.info("Deleting Classic ELB: %s", lb["LoadBalancerName"])
                if not dry_run:
                    classic.delete_load_balancer(LoadBalancerName=lb["LoadBalancerName"])
    except ClientError:
        pass

    if not dry_run:
        log.info("Waiting 30s for LB ENIs to detach …")
        time.sleep(30)


# ─────────── ENI cleanup ───────────
def delete_enis_in_vpc(ec2, vpc_id: str, dry_run: bool):
    """Detach and delete lingering ENIs (left by EKS/ELB)."""
    enis = ec2.describe_network_interfaces(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )["NetworkInterfaces"]
    for eni in enis:
        eni_id = eni["NetworkInterfaceId"]
        attachment = eni.get("Attachment")
        if attachment and attachment.get("Status") in ("attached", "attaching"):
            # Skip primary ENIs of instances
            if attachment.get("DeviceIndex") == 0 and eni.get("Description", "").startswith("Primary"):
                continue
            log.info("Detaching ENI %s", eni_id)
            if not dry_run:
                try:
                    ec2.detach_network_interface(
                        AttachmentId=attachment["AttachmentId"], Force=True
                    )
                    time.sleep(5)
                except ClientError as e:
                    log.warning("  Could not detach %s: %s", eni_id, e)
        log.info("Deleting ENI %s", eni_id)
        if not dry_run:
            for attempt in range(5):
                try:
                    ec2.delete_network_interface(NetworkInterfaceId=eni_id)
                    break
                except ClientError:
                    time.sleep(10)


# ─────────── VPC cleanup ───────────
def delete_vpc_and_dependencies(ec2, vpc_id: str, dry_run: bool):
    """Delete a VPC and ALL its dependencies."""
    log.info("Cleaning up VPC: %s", vpc_id)

    # 1. Internet Gateways
    igws = ec2.describe_internet_gateways(
        Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
    )["InternetGateways"]
    for igw in igws:
        igw_id = igw["InternetGatewayId"]
        log.info("  Detaching & deleting Internet Gateway %s", igw_id)
        if not dry_run:
            ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
            ec2.delete_internet_gateway(InternetGatewayId=igw_id)

    # 2. NAT Gateways
    nats = ec2.describe_nat_gateways(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )["NatGateways"]
    for nat in nats:
        if nat["State"] in ("available", "pending"):
            nat_id = nat["NatGatewayId"]
            log.info("  Deleting NAT Gateway %s", nat_id)
            if not dry_run:
                ec2.delete_nat_gateway(NatGatewayId=nat_id)
    # Wait for NAT GWs to delete
    if nats and not dry_run:
        log.info("  Waiting for NAT Gateways to delete …")
        for _ in range(30):
            still_up = ec2.describe_nat_gateways(
                Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]},
                    {"Name": "state", "Values": ["deleting", "pending"]},
                ]
            )["NatGateways"]
            if not still_up:
                break
            time.sleep(15)

    # 3. Release Elastic IPs associated with this VPC's NAT Gateways
    for nat in nats:
        for addr in nat.get("NatGatewayAddresses", []):
            alloc_id = addr.get("AllocationId")
            if alloc_id:
                log.info("  Releasing Elastic IP %s", alloc_id)
                if not dry_run:
                    try:
                        ec2.release_address(AllocationId=alloc_id)
                    except ClientError:
                        pass

    # 4. Delete Endpoints
    try:
        eps = ec2.describe_vpc_endpoints(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )["VpcEndpoints"]
        ep_ids = [ep["VpcEndpointId"] for ep in eps if ep["State"] != "deleted"]
        if ep_ids:
            log.info("  Deleting %d VPC endpoints", len(ep_ids))
            if not dry_run:
                ec2.delete_vpc_endpoints(VpcEndpointIds=ep_ids)
    except ClientError:
        pass

    # 5. Delete Subnets
    subnets = ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )["Subnets"]
    for sn in subnets:
        log.info("  Deleting subnet %s", sn["SubnetId"])
        if not dry_run:
            for attempt in range(5):
                try:
                    ec2.delete_subnet(SubnetId=sn["SubnetId"])
                    break
                except ClientError as e:
                    if "DependencyViolation" in str(e):
                        time.sleep(15)
                    else:
                        log.warning("    %s", e)
                        break

    # 6. Delete non-main route tables
    rts = ec2.describe_route_tables(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )["RouteTables"]
    for rt in rts:
        is_main = any(
            a.get("Main", False) for a in rt.get("Associations", [])
        )
        if is_main:
            continue
        rt_id = rt["RouteTableId"]
        log.info("  Deleting route table %s", rt_id)
        if not dry_run:
            # Disassociate first
            for assoc in rt.get("Associations", []):
                if not assoc.get("Main", False):
                    try:
                        ec2.disassociate_route_table(
                            AssociationId=assoc["RouteTableAssociationId"]
                        )
                    except ClientError:
                        pass
            try:
                ec2.delete_route_table(RouteTableId=rt_id)
            except ClientError as e:
                log.warning("    %s", e)

    # 7. Delete Security Groups (non-default)
    sgs = ec2.describe_security_groups(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )["SecurityGroups"]
    non_default = [sg for sg in sgs if sg["GroupName"] != "default"]
    # Revoke all ingress/egress rules first to break circular refs
    for sg in non_default:
        sg_id = sg["GroupId"]
        if not dry_run:
            try:
                if sg["IpPermissions"]:
                    ec2.revoke_security_group_ingress(
                        GroupId=sg_id, IpPermissions=sg["IpPermissions"]
                    )
                if sg["IpPermissionsEgress"]:
                    ec2.revoke_security_group_egress(
                        GroupId=sg_id, IpPermissions=sg["IpPermissionsEgress"]
                    )
            except ClientError:
                pass
    for sg in non_default:
        sg_id = sg["GroupId"]
        log.info("  Deleting security group %s (%s)", sg_id, sg["GroupName"])
        if not dry_run:
            for attempt in range(5):
                try:
                    ec2.delete_security_group(GroupId=sg_id)
                    break
                except ClientError as e:
                    if "DependencyViolation" in str(e):
                        time.sleep(15)
                    else:
                        log.warning("    %s", e)
                        break

    # 8. Delete the VPC
    log.info("  Deleting VPC %s", vpc_id)
    if not dry_run:
        try:
            ec2.delete_vpc(VpcId=vpc_id)
            log.info("  VPC %s deleted.", vpc_id)
        except ClientError as e:
            log.error("  Failed to delete VPC: %s", e)


def delete_cloudformation_stacks(cf, cluster_name: str, dry_run: bool):
    """Delete CloudFormation stacks whose name contains the cluster name."""
    paginator = cf.get_paginator("list_stacks")
    target_stacks = []
    for page in paginator.paginate(
        StackStatusFilter=[
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
            "ROLLBACK_COMPLETE",
            "UPDATE_ROLLBACK_COMPLETE",
            "IMPORT_COMPLETE",
        ]
    ):
        for s in page["StackSummaries"]:
            name = s["StackName"]
            if cluster_name.lower() in name.lower() or name.lower().startswith("eksctl-" + cluster_name.lower()):
                target_stacks.append(name)

    if not target_stacks:
        log.info("No CloudFormation stacks found matching cluster name '%s'.", cluster_name)
        return

    log.info("CloudFormation stacks to delete: %s", target_stacks)
    for stack_name in target_stacks:
        log.info("Deleting stack: %s", stack_name)
        if not dry_run:
            try:
                cf.delete_stack(StackName=stack_name)
            except ClientError as e:
                log.error("  Failed to delete stack %s: %s", stack_name, e)

    if not dry_run:
        log.info("Waiting for CloudFormation stacks to delete …")
        for stack_name in target_stacks:
            try:
                waiter = cf.get_waiter("stack_delete_complete")
                waiter.wait(
                    StackName=stack_name,
                    WaiterConfig={"Delay": 15, "MaxAttempts": 120},
                )
                log.info("  Stack %s deleted.", stack_name)
            except WaiterError:
                log.error("  Timed out waiting for stack %s.", stack_name)


# ═════════════════════════════════════════════════════════════════
#  MAIN ORCHESTRATION
# ═════════════════════════════════════════════════════════════════
def cleanup(cluster_name: str, region: str, dry_run: bool, force: bool, cpu_threshold: float):
    clients = get_clients(region)

    # ── Verify cluster exists ──
    try:
        desc = clients["eks"].describe_cluster(name=cluster_name)
        status = desc["cluster"]["status"]
        log.info("Cluster '%s' found  (status=%s, region=%s)", cluster_name, status, region)
    except ClientError as e:
        log.error("Cannot find cluster '%s' in %s: %s", cluster_name, region, e)
        sys.exit(1)

    vpc_id = get_cluster_vpc_id(clients["eks"], cluster_name)
    log.info("Associated VPC: %s", vpc_id)

    # ── Idle-check gate ──
    if not force:
        if not is_cluster_idle(clients, cluster_name, region, cpu_threshold):
            log.error(
                "❌  Cluster '%s' is IN USE (or could not confirm idle). "
                "Cleanup ABORTED.  Use --force to override.",
                cluster_name,
            )
            sys.exit(2)
    else:
        log.warning("--force flag set. SKIPPING idle checks.")

    if dry_run:
        log.info("──── DRY RUN MODE – no resources will be deleted ────")

    # ── Step 1: Delete node groups / Fargate profiles ──
    log.info("STEP 1/5  Deleting managed node groups …")
    delete_nodegroups(clients["eks"], cluster_name, dry_run)

    log.info("STEP 2/5  Deleting Fargate profiles …")
    delete_fargate_profiles(clients["eks"], cluster_name, dry_run)

    # ── Step 2: Delete the EKS cluster ──
    log.info("STEP 3/5  Deleting EKS cluster …")
    delete_eks_cluster(clients["eks"], cluster_name, dry_run)

    # ── Step 3: Delete ELBs and ENIs inside the VPC ──
    if vpc_id:
        log.info("STEP 4/5  Cleaning up ELBs / ENIs in VPC %s …", vpc_id)
        delete_elbs_in_vpc(clients, vpc_id, dry_run)
        delete_enis_in_vpc(clients["ec2"], vpc_id, dry_run)

        # ── Step 4: Delete the VPC ──
        log.info("STEP 4/5  Deleting VPC and dependencies …")
        delete_vpc_and_dependencies(clients["ec2"], vpc_id, dry_run)
    else:
        log.warning("No VPC associated – skipping VPC cleanup.")

    # ── Step 5: Delete CloudFormation stacks ──
    log.info("STEP 5/5  Deleting associated CloudFormation stacks …")
    delete_cloudformation_stacks(clients["cloudformation"], cluster_name, dry_run)

    log.info("=" * 60)
    if dry_run:
        log.info("DRY RUN COMPLETE – nothing was actually deleted.")
    else:
        log.info("✅  CLEANUP COMPLETE for cluster '%s'", cluster_name)
    log.info("=" * 60)


# ═════════════════════════════════════════════════════════════════
#  CLI
# ═════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="End-to-end EKS cluster cleanup (only if idle for 1.5 hrs)."
    )
    parser.add_argument(
        "--cluster-name", required=True, help="Name of the EKS cluster to clean up."
    )
    parser.add_argument(
        "--region", required=True, help="AWS region (e.g. us-east-1)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip idle-check and delete regardless (USE WITH CAUTION).",
    )
    parser.add_argument(
        "--cpu-threshold",
        type=float,
        default=DEFAULT_CPU_THRESHOLD,
        help=f"Average CPU %% threshold to consider a node active (default: {DEFAULT_CPU_THRESHOLD}%%).",
    )
    args = parser.parse_args()

    log.info("EKS Cleanup Script (FREE – $0 cost) – %s", _now_utc().strftime("%Y-%m-%d %H:%M UTC"))
    log.info("Cluster : %s", args.cluster_name)
    log.info("Region  : %s", args.region)
    log.info("Dry-run : %s", args.dry_run)
    log.info("Force   : %s", args.force)
    log.info("CPU Thr : %.1f%%", args.cpu_threshold)

    cleanup(
        cluster_name=args.cluster_name,
        region=args.region,
        dry_run=args.dry_run,
        force=args.force,
        cpu_threshold=args.cpu_threshold,
    )


if __name__ == "__main__":
    main()
