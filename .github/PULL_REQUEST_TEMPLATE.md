## Summary

What does this change do, and why? Link any related issue (`Closes #123`).

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor / internal change
- [ ] Documentation
- [ ] Migration / schema change

## Checklist

- [ ] `uv run ruff check .` passes
- [ ] `uv run black --check .` passes
- [ ] `uv run pytest` passes (provider stays mocked; no real provider calls)
- [ ] Behavior changes are covered by a test that fails before this change
- [ ] Commits are granular and use conventional, imperative messages (no emoji)
- [ ] No secrets committed; `.env.example` updated if a new setting was added

## Database migrations

- [ ] Not applicable, or
- [ ] Adds a new migration in `migrations/versions/` (applied migrations are not
      edited) and `app/models.py` is kept in sync
- [ ] `alembic upgrade head` then `downgrade -1` then `upgrade head` round-trips
      cleanly

## Notes for reviewers

Anything that needs extra attention: trade-offs, follow-ups, or areas you are
unsure about.
