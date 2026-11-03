# Security policy

Armadok drives the Docker daemon, which is equivalent to root access on the machine it runs on. Security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue.**

Use GitHub's private reporting: go to the **Security** tab of this repository and choose **Report a vulnerability**
(direct link: <https://github.com/Dr-J-Watson/armadok/security/advisories/new>). Only the maintainer can see the report.

Please include, as far as you can:
- what is affected (component, version or commit);
- steps to reproduce, or a proof of concept;
- the impact you see (what an attacker gains, and what they need first);
- any suggested fix.

Do not include real secrets, real domains or personal data in the report.

## What to expect

- The maintainer aims to acknowledge a report within **7 days** and to keep you informed of progress.
- Confirmed issues are fixed in a patch release, announced through a GitHub security advisory, with credit to the reporter unless you prefer to stay anonymous.
- Please give a reasonable time to fix the issue before any public disclosure (coordinated disclosure).
- There is no bug bounty.

## Supported versions

Armadok is not released yet. Until the first stable release (1.0), only the latest release (or the `main` branch when there is none) receives security fixes. After 1.0, this table will list the supported lines.

| Version | Supported |
|---|---|
| latest release / `main` | yes |
| anything older | no |

## Scope

In scope: the Armadok code in this repository and the official container image (`ghcr.io/dr-j-watson/armadok`).

Out of scope:
- a deployment that exposes the interface or the Docker socket publicly without access control (the documentation asks you to restrict access and enable two-factor authentication);
- vulnerabilities in third-party software (Docker, Traefik, your DNS provider): report them upstream. If Armadok is affected in a way that needs a change here, report it as above.

## Dependencies

Dependencies are monitored automatically (Dependabot alerts, image scanning). Armadok will also warn its administrator when a known vulnerability affects one of its own components.
