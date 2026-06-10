# Security Policy

## Reporting a Vulnerability

**Do not open a GitHub issue for security vulnerabilities.**

Please report security issues via:

- **Email**: security@workticket.app
- **PGP Key**: [Download PGP key](https://workticket.app/.well-known/security.txt)
- **Bug Bounty**: [https://hackerone.com/workticket](https://hackerone.com/workticket)

### Response SLA

| Severity | Initial Response | Status Update | Patch |
|----------|-----------------|---------------|-------|
| Critical | 2 hours | Every 4 hours | 12 hours |
| High | 4 hours | Every 8 hours | 48 hours |
| Medium | 24 hours | Every 48 hours | 7 days |
| Low | 48 hours | Weekly | Next release |

### Scope

- `*.workticket.app` (web applications and APIs)
- `api.workticket.app` (backend API)
- `dashboard.workticket.app` (web dashboard)
- Mobile applications (iOS / Android)
- Published Docker images (`ghcr.io/workticket/*`)
- Published npm packages (`@workticket/*`)
- Published Python packages

### Out of Scope

- Denial of Service (DoS/DDoS) attacks
- Social engineering attacks
- Physical security issues
- Self-XSS
- Missing security headers without demonstrated impact
- Issues without a realistic attack scenario

### Safe Harbor

We follow safe harbor practices. Researchers acting in good faith will not face legal action.

1. Do not exfiltrate, modify, or destroy data
2. Do not degrade or deny services
3. Do not publicly disclose before a fix is deployed
4. Do not attempt to access user accounts or data belonging to others
5. Halt testing immediately if you discover user PII

## Vulnerability Disclosure Program

This VDP is aligned with the [CISA VDP guidelines](https://www.cisa.gov/).

### Program Manager
- **Email**: security@workticket.app
- **PGP Fingerprint**: Check `/.well-known/security.txt` on `workticket.app`
- **Encryption**: Always use PGP when submitting sensitive vulnerability details

### Reporting Guidelines

Include the following in your report:
1. Type of vulnerability
2. Affected component and version
3. Step-by-step reproduction
4. Proof of concept (code, screenshots, video)
5. Potential impact
6. Any suggested fixes

### Disclosure Timeline
- Day 0: Report received
- Day 1: Acknowledgement sent
- Day 5: Triage completed
- Day 15: Fix implemented (critical/high)
- Day 30: Public disclosure (coordinated with researcher)
- Day 90: Public disclosure without researcher coordination (if researcher is unresponsive)

## Security Acknowledgments

We maintain a [Security Hall of Fame](https://workticket.app/security/thanks) to recognize researchers who have responsibly disclosed vulnerabilities.

## Secure Development

| Practice | Status |
|----------|--------|
| SAST (Bandit + Semgrep) | Active |
| Secret Scanning (Gitleaks) | Active |
| Container Scanning (Trivy) | Active |
| Dependency Audit (pip-audit, npm audit) | Active |
| SBOM (CycloneDX + SPDX) | Active |
| Image Signing (Cosign) | Active |
| SLSA Provenance | Level 2 |
| Fuzz Testing (SQL Injection + Migrations) | Active |
| Adversarial Testing (Prompt Injection) | Active |
| Secret Rotation (Automated) | Active |
