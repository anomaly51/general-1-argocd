# Vault demo: от значения до Pod

Секретное значение не хранится в Git. Администратор один раз записывает его в
Vault:

```sh
vault kv put kv/apps/vault-demo message='Hello from Vault'
```

В Git для интеграции остался только этот блок в [`values.yaml`](values.yaml):

```yaml
vaultSecrets:
  enabled: true
  secrets:
    - name: vault-demo
      refreshAfter: 10s
```

Общий chart
[`shared/vault-secret`](../../shared/vault-secret/README.md) вычисляет namespace
из Argo CD destination и создаёт три Kubernetes-ресурса:

1. `ServiceAccount/vault-demo` — Kubernetes identity.
2. `VaultAuth/vault-demo` — вход через общую role `kubernetes-workloads`.
3. `VaultStaticSecret/vault-demo` — копирование
   `kv/apps/vault-demo` в `Secret/vault-demo`.

[`Deployment`](templates/deployment.yaml) знает только об обычном Kubernetes
Secret. Ключ `message` монтируется как файл `/www/index.html`:

```text
Vault kv/apps/vault-demo
          ↓ VSO
Kubernetes Secret/vault-demo
          ↓ volume
Pod /www/index.html
```

Посмотреть результат:

```sh
kubectl --kubeconfig ~/.kube/configs/general-1-k3s.yaml \
  -n apps port-forward deployment/vault-demo 8080:8080
```

Открой <http://127.0.0.1:8080/>. Это учебное приложение специально показывает
значение по HTTP; настоящие пароли так использовать нельзя.
