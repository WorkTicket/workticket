#!/bin/bash
set -euo pipefail

readonly PRIMARY_REGION="us-east-1"
readonly DR_REGION="us-west-2"
readonly PRIMARY_CLUSTER="workticket-prod"
readonly DR_CLUSTER="workticket-prod-dr"
readonly RDS_INSTANCE="workticket-prod-dr"
readonly NAMESPACE="workticket"
readonly DOMAIN="workticket.app"
readonly HOSTED_ZONE_ID="ZXXXXXXXXXXXXX"
readonly DR_ALB_DNS="workticket-dr.us-west-2.elb.amazonaws.com"

echo "[DR FAILOVER] $(date -u '+%Y-%m-%dT%H:%M:%SZ') - Starting failover to ${DR_REGION}"

echo "[1/7] Checking DR region health..."
if ! aws eks describe-cluster --name "${DR_CLUSTER}" --region "${DR_REGION}" --output text > /dev/null 2>&1; then
    echo "ERROR: DR cluster ${DR_CLUSTER} not found in ${DR_REGION}"
    exit 1
fi

echo "[2/7] Promoting RDS read replica in ${DR_REGION}..."
aws rds promote-read-replica \
    --db-instance-identifier "${RDS_INSTANCE}" \
    --region "${DR_REGION}"

echo "[3/7] Waiting for RDS promotion..."
aws rds wait db-instance-available \
    --db-instance-identifier "${RDS_INSTANCE}" \
    --region "${DR_REGION}"
sleep 30

echo "[4/7] Scaling DR EKS node group..."
aws eks update-nodegroup-config \
    --cluster-name "${DR_CLUSTER}" \
    --nodegroup-name "workticket-prod-dr-ondemand" \
    --scaling-config desiredSize=5,minSize=3,maxSize=15 \
    --region "${DR_REGION}"

echo "[5/7] Updating kubeconfig and scaling apps..."
aws eks update-kubeconfig --name "${DR_CLUSTER}" --region "${DR_REGION}"

kubectl scale deployment/workticket-api -n "${NAMESPACE}" --replicas=3
kubectl scale deployment/celery-worker-text -n "${NAMESPACE}" --replicas=3
kubectl scale deployment/celery-worker-image -n "${NAMESPACE}" --replicas=3
kubectl scale deployment/celery-worker-audio -n "${NAMESPACE}" --replicas=3
kubectl scale deployment/celery-worker-default -n "${NAMESPACE}" --replicas=2

echo "[6/7] Updating Route53 DNS to DR region..."
aws route53 change-resource-record-sets \
    --hosted-zone-id "${HOSTED_ZONE_ID}" \
    --change-batch '{
        "Changes": [
            {
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": "api.'"${DOMAIN}"'",
                    "Type": "CNAME",
                    "TTL": 60,
                    "ResourceRecords": [{"Value": "'"${DR_ALB_DNS}"'"}]
                }
            }
        ]
    }'

echo "[7/7] Verifying application health..."
aws eks update-kubeconfig --name "${DR_CLUSTER}" --region "${DR_REGION}"
SVC_IP=$(kubectl get svc workticket-api -n "${NAMESPACE}" -o jsonpath='{.spec.clusterIP}')
HEALTH_URL="http://${SVC_IP}:8000/healthz"

for i in $(seq 1 30); do
    if kubectl run health-check --rm -i --restart=Never --image=curlimages/curl:latest \
        -- curl -sf "${HEALTH_URL}" > /dev/null 2>&1; then
        echo "[DONE] $(date -u '+%Y-%m-%dT%H:%M:%SZ') - Failover to ${DR_REGION} complete. Application healthy."
        exit 0
    fi
    echo "Waiting for application... attempt ${i}/30"
    sleep 10
done

echo "WARNING: Health check failed after 5 minutes. Manual investigation required."
exit 1
