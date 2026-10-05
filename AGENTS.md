# Codex Instructions

- Use `connect-cluster`; it provides the available cluster URLs.
- Deploy all cluster changes through Argo CD using GitOps. Do not deploy resources manually.
- After every repository change, commit and push it. Verify the resulting state through Argo CD/GitOps before considering the work complete.
- Store no application credentials in macOS Keychain. Only the primary Vault login may be kept there; keep every other secret in Vault.
