# Supply Chain Security & SLSA Compliance

## Current State
- SBOM generation: Syft in CI (CycloneDX JSON, weekly)
- Container scanning: Trivy (critical/high fail)
- Dependency auditing: pip-audit + npm audit (weekly + PR)
- Image provenance: Docker BuildKit provenance records

## Target: SLSA Level 2

### Build Requirements
- [x] Build as code (Dockerfile + GitHub Actions)
- [x] Build service (GitHub Actions hosted runners)
- [ ] Build metadata collected (provenance attestations)
- [ ] Signed provenance (cosign)

### Steps to SLSA 2

1. **Enable provenance attestations in Docker build:**
   ```yaml
   # Already configured in docker/build-push-action@v5
   - uses: docker/build-push-action@v5
     with:
       provenance: true
       sbom: true
   ```

2. **Sign images with cosign:**
   ```bash
   cosign sign --yes ghcr.io/org/workticket/backend:${{ github.sha }}
   ```

3. **Generate signed provenance:**
   Generate SLSA provenance using the slsa-github-generator:
   ```yaml
   - uses: slsa-framework/slsa-github-generator/.github/workflows/generator_generic_slsa3.yml@v2
   ```

4. **Store signatures and provenance in OCI registry:**
   ```bash
   cosign attach attestation --predicate provenance.json ghcr.io/org/workticket/backend:sha-${{ github.sha }}
   ```

## Image Promotion Pipeline

```
┌─────────┐    ┌──────────┐    ┌────────────┐    ┌──────────┐
│  Build   │───>│  Scan    │───>│   Sign +   │───>│  Deploy  │
│  + SBOM  │    │  (Trivy) │    │  Attest    │    │  (GitOps)│
└─────────┘    └──────────┘    └────────────┘    └──────────┘
```

### Admission Control
- Kyverno ClusterPolicy verifies cosign signature on all deployments
- Images without valid signature are rejected
- Images with CRITICAL vulnerabilities are blocked

### SBOM Distribution
- SBOMs stored alongside images in GHCR
- Weekly dependency audit generates aggregated SBOM
- SBOMs available for customer compliance requests

## Key Tools
- **Cosign**: Image signing and verification
- **Syft**: SBOM generation
- **Grype**: Vulnerability scanning (used by Trivy)
- **SLSA Framework**: Build provenance
- **Kyverno**: Admission control for verified images
