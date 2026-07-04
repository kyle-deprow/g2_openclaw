---
title: Test Behavior, Not Implementation
impact: CRITICAL
impactDescription: resilient tests that survive refactors
tags: tdd, testing, behavior, black-box
---

## Test Behavior, Not Implementation

Tests should verify observable outcomes (return values, state changes, side
effects) — not how the code achieves them internally. Implementation-coupled
tests break on every refactor and provide no design feedback.

**Incorrect (testing internal method calls):**

```python
# BAD: Breaks if you rename _hash_credential or change hashing strategy
def test_register_hashes_credential(mocker):
    mock_hash = mocker.patch("spine_sense.auth._hash_credential")
    register_user(email="a@b.com", credential="example-credential")
    mock_hash.assert_called_once_with("example-credential")
```

**Correct (testing observable behavior):**

```python
def test_registered_user_can_authenticate(user_repo: FakeUserRepo) -> None:
    register_user(email="a@b.com", credential="example-credential", repo=user_repo)

    result = authenticate(email="a@b.com", credential="example-credential", repo=user_repo)

    assert result is not None
    assert result.email == "a@b.com"


def test_wrong_credential_rejects_authentication(user_repo: FakeUserRepo) -> None:
    register_user(email="a@b.com", credential="example-credential", repo=user_repo)

    result = authenticate(email="a@b.com", credential="wrong-credential", repo=user_repo)

    assert result is None
```

The password is hashed correctly if authentication works — no need to test the
hashing mechanism directly.
