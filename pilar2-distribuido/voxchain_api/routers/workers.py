import json
import os
import httpx
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header

from voxchain_api.config import config
from voxchain_api.models import (
    PoolHealth,
    PoolPolicy,
    WorkerStatus,
    WorkerSwitchRequest,
    RegisterWorkerRequest,
)
from voxchain_api.services.redis_reader import RedisReader
from voxchain_api.services.rabbitmq_publisher import RabbitMQPublisher
from common.identity import verify
from datetime import datetime, timezone

router = APIRouter(prefix="/api/workers", tags=["workers"])

logger = logging.getLogger("voxchain.api.workers")

# Kubernetes dynamic pod spawning configuration
K8S_ENABLED = False
NAMESPACE = "g-git-push-cv"

if os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/namespace"):
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r") as f:
            NAMESPACE = f.read().strip()
    except Exception as e:
        logger.warning(f"Failed to read Kubernetes namespace from secret: {e}")

try:
    from kubernetes import client, config as k8s_config
    
    # 1. Prioritize custom Kubeconfig env var (for remote cluster connection)
    kubeconfig_env = os.getenv("KUBECONFIG")
    if kubeconfig_env and os.path.exists(kubeconfig_env):
        try:
            k8s_config.load_kube_config(config_file=kubeconfig_env)
            K8S_ENABLED = True
            logger.info(f"Loaded remote Kubernetes config from KUBECONFIG={kubeconfig_env}. Namespace: {NAMESPACE}")
        except Exception as e:
            logger.error(f"Failed to load remote Kubernetes config from {kubeconfig_env}: {e}")

    # 2. Fallback to in-cluster context
    if not K8S_ENABLED:
        try:
            k8s_config.load_incluster_config()
            K8S_ENABLED = True
            logger.info(f"Loaded in-cluster Kubernetes config. Namespace: {NAMESPACE}")
        except Exception:
            pass

    # 3. Fallback to local default kubeconfig
    if not K8S_ENABLED:
        try:
            k8s_config.load_kube_config()
            K8S_ENABLED = True
            logger.info(f"Loaded default local kube config. Namespace: {NAMESPACE}")
        except Exception:
            logger.warning("No Kubernetes config found. Dynamic Pod spawning is disabled.")
except ImportError:
    logger.warning("kubernetes python package not installed. Dynamic Pod spawning is disabled.")


def _spawn_k8s_worker(worker_id: str, private_key_pem: str):
    if not K8S_ENABLED:
        logger.info("Kubernetes is not enabled, skipping dynamic spawner.")
        return

    # 1. Create Secret for the worker private key
    secret_name = f"secret-{worker_id}"
    secret_body = client.V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=client.V1ObjectMeta(name=secret_name, namespace=NAMESPACE),
        string_data={"private-key.pem": private_key_pem.strip()}
    )
    
    # 2. Create Deployment for the worker pod
    deployment_name = f"worker-dep-{worker_id}"
    
    container = client.V1Container(
        name="gpu-miner",
        image="southamerica-east1-docker.pkg.dev/voxchain/voxchain-images/worker-gpu:latest",
        image_pull_policy="Always",
        env=[
            client.V1EnvVar(name="WORKER_ID", value=worker_id),
            client.V1EnvVar(name="WORKER_MODE", value="standalone"),
            client.V1EnvVar(name="WORKER_PRIVKEY_PEM", value="/app/keys/private-key.pem"),
            client.V1EnvVar(
                name="RABBITMQ_USER",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(name="rabbitmq-credentials", key="username")
                )
            ),
            client.V1EnvVar(
                name="RABBITMQ_PASS",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(name="rabbitmq-credentials", key="password")
                )
            ),
            client.V1EnvVar(
                name="RABBITMQ_HOST",
                value_from=client.V1EnvVarSource(
                    config_map_key_ref=client.V1ConfigMapKeySelector(name="worker-config", key="rabbitmq-host")
                )
            ),
            client.V1EnvVar(name="RABBITMQ_URL", value="amqp://$(RABBITMQ_USER):$(RABBITMQ_PASS)@$(RABBITMQ_HOST):5672/"),
            client.V1EnvVar(
                name="REDIS_HOST",
                value_from=client.V1EnvVarSource(
                    config_map_key_ref=client.V1ConfigMapKeySelector(name="worker-config", key="redis-host", optional=True)
                )
            ),
            client.V1EnvVar(name="REDIS_URL", value="redis://$(REDIS_HOST):6379/0"),
            client.V1EnvVar(name="WORKER_CAPACITY", value="1"),
            client.V1EnvVar(name="LOG_DIR", value="/var/log/voxchain"),
        ],
        resources=client.V1ResourceRequirements(
            limits={"nvidia.com/gpu": "1"}
        ),
        volume_mounts=[
            client.V1VolumeMount(name="key-volume", mount_path="/app/keys", read_only=True)
        ]
    )
    
    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"app": f"worker-gpu-{worker_id}"}),
        spec=client.V1PodSpec(
            termination_grace_period_seconds=10,
            containers=[container],
            volumes=[
                client.V1Volume(
                    name="key-volume",
                    secret=client.V1SecretVolumeSource(secret_name=secret_name)
                )
            ]
        )
    )
    
    spec = client.V1DeploymentSpec(
        replicas=1,
        selector=client.V1LabelSelector(match_labels={"app": f"worker-gpu-{worker_id}"}),
        template=template
    )
    
    deployment_body = client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=deployment_name, namespace=NAMESPACE),
        spec=spec
    )
    
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    
    try:
        logger.info(f"Creating K8s Secret {secret_name} in namespace {NAMESPACE}...")
        core_api.create_namespaced_secret(namespace=NAMESPACE, body=secret_body)
        
        logger.info(f"Creating K8s Deployment {deployment_name} in namespace {NAMESPACE}...")
        apps_api.create_namespaced_deployment(namespace=NAMESPACE, body=deployment_body)
    except Exception as e:
        logger.error(f"Failed to create K8s resources for worker {worker_id}: {e}")
        try:
            core_api.delete_namespaced_secret(name=secret_name, namespace=NAMESPACE)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to spawn Kubernetes worker node: {e}")


