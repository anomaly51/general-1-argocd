# Vault demo: откуда берётся secret

Здесь нет скрытой магии. Значение впервые попадает в Vault только в момент
выполнения этой команды администратором:

```sh
vault kv put kv/apps/vault-demo message='Hello from Vault'
```

Эта команда **не выполняется Argo CD** и значение не хранится в Git. Она
создаёт KV v2 secret по пути `kv/apps/vault-demo` с ключом `message`.

Адрес Vault также не спрятан в demo: глобальный `VaultConnection/default`
создаётся настройкой `defaultVaultConnection` в
[`../../infrastructure/vault-system/vault-secrets-operator/values.yaml`](../../infrastructure/vault-system/vault-secrets-operator/values.yaml)
и указывает на `http://vault.vault-system.svc.cluster.local:8200`.

## Как значение доходит до приложения

Весь Kubernetes-код находится в одном файле
[`templates/demo.yaml`](templates/demo.yaml) и читается сверху вниз:

1. `ServiceAccount/vso-vault-demo` — identity для входа в Vault.
2. `VaultAuth/vault-demo` — VSO входит в Vault через role
   `apps-vault-demo`.
3. `VaultStaticSecret/vault-demo` — VSO читает `kv/apps/vault-demo` и создаёт
   обычный Kubernetes `Secret/vault-demo`.
4. `Deployment/vault-demo` берёт из него ключ `message`, монтирует его как
   `/www/index.html`, а BusyBox `httpd` показывает этот файл.

```text
vault kv put
     ↓
Vault: kv/apps/vault-demo
     ↓  VaultStaticSecret
Kubernetes: Secret/vault-demo
     ↓  Secret volume
Pod: /www/index.html
```

Vault role уже настроена отдельно и разрешает этой ServiceAccount читать
только данный путь. Её policy находится в
[`../../vault-config/policies/apps-vault-demo.hcl`](../../vault-config/policies/apps-vault-demo.hcl).

## Посмотреть результат

```sh
kubectl --kubeconfig ~/.kube/configs/general-1-k3s.yaml \
  -n apps port-forward deployment/vault-demo 8080:8080
```

Открой <http://127.0.0.1:8080/>.

Измени значение той же командой `vault kv put`. VSO проверяет Vault каждые
10 секунд, затем Kubernetes обновляет файл в Pod.

Это учебное приложение намеренно показывает значение по HTTP. Настоящие
пароли так показывать нельзя.
