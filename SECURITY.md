# Security Policy

## Supported versions

Security fixes are applied to the current `main` branch and the latest published
release. Older releases may require upgrading before a fix can be provided.

| Version | Supported |
| --- | --- |
| Current `main` / latest release | Yes |
| Older releases | No |

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
**Security > Report a vulnerability** form for this repository so the report,
proof of concept, and any sensitive details remain private.

If that form is not visible, open only a minimal public issue asking the
maintainer to establish a private contact channel. Do not include vulnerability
details, logs, recordings, credentials, or a proof of concept in that issue.

Include, when possible:

- the affected version or commit;
- operating system and Python version;
- a minimal reproduction or source-to-sink explanation;
- the expected impact and prerequisites;
- whether the issue is already public.

The maintainer aims to acknowledge a report within 7 days and provide an initial
triage result within 14 days. These are response targets, not a guaranteed SLA.
Coordinated disclosure timing will be agreed with the reporter after impact and
remediation are understood.

## Supply-chain scope

Reports about dependency confusion, compromised or mutable model artifacts,
unsafe installation instructions, build/release provenance, and CI workflow
integrity are in scope. A dependency advisory is not automatically exploitable
in ZenWhisper; reports should identify the affected installation profile and
reachable behavior where possible.

For the repository's dependency and model update controls, see
[`security/README.md`](security/README.md).