def _delete_k8s_worker(worker_id: str):
    if not K8S_ENABLED:
        return
        
    secret_name = f"secret-{worker_id}"
    deployment_name = f"worker-dep-{worker_id}"
    
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    
    try:
        logger.info(f"Deleting K8s Deployment {deployment_name} in namespace {NAMESPACE}...")
        apps_api.delete_namespaced_deployment(name=deployment_name, namespace=NAMESPACE)
    except Exception as e:
        logger.warning(f"Failed to delete Deployment {deployment_name}: {e}")
        
    try:
        logger.info(f"Deleting K8s Secret {secret_name} in namespace {NAMESPACE}...")
        core_api.delete_namespaced_secret(name=secret_name, namespace=NAMESPACE)
    except Exception as e:
        logger.warning(f"Failed to delete Secret {secret_name}: {e}")


# Worker admin URLs - these should be configured via environment variables
# Use Docker service names when running in docker-compose
WORKER_ADMIN_URLS = [
    "http://worker-pool-coordinator:9090",
    "http://worker-1:9090",
    "http://worker-2:9090",
]

# Mapping from worker_id to Docker service name for pool coordinators
POOL_COORDINATOR_MAPPING = {
    "pool-coordinator-1": "worker-pool-coordinator",
    "worker-2": "worker-2",  # worker-2 can also become a pool coordinator
}

# Mapping from owner_id to their owned workers
# Updated for the demo scenario where each user corresponds to a node in k3s-cluster
OWNER_WORKERS_MAPPING = {
    "default": ["worker-1", "worker-2", "pool-coordinator-1"],  # For local dev
    "valentin": ["worker-standalone"],
    "gustavo": ["worker-pool-coordinator"],
    "matt": ["worker-pool-miner-1"],
    "profesor1": ["worker-pool-miner-2"],
    "profesor2": ["worker-pool-miner-3"],
}

# Combined list of all registered worker IDs
ALL_REGISTERED_WORKER_IDS = [
    "worker-1", "worker-2", "pool-coordinator-1",
    "worker-standalone", "worker-pool-coordinator",
    "worker-pool-miner-1", "worker-pool-miner-2", "worker-pool-miner-3"
]


def get_owner_id(owner_id: str = Header(None, alias="X-Owner-Id")) -> str:
    """Get the owner ID from the header, default to 'default' for local dev."""
    if owner_id is None:
        return "default"
    return owner_id


def verify_worker_ownership(worker_id: str, owner_id: str) -> bool:
    """Verify that the owner has permission to modify the worker."""
    # 1. Check hardcoded/demo mappings
    owned_workers = OWNER_WORKERS_MAPPING.get(owner_id, [])
    if worker_id in owned_workers:
        return True

    # 2. Check dynamic Redis mappings
    try:
        redis = RedisReader()
        registered_owner = redis.store.r.get(f"worker:owner:{worker_id}")
        if registered_owner:
            registered_owner_str = registered_owner.decode("utf-8") if isinstance(registered_owner, bytes) else registered_owner
            if registered_owner_str == owner_id:
                return True
    except Exception:
        pass

    raise HTTPException(status_code=403, detail="You do not have permission to modify this worker")


