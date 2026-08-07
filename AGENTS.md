# Codex Instructions

- Use `connect-cluster`; it provides the available cluster URLs.
- Deploy all cluster changes through Argo CD using GitOps. Do not deploy resources manually.
- Store no application credentials in macOS Keychain. Only the primary Vault login may be kept there; keep every other secret in Vault.
- After creating or changing DNS records, refresh macOS DNS before validating the affected hostnames: run `networksetup -setdnsservers Wi-Fi 1.1.1.1 8.8.8.8` and then `dscacheutil -flushcache`.
