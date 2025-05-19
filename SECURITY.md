# 🛡 Security Policy

## Supported Versions

| Version  | Supported            |
| -------- | -------------------- |
| `main`   | :white\_check\_mark: |
| `v1.x`   | :white\_check\_mark: |
| `< v1.0` | :x:                  |

The `main` and `v1.x` branches receive security updates. Older versions are unsupported.

---

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please follow these steps:

1. **Do NOT create a public issue.** Instead, send an email to the security mailing list:

   ```
   security@Tomashevskyi-Vladyslav.dev
   ```
2. Include in your report:

   * A clear description of the vulnerability.
   * Steps to reproduce the issue.
   * Impact assessment (e.g., data exposure, denial of service).
   * Suggested remediation, if known.
3. The security team will confirm receipt within **48 hours** and work with you on a fix.

---

## Security Updates Process

* Once a vulnerability is confirmed, a fix will be backported to all supported versions.
* A coordinated disclosure will occur:

  1. **Private** patch release with security fix.
  2. **Public** release and advisory after downstream projects have had time to update.
* Security advisories will be published in the [GitHub Security Advisory](https://github.com/your-org/website-contract-monitor/security/advisories).

---

## Dependencies

We regularly audit our dependencies for known vulnerabilities. To update:

```bash
pip install --upgrade -r requirements.txt
```

Check for outdated packages with:

```bash
pip list --outdated
```

---

## Best Practices

* **Secret Management:** Do not commit API keys, tokens, or credentials to the repository.
* **Environment Isolation:** Run the bot in a dedicated service account or container with the least privileges.
* **Network Controls:** Restrict RPC endpoints and Telegram API access via firewall rules.
* **Monitoring & Logging:** Review logs for unexpected errors and monitor for unusual bot behavior.

---