def get_redis_reader() -> RedisReader:
    return RedisReader()


def get_rabbitmq_publisher() -> RabbitMQPublisher:
    return RabbitMQPublisher()


@router.get("/status", response_model=list[WorkerStatus])
async def get_all_workers_status(redis: RedisReader = Depends(get_redis_reader)):
    """Get status of all registered workers."""
    statuses = []
    redis_client = redis.store.r

    # Dynamic worker discovery via Redis keys matching worker:status:*
    discovered_worker_ids = set()
    try:
        # scan_iter is non-blocking (non-blocking alternative to KEYS)
        for key in redis_client.scan_iter("worker:status:*"):
            key_str = key.decode("utf-8") if isinstance(key, bytes) else key
            parts = key_str.split("worker:status:")
            if len(parts) > 1:
                discovered_worker_ids.add(parts[1])
    except Exception:
        pass

    # Registered workers set from Redis
    registered_worker_ids = set()
    try:
        members = redis_client.smembers("registered_workers")
        for member in members:
            member_str = member.decode("utf-8") if isinstance(member, bytes) else member
            registered_worker_ids.add(member_str)
    except Exception:
        pass

    # Union of all worker IDs: preconfigured + discovered + registered
    all_worker_ids = set(ALL_REGISTERED_WORKER_IDS) | discovered_worker_ids | registered_worker_ids

    for worker_id in all_worker_ids:
        # 1. Try to read from Redis
        try:
            status_data = redis_client.get(f"worker:status:{worker_id}")
            if status_data:
                data = json.loads(status_data)
                # Map dynamic pubkey if stored in Redis but not reported in status
                if "pubkey" not in data or not data["pubkey"]:
                    pk = redis_client.get(f"worker:pubkey:{worker_id}")
                    if pk:
                        data["pubkey"] = pk.decode("utf-8") if isinstance(pk, bytes) else pk
                statuses.append(WorkerStatus(**data))
                continue
        except Exception:
            pass

        # 2. Fallback to HTTP (for local development/backwards compatibility)
        local_url_mapping = {
            "worker-1": "http://worker-1:9090",
            "worker-2": "http://worker-2:9090",
            "pool-coordinator-1": "http://worker-pool-coordinator:9090"
        }
        base_url = local_url_mapping.get(worker_id)
        if base_url:
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(f"{base_url}/status", timeout=1.0)
                    if response.status_code == 200:
                        data = response.json()
                        statuses.append(WorkerStatus(**data))
                        continue
            except Exception:
                pass

        # 3. If stopped but pre-configured or registered, show as stopped
        stored_pubkey = None
        try:
            pk = redis_client.get(f"worker:pubkey:{worker_id}")
            if pk:
                stored_pubkey = pk.decode("utf-8") if isinstance(pk, bytes) else pk
        except Exception:
            pass

        if worker_id in ALL_REGISTERED_WORKER_IDS or worker_id in registered_worker_ids:
            statuses.append(
                WorkerStatus(
                    worker_id=worker_id,
                    mode="unknown",
                    running=False,
                    pubkey=stored_pubkey
                )
            )

    return statuses


@router.get("/{worker_id}/status", response_model=WorkerStatus)
async def get_worker_status(worker_id: str, redis: RedisReader = Depends(get_redis_reader)):
    """Get status of a specific worker by ID."""
    redis_client = redis.store.r

    # 1. Try to read from Redis
    try:
        status_data = redis_client.get(f"worker:status:{worker_id}")
        if status_data:
            data = json.loads(status_data)
            if "pubkey" not in data or not data["pubkey"]:
                pk = redis_client.get(f"worker:pubkey:{worker_id}")
                if pk:
                    data["pubkey"] = pk.decode("utf-8") if isinstance(pk, bytes) else pk
            return WorkerStatus(**data)
    except Exception:
        pass

    # 2. Fallback to HTTP
    local_url_mapping = {
        "worker-1": "http://worker-1:9090",
        "worker-2": "http://worker-2:9090",
        "pool-coordinator-1": "http://worker-pool-coordinator:9090"
    }
    base_url = local_url_mapping.get(worker_id)
    if base_url:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{base_url}/status", timeout=1.0)
                if response.status_code == 200:
                    data = response.json()
                    return WorkerStatus(**data)
        except Exception:
            pass

    # 3. Check if registered in Redis
    try:
        is_member = redis_client.sismember("registered_workers", worker_id)
        if is_member or worker_id in ALL_REGISTERED_WORKER_IDS:
            pk = redis_client.get(f"worker:pubkey:{worker_id}")
            stored_pubkey = pk.decode("utf-8") if isinstance(pk, bytes) else pk if pk else None
            return WorkerStatus(
                worker_id=worker_id,
                mode="unknown",
                running=False,
                pubkey=stored_pubkey
            )
    except Exception:
        pass

    raise HTTPException(status_code=404, detail="Worker not found")


