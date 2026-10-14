# Third-party dependency notes

LAP's own source code and documentation are licensed under Apache-2.0; see
[LICENSE](LICENSE). Dependency declarations and lockfiles do not change that
license. The public source tree does not vendor Python or Go packages, Terraform
provider binaries, Helm charts, or container base images. Each upstream work
retains its own license.

These dependencies merit particular attention when distributing built artifacts:

| Component | Use | Upstream license |
| --- | --- | --- |
| [`psycopg` and `psycopg-binary` 3.2.10](https://github.com/psycopg/psycopg/blob/3.2.10/LICENSE.txt) | API runtime; the binary wheel also bundles native libraries | LGPL-3.0 |
| [`certifi` 2026.7.22](https://github.com/certifi/python-certifi/blob/2026.07.22/LICENSE) | Python trust-store dependency | MPL-2.0 (CA bundle) |
| [`windsorcli/hyperv` 0.3.1](https://github.com/windsorcli/terraform-provider-hyperv/blob/v0.3.1/LICENSE) | Terraform provider downloaded separately | MPL-2.0 |

Before distributing LAP container images or binaries, inventory the exact
contents of those artifacts, including base-image packages, bundled native
libraries, and linked Go modules. Preserve applicable copyright, license, and
NOTICE material. For images containing `psycopg` or `psycopg-binary`, provide
the LGPL and GPL license texts and satisfy the LGPL source and replacement or
relinking terms as applicable. This source-repository note is not a complete
license bundle for a built artifact.
