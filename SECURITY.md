# Security Policy

## Supported versions

This project is pre-1.0 and ships from `main`. Security fixes are applied to the
latest `main`; there are no backports to older commits.

## Reporting a vulnerability

Please report suspected vulnerabilities privately, not through public issues or
pull requests.

- Use GitHub's private vulnerability reporting for this repository
  (**Security → Report a vulnerability**), which opens a confidential advisory
  with the maintainers.
- Include the affected version or commit, a description of the issue, and the
  minimal steps or proof-of-concept needed to reproduce it.

You can expect an initial acknowledgement within a few days. Once a fix is
available we will coordinate a disclosure timeline with you and credit your
report unless you prefer to remain anonymous.

## Scope and handling notes

- **Secrets** live only in environment variables (`LLM_API_KEY`, `API_KEY`, and
  the `DATABASE_URL`); `.env` is git-ignored and `.env.example` carries dummy
  values only. Never paste real keys into an issue or PR.
- **Ticket content is untrusted.** Subject and body are attacker-controlled and
  are treated as data to classify, never as instructions; every provider
  response is validated against a strict schema before it is stored. Reports of
  ways to bypass that validation or to smuggle ticket text into the
  classification instructions are in scope.
- The optional `API_KEY` gate is compared in constant time. Logs are structured
  JSON and deliberately exclude ticket content and secrets; a report showing
  either leaking into logs is in scope.
- Please do not run automated scanners against any hosted instance you do not
  own, and do not access, modify, or destroy data that is not yours.