@router.post("/{worker_id}/switch-mode", response_model=dict)
async def switch_worker_mode(
    worker_id: str, 
    request: WorkerSwitchRequest, 
    owner_id: str = Depends(get_owner_id),
    redis: RedisReader = Depends(get_redis_reader),
    publisher: RabbitMQPublisher = Depends(get_rabbitmq_publisher)
):
    """Switch a worker to a different mode (standalone, pool-coordinator, pool-worker)."""
    verify_worker_ownership(worker_id, owner_id)

    # 1. Publish command to RabbitMQ to notify the worker of the change
    try:
        cmd = {
            "type": "switch_mode",
            "mode": request.target,
            "pool_url": request.pool_url or ""
        }
        publisher.messaging.publish_worker_command(worker_id, cmd)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to publish switch-mode command to RabbitMQ: {e}")
    finally:
        publisher.close()

    # 2. Update expected status in Redis for immediate UI response
    redis_client = redis.store.r
    try:
        status_data = redis_client.get(f"worker:status:{worker_id}")
        status = json.loads(status_data) if status_data else {}
        status["mode"] = request.target
        status["worker_id"] = worker_id
        status["running"] = True
        if request.target == "pool-worker":
            status["pool_url"] = request.pool_url
        elif request.target == "pool-coordinator":
            status["pool_url"] = f"http://{worker_id}:9001"
        else:
            status["pool_url"] = ""
        redis_client.set(f"worker:status:{worker_id}", json.dumps(status), ex=15)
    except Exception:
        pass

    # 3. Fallback/Dual invocation via HTTP for local dev
    local_url_mapping = {
        "worker-1": "http://worker-1:9090",
        "worker-2": "http://worker-2:9090",
        "pool-coordinator-1": "http://worker-pool-coordinator:9090"
    }
    base_url = local_url_mapping.get(worker_id)
    if base_url:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{base_url}/switch-mode",
                    json=request.model_dump(),
                    timeout=2.0,
                )
        except Exception:
            pass

    return {"ok": True, "mode": request.target}


@router.get("/pool/{pool_id}/health", response_model=PoolHealth)
async def get_pool_health(pool_id: str, redis: RedisReader = Depends(get_redis_reader)):
    """Get health status of a specific pool coordinator."""
    redis_client = redis.store.r

    # 1. Try to read from Redis
    try:
        health_data = redis_client.get(f"pool:health:{pool_id}")
        if health_data:
            data = json.loads(health_data)
            return PoolHealth(**data)
    except Exception:
        pass

    # 2. Fallback to HTTP
    service_name = POOL_COORDINATOR_MAPPING.get(pool_id, pool_id)
    pool_url = f"http://{service_name}:9001"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{pool_url}/health", timeout=2.0)
            if response.status_code == 200:
                return PoolHealth(**response.json())
    except Exception:
        pass
    raise HTTPException(status_code=404, detail="Pool not found")


@router.post("/pool/{pool_id}/policy")
async def set_pool_policy(
    pool_id: str, 
    policy: PoolPolicy, 
    owner_id: str = Depends(get_owner_id),
    redis: RedisReader = Depends(get_redis_reader)
):
    """Set voting policy for a specific pool coordinator."""
    verify_worker_ownership(pool_id, owner_id)

    # 1. Write the policy to Redis so the remote coordinator can read it periodically
    redis_client = redis.store.r
    try:
        redis_client.set(f"pool:policy:{pool_id}", json.dumps(policy.model_dump()))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save policy to Redis: {e}")

    # 2. Fallback to HTTP for local dev
    service_name = POOL_COORDINATOR_MAPPING.get(pool_id, pool_id)
    pool_url = f"http://{service_name}:9001"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{pool_url}/pool/policy",
                json=policy.model_dump(),
                timeout=2.0,
            )
            if response.status_code == 200:
                return response.json()
    except Exception:
        pass

    return {"ok": True, "policy": policy.model_dump()}


