---
name: error-handling-and-failure-modes
description: "Use when adding a call that can fail - file IO, subprocess, network or API calls, parsing, or external tools - to handle its failure paths, not only the happy path."
---
# Error Handling and Failure Modes

> **Every external call has a failure path; write it before you ship the happy path.**

## Use this when
- You are adding IO, a subprocess, a network/API request, deserialization, or any call that can fail or return junk.

## Do NOT use for
- Pure in-memory logic with no external boundary.

## Procedure

1. **List failure modes** for each external call (see table below).
2. **Classify each as retryable or fatal.**
3. **Decide the behavior:** retry with backoff, fall back, or surface a clear error.
4. **Never swallow errors silently.** No bare `except:`, no `pass` in except blocks.
5. **Preserve original cause** with `raise ... from exc`.
6. **Add a failure path test.**

### Step 1: List Failure Modes
For each external call, identify what can go wrong:

| Call type | Failure modes |
|-----------|--------------|
| File IO | not found, permission denied, disk full, encoding error |
| Network/API | timeout, connection refused, rate limit (429), server error (5xx), invalid JSON |
| Subprocess | non-zero exit, timeout, broken pipe, signal kill |
| Parsing/decode | malformed input, unexpected type, missing fields |
| Database | connection lost, constraint violation, deadlock |

### Step 2: Classify Each as Retryable or Fatal

```
Retryable (transient):
  → timeout, rate limit, connection refused, deadlock
  → Use exponential backoff with jitter

Fatal (won't fix itself):
  → not found, permission denied, malformed input, constraint violation
  → Surface immediately with a clear error message

Unknown:
  → Treat as fatal until proven otherwise
```

### Step 3: Decide the Behavior

```python
# Retryable: retry with backoff
import time
for attempt in range(3):
    try:
        return call_external_api()
    except (ConnectionError, TimeoutError) as e:
        if attempt == 2:
            raise  # Give up after 3 attempts
        time.sleep(2 ** attempt)  # Exponential backoff

# Fatal: surface with context
try:
    config = load_config(path)
except FileNotFoundError:
    raise ConfigError(f"Config file not found: {path}") from None
except json.JSONDecodeError as e:
    raise ConfigError(f"Invalid JSON in {path}: {e}") from e

# Fallback: degraded behavior
try:
    return get_from_cache(key)
except CacheError:
    return get_from_database(key)  # Cache miss, fallback to DB
```

### Step 4: Never Swallow Errors Silently

```python
# BAD
try:
    do_something()
except:
    pass

# BAD
try:
    do_something()
except Exception:
    logging.debug("error")  # Hides the problem

# GOOD
try:
    do_something()
except SpecificError as e:
    logger.error("Failed to do_something: %s", e, exc_info=True)
    raise  # Or handle explicitly
```

### Step 5: Preserve Original Cause

```python
# GOOD: chain exceptions
raise ValueError("Invalid config") from original_error

# GOOD: add context
try:
    result = parse_response(data)
except json.JSONDecodeError as e:
    raise ParseError(f"Failed to parse API response: {e}") from e
```

### Step 6: Add a Failure Path Test

```python
def test_api_timeout_returns_retryable_error():
    with mock.patch("httpx.Client.get", side_effect=httpx.TimeoutException):
        result = fetch_data("http://api.example.com/data")
        assert result.error_code == "TIMEOUT"
        assert result.retryable is True

def test_missing_file_raises_clear_error():
    with pytest.raises(ConfigError, match="Config file not found"):
        load_config("/nonexistent/path.json")
```

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "This will never fail" | Network calls fail. Files get deleted. APIs change. Disk fills up. |
| "I'll add error handling later" | Later means after the bug is in production. Write it now. |
| "try/except is too verbose" | Verbose errors are easier to debug than silent failures. |
| "Just catch Exception and move on" | Broad catches hide bugs. Catch specific exceptions. |
| "The caller will handle it" | If you know the failure mode, handle it at the source. |

## Red Flags
- Bare `except:` or `except Exception:` with no handling
- `pass` in an except block
- Error messages that don't include context (what failed, why)
- No tests for any failure path
- Catching exceptions just to re-raise a different type without logging
- Ignoring return codes from subprocesses

## Done when
- The unhappy paths are handled explicitly and at least one is tested.

## Verification
- [ ] Every external call has explicit error handling
- [ ] No bare `except:` or silent error swallowing
- [ ] Error messages include context (what, why, how to fix)
- [ ] At least one failure path is tested per external call
- [ ] Original exceptions are preserved (chained)
