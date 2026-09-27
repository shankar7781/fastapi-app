# Kubernetes manifests

Dev/staging-oriented manifests for the QA API. Apply in this order (Namespace and Secret/ConfigMap must exist before things that reference them):

```bash
kubectl apply -f namespace.yaml
kubectl apply -f configmap.yaml

cp secret.yaml.example secret.yaml   # then edit secret.yaml with real values
kubectl apply -f secret.yaml

kubectl apply -f postgres.yaml
kubectl apply -f redis.yaml
kubectl apply -f app-deployment.yaml
kubectl apply -f app-service.yaml
kubectl apply -f hpa.yaml
kubectl apply -f ingress.yaml   # only if you have an ingress controller set up
```

Before applying `app-deployment.yaml`, build and push your app image, then update the `image:` field to point at it (`qa-api:latest` is a placeholder).

**Production note:** `postgres.yaml` and `redis.yaml` run those services *inside* the cluster, which is fine for testing the whole stack end-to-end, but the README's migration section recommends managed services (RDS, ElastiCache, etc.) for real production — swap `DATABASE_URL`/`REDIS_URL` in `configmap.yaml` to point at those instead, and you can drop `postgres.yaml`/`redis.yaml` entirely.