def _verify_timestamp_freshness(timestamp_str: str) -> None:
    try:
        ts = datetime.fromisoformat(timestamp_str).timestamp()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid timestamp format")
    
    now_ts = datetime.now(timezone.utc).timestamp()
    max_age = float(os.getenv("PROPOSAL_MAX_AGE_SECONDS", "300"))
    if abs(now_ts - ts) > max_age:
        raise HTTPException(status_code=400, detail="Timestamp expired or too far in the future")


@router.post("/register", response_model=dict)
async def register_worker(
    request: RegisterWorkerRequest,
    redis: RedisReader = Depends(get_redis_reader)
):
    """Register a new worker ID, mapping it to the citizen owner pubkey."""
    worker_id = request.worker_id.strip()
    pubkey = request.pubkey.strip()
    signature = request.signature.strip()
    timestamp = request.timestamp.strip()

    if not worker_id:
        raise HTTPException(status_code=400, detail="Worker ID cannot be empty")
    if not pubkey:
        raise HTTPException(status_code=400, detail="Public Key cannot be empty")

    # 1. Collision Protection: check against default and other owned workers
    if worker_id in ALL_REGISTERED_WORKER_IDS:
        raise HTTPException(status_code=409, detail="Worker ID is reserved for a demo/default worker")

    redis_client = redis.store.r
    try:
        existing_owner = redis_client.get(f"worker:owner:{worker_id}")
        if existing_owner:
            existing_owner_str = existing_owner.decode("utf-8") if isinstance(existing_owner, bytes) else existing_owner
            if existing_owner_str != pubkey:
                raise HTTPException(status_code=409, detail="Worker ID is already registered by another owner")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database check failed: {e}")

    # 2. Replay Protection: verify timestamp freshness
    _verify_timestamp_freshness(timestamp)

    # 3. Ownership Authentication: verify signature of `${worker_id}|register|${timestamp}`
    msg = f"{worker_id}|register|{timestamp}".encode()
    if not verify(pubkey, msg, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 4. Save to Redis
    try:
        redis_client.sadd("registered_workers", worker_id)
        redis_client.set(f"worker:owner:{worker_id}", pubkey)
        redis_client.set(f"worker:pubkey:{worker_id}", pubkey)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save registration: {e}")

    # 5. Dynamic Kubernetes worker deployment if private key uploaded
    if request.private_key:
        _spawn_k8s_worker(worker_id, request.private_key)

    return {"ok": True, "worker_id": worker_id, "pubkey": pubkey}


@router.delete("/{worker_id}", response_model=dict)
async def unregister_worker(
    worker_id: str,
    x_signature: str = Header(None, alias="X-Signature"),
    x_timestamp: str = Header(None, alias="X-Timestamp"),
    redis: RedisReader = Depends(get_redis_reader)
):
    """Unregister a worker ID (only if owned by current caller)."""
    if not x_signature or not x_timestamp:
        raise HTTPException(status_code=400, detail="Missing verification headers (X-Signature / X-Timestamp)")

    # 1. Collision/Demo check: reject deleting hardcoded demo workers
    if worker_id in ALL_REGISTERED_WORKER_IDS:
        raise HTTPException(status_code=403, detail="Cannot delete preconfigured demo workers")

    redis_client = redis.store.r

    # 2. Fetch owner public key. If not found, 404
    owner_pubkey_bytes = redis_client.get(f"worker:owner:{worker_id}")
    if not owner_pubkey_bytes:
        raise HTTPException(status_code=404, detail="Worker registration not found")
    owner_pubkey = owner_pubkey_bytes.decode("utf-8") if isinstance(owner_pubkey_bytes, bytes) else owner_pubkey_bytes

    # 3. Replay Protection: verify timestamp freshness
    _verify_timestamp_freshness(x_timestamp)

    # 4. Verify signature of `${worker_id}|delete|${timestamp}`
    msg = f"{worker_id}|delete|{x_timestamp}".encode()
    if not verify(owner_pubkey, msg, x_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 5. Delete metadata
    try:
        redis_client.srem("registered_workers", worker_id)
        redis_client.delete(f"worker:owner:{worker_id}")
        redis_client.delete(f"worker:pubkey:{worker_id}")
        redis_client.delete(f"worker:status:{worker_id}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to unregister worker: {e}")

    # 6. Delete dynamic Kubernetes resources
    _delete_k8s_worker(worker_id)

    return {"ok": True}
