# EKS Cluster End-to-End Cleanup Script  (100% FREE – $0 cost)

Automated Python script that **safely deletes** an entire EKS cluster along with its node groups, Fargate profiles, VPC (and all dependencies), load balancers, and associated CloudFormation stacks.

> **Zero cost:** All idle checks use `kubectl` and free AWS APIs only. **No CloudWatch calls = no charges.**

**Safety first:** Cleanup only proceeds if the cluster has been **idle for the last 1.5 hours**. The script runs **7 independent checks** before allowing deletion:

| # | Check | Method | Cost |
|---|-------|--------|------|
| 1 | **Running user pods** | `kubectl get pods` | Free |
| 2 | **Recent container starts** | Pod `containerStatuses.startedAt` | Free |
| 3 | **Recent scheduling events** | `kubectl get events` | Free |
| 4 | **Node CPU usage** | `kubectl top nodes` (metrics-server) | Free |
| 5 | **User services** | `kubectl get services` (LB/NodePort) | Free |
| 6 | **Recent deployments** | `kubectl get deployments` conditions | Free |
| 7 | **Node launch times** | EC2 `DescribeInstances` API | Free |

If **any** check shows activity, cleanup is **aborted**.

---

## Prerequisites

- Python 3.10+
- AWS CLI configured (`aws configure`) with permissions to manage EKS, EC2, ELB, CloudFormation
- `kubectl` installed and in PATH
- `boto3` (`pip install -r requirements.txt`)
- *(Optional)* metrics-server on your cluster for `kubectl top nodes` CPU check — if not installed, that check is gracefully skipped and other checks still apply

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Dry-run first (shows what would be deleted, deletes nothing)
python main.py --cluster-name my-cluster --region us-east-1 --dry-run

# Actual cleanup (only if idle for 1.5 hrs)
python main.py --cluster-name my-cluster --region us-east-1

# Force cleanup (SKIP idle checks – use with caution)
python main.py --cluster-name my-cluster --region us-east-1 --force

# Custom CPU threshold for kubectl top nodes (default 5%)
python main.py --cluster-name my-cluster --region us-east-1 --cpu-threshold 10
```

## CLI Options

| Flag | Required | Description |
|------|----------|-------------|
| `--cluster-name` | Yes | Name of the EKS cluster |
| `--region` | Yes | AWS region (e.g. `us-east-1`) |
| `--dry-run` | No | Preview mode – no deletions |
| `--force` | No | Skip idle checks (dangerous) |
| `--cpu-threshold` | No | CPU % for `kubectl top nodes` to consider node active (default: 5) |

## Deletion Order

1. Managed node groups (waits for completion)
2. Fargate profiles (waits for completion)
3. EKS cluster (waits for completion)
4. Load balancers (ALB/NLB/Classic) inside the VPC
5. Detach & delete lingering ENIs
6. VPC dependencies: IGWs → NAT GWs → Elastic IPs → VPC Endpoints → Subnets → Route Tables → Security Groups
7. VPC itself
8. CloudFormation stacks matching the cluster name

## How the 1.5-Hour Idle Check Works (all free)

| Check | Logic |
|-------|-------|
| **User pods** | Any non-system pod `Running`/`Pending` → in use |
| **Container starts** | Any container `startedAt` within 90 min → in use |
| **Events** | Any `Scheduled`/`Started`/`Pulling`/`Created` event in user NS within 90 min → in use |
| **Node CPU** | `kubectl top nodes` — any node above threshold → in use. Gracefully skipped if metrics-server not installed |
| **Services** | Any `LoadBalancer`/`NodePort` service in user NS → in use |
| **Deployments** | Any deployment `lastUpdateTime` within 90 min → in use |
| **Node launches** | EC2 `LaunchTime` within 90 min → in use (scaling activity) |
| **Fail-safe** | If `kubectl` is unavailable or any API fails → assumes cluster **is in use** and aborts |
