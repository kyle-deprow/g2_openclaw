# Plan C — in-sandbox source provenance: recorded by containment, verified by the worker, gated before review

You are the implementation worker for the g2_openclaw repository (Python 3.13, uv, pytest, ruff, mypy strict).
You are NOT alone in the repository. Do NOT commit. Do NOT run `git add`, `git commit`, `git stash`, or touch git state.
Do NOT run `scripts/push-openclaw-config.sh`, `systemctl`, or any `gateway-cli` command against a real research root.
Do NOT modify any file outside the "Files you may change" list. Do NOT touch /home/dev/repos/quantipy.

## Background (verified facts, do not re-derive)

The research driver runs three kinds of contained Python stages through `gateway/research/containment.py::stage_plan` →
`bwrap_argv` (bubblewrap, `--clearenv`, `PYTHONPATH=/snapshot/src`, analysis stage `PYTHONPATH=/snapshot/src:/work`):
`targets-sNNN` (worktree at `/work`, writable `/stage`), `evaluate-sNNN` (writable `/stage`, evaluator console script), `analysis`
(worktree `/work`, `--dir /stage`, writable bind at `/stage/analysis`), plus `validate-cNNN` (read-only inputs, no writable mount).
`gateway/research/worker.py::_contained_run_plan` executes them for the historical run. Experiment authors reuse `stage_plan` from
their own synthetic integration harness before submitting an implementation.

The last Opus review FAILED because nothing proved, from inside the sandboxes, which module files were actually imported: host-side
hashes of the snapshot and worktree do not show what `PYTHONPATH=/snapshot/src` resolved to inside bwrap. Concretely, the shared
Quantipy venv (`/home/dev/repos/quantipy/.venv`, read-only-bound into every sandbox) carries an editable install of quantipy whose
meta-path finder points at `/home/dev/repos/quantipy/src`; on the host that finder wins over PYTHONPATH. Inside the sandbox that path
is not mounted, so PathFinder should fall back to `/snapshot/src` — but only an in-sandbox record proves it.

Verified prototype (Python 3.11 = the shared venv interpreter, and 3.13): a `sitecustomize.py` placed first on PYTHONPATH is imported
by `site` even under `python -P -s`, and an `atexit` hook in it runs on normal exit, `sys.exit(n)`, and for console-script entrypoints.
`sys.flags.safe_path` and `sys.flags.no_user_site` are both available on 3.11+. The recorder must be stdlib-only and 3.11-compatible.

## Deliverable

### 1. `gateway/research/containment_provenance/sitecustomize.py` (new; stdlib only; no imports from `gateway`)

- Module-level: read `RESEARCH_PROVENANCE_DIR` and `RESEARCH_PROVENANCE_STAGE` from `os.environ`. If either is missing or empty, do
  nothing (no atexit registration). Otherwise register one `atexit` function.
- The atexit function writes exactly one JSON file `f"{stage}-{os.getpid()}.json"` inside `RESEARCH_PROVENANCE_DIR`, created with
  `os.open(..., O_WRONLY|O_CREAT|O_EXCL, 0o644)`, `json.dump(..., sort_keys=True, separators=(",", ":"))`, `flush`, `fsync`. Content:
  ```
  {"contract": "research-containment-provenance-v1", "stage": <stage>, "pid": int, "ppid": int,
   "argv": sys.argv, "executable": sys.executable, "cwd": os.getcwd(), "python_version": sys.version.split()[0],
   "flags": {"safe_path": bool(getattr(sys.flags, "safe_path", False)), "no_user_site": bool(sys.flags.no_user_site)},
   "pythonpath": os.environ.get("PYTHONPATH"), "sys_path": list(sys.path),
   "recorder": {"path": os.path.realpath(__file__), "sha256": <sha256 of this file's bytes read at exit>},
   "modules": [ {"name": str, "path": str (realpath of module.__file__), "origin": "snapshot"|"work"|"recorder"|"runtime",
                 "sha256": str  # present for snapshot/work/recorder origins only (bytes read inside the sandbox)
                }, ... sorted by name ],
   "written_at": ISO-8601 UTC "Z"}
  ```
  Origin rule on the realpath: starts with `/snapshot/src/` → `snapshot`; `/work/` → `work`; `/provenance/` → `recorder`; else `runtime`.
  Skip modules whose `__file__` is not a `str` (namespace packages, builtins, frozen). Never raise out of the hook: wrap the body in
  `try/except Exception` and on failure write nothing (a missing record is what the host detects). Do not import third-party modules.
  Keep this file under 120 lines.

### 2. `gateway/research/containment.py`

- Constants: `PROVENANCE_MOUNT = "/provenance"`, `PROVENANCE_STAGE_DIR = "/stage/provenance"`,
  `PROVENANCE_RECORDER_SOURCE = Path(__file__).resolve().parent / "containment_provenance"` (the directory),
  `PROVENANCE_CONTRACT = "research-containment-provenance-v1"`.
- `recorder_sha256() -> str`: sha256 of `PROVENANCE_RECORDER_SOURCE / "sitecustomize.py"` bytes (raise ContainmentError if missing).
- `_base_argv`: after the `/snapshot` ro-bind add `--ro-bind <PROVENANCE_RECORDER_SOURCE> /provenance`; set `PYTHONPATH` to
  `/provenance:/snapshot/src`. The analysis override becomes `/provenance:/snapshot/src:/work`.
- `bwrap_argv`: new keyword `provenance_dir: Path | None = None`. For `targets-*`, `evaluate-*` and `analysis` it is REQUIRED
  (`ContainmentError("provenance directory is required for <stage>")` when None); it must be an absolute, existing, non-symlink
  directory. For `validate-*` it must be None (`ContainmentError` otherwise). When set, append, AFTER the stage's own `/stage` mounts:
  `--bind <provenance_dir> /stage/provenance --setenv RESEARCH_PROVENANCE_DIR /stage/provenance --setenv RESEARCH_PROVENANCE_STAGE <stage>`.
  (For analysis the existing `--dir /stage` already exists, so the bind lands inside it.)
- Keep every other mount/env exactly as today. `stage_plan(**mounts)` passes `provenance_dir` through unchanged.

### 3. `gateway/research/provenance.py` (new host-side verifier)

```python
class ProvenanceError(RuntimeError): ...
MAX_RECORD_BYTES = 8 * 1024 * 1024
def verify_stage_provenance(records_dir: Path, *, stage: str, pins: RuntimePins, worktree: Path, commit: str,
                            work_top_levels: frozenset[str], require_quantipy: bool) -> dict[str, object]
```
Checks (each failure → `ProvenanceError` with a specific message):
1. `records_dir` is a real directory; it contains ≥1 regular non-symlink `*.json` file ≤ MAX_RECORD_BYTES; no other entries.
   Parse each with a strict duplicate-key-rejecting `object_pairs_hook`; require `contract == PROVENANCE_CONTRACT`, `stage == stage`,
   `flags.safe_path is True`, `flags.no_user_site is True`, `recorder.path == "/provenance/sitecustomize.py"`,
   `recorder.sha256 == containment.recorder_sha256()`, `pythonpath` starts with `"/provenance:/snapshot/src"`.
2. For every module with origin `snapshot`: `path` starts with `/snapshot/src/`; the host file `pins.snapshot_dir / relative` is a
   regular file whose sha256 equals the recorded sha256.
3. For every module with origin `work`: `path` starts with `/work/`; the bytes of `git -C <worktree> cat-file blob <commit>:<relative>`
   have sha256 equal to the recorded sha256 (a module imported from an untracked or modified file fails here). Run git with
   `capture_output=True`, `check=False`, and treat a non-zero exit as a failure naming the relative path.
4. Every module whose top-level package name (`name.partition(".")[0]`) is `quantipy` must have origin `snapshot`. If
   `require_quantipy` is true, at least one such module must be present across the stage's records.
5. Every module whose top-level name is in `work_top_levels` must have origin `work`. No origin-`work` module may have top-level `quantipy`.
6. Modules with origin `recorder`: exactly the module named `sitecustomize`; its sha256 must equal `recorder_sha256()`.
Return a summary dict: `{"stage", "record_count", "recorder_sha256", "snapshot_modules", "work_modules", "runtime_modules",
"work_top_levels": sorted list, "argv": [each record's argv], "pids": [...]}` (ints/lists only, JSON-serializable).

### 4. `gateway/research/worker.py`

- Targets stage: after `_require_empty_directory(target_dir, ...)`, create `target_dir / "provenance"` with `_owned_directory`, pass
  `provenance_dir=` to `stage_plan`, and after a successful stage call `verify_stage_provenance(..., stage=f"targets-{sid}",
  require_quantipy=False, work_top_levels=<see below>)`; on `ProvenanceError` set `status = "provenance_invalid"` and raise
  `ContainmentError(f"targets provenance invalid for {sid}: {exc}")`. Store the summary as `scenarios_evidence[sid]["targets_provenance"]`.
- Evaluate stage: same with `evaluator_dir / "provenance"`, `require_quantipy=True`, summary key `"evaluator_provenance"`. The existing
  `output_dir` (= `/stage/out`) entry check is unaffected because provenance lives at `/stage/provenance`.
- Analysis stage: `analysis_dir / "provenance"`, `require_quantipy=True`, summary stored in the evidence as `"analysis_provenance"`.
  The analysis manifest scan (`for path in analysis_dir.rglob("*")`) must skip the `provenance` directory subtree, and
  `_require_empty_directory(analysis_dir, ...)` must run BEFORE the provenance dir is created.
- `work_top_levels` = `{plan.analysis.module.partition(".")[0]}` ∪ (`{argv[2].partition(".")[0]}` when the scenario `targets_argv[1] == "-m"`).
- Include `"provenance_invalid"` handling in the except block only via the explicit `status` set above (no string matching).

### 5. Pre-review mechanical gate at implementation submission

- `gateway/research/provenance.py`: add
  `validate_provenance_evidence(store: ResearchStore, attempt_id: str, run_plan: RunPlan, path: Path) -> str` that:
  - reads `path` (regular, ≤ 1 MiB, strict JSON object) with exactly the keys
    `{"contract": "research-provenance-evidence-v1", "attempt_id", "commit", "stages": [{"stage": str, "records_dir": str}, ...]}`;
  - requires `attempt_id`/`commit` to equal the attempt's id and `run_plan.commit`; stage names must match containment's stage grammar,
    be unique, and include ≥1 `targets-*`, ≥1 `evaluate-*`, and exactly one `analysis`;
  - requires every `records_dir` to be an absolute path inside `attempt.worktree_path`, and every `*.json` file inside it to be tracked
    at `commit` with identical bytes (`git -C worktree cat-file blob <commit>:<relative>` sha256 == file sha256), so the provenance
    bytes are part of the reviewed source;
  - loads pins with `runtime_pins_from_record(dict(store.config()))` and calls `verify_runtime_pins(pins)`;
  - calls `verify_stage_provenance` for each stage with `worktree=attempt.worktree_path`, `commit`, `work_top_levels` derived as in §4
    from the run plan, `require_quantipy=True` for evaluate/analysis and False for targets;
  - returns canonical JSON (`to_json` from codec) of `{"contract": "research-provenance-evidence-v1", "attempt_id", "commit",
    "recorder_sha256", "stages": [summary dicts]}`; any failure raises `ProvenanceError`.
- `gateway/research/cli.py::implementation-submit`: add a REQUIRED option `--provenance-evidence PATH`. Call
  `validate_provenance_evidence` BEFORE `store.submit_implementation` and pass its JSON as a new keyword
  `containment_provenance=` to `submit_implementation`. A `ProvenanceError` is reported through `_fail` (exit 1) and the attempt
  stays OPENED.
- `gateway/research/store.py::submit_implementation`: new required keyword `containment_provenance: str`. Persist it in the same
  transaction as `attempt_evidence` kind `containment_provenance` and project it to `containment-provenance.json` next to
  `implementation.json`; on the replay path require equality with the stored payload (StoreConflict otherwise) and repair its projection.
- `gateway/research/review_evidence.py::build_review_bundle`: require `containment_provenance` evidence (BundleError
  "review bundle requires containment provenance evidence" when absent) and write it into the bundle as `containment-provenance.json`
  (so the bundle digest and the reviewer see the host-verified summary). Append one sentence to the default instructions text:
  "containment-provenance.json is the host-verified in-sandbox import provenance for the tested commit."
- `gateway/research/wake.py::compose_wake`: the OPENED action string becomes
  ``dispatch coder; submit with `gateway-cli research implementation-submit {attempt_id} --root ROOT --file impl.json --run-plan run-plan.json --provenance-evidence provenance.json` ``.

### 6. Runtime skill and mirror text (short, factual)

Add a "Containment provenance" section (≤ 12 lines each) to `gateway/agent_config/skills/autoresearch/SKILL.md`,
`gateway/agent_config/skills/research-loop/SKILL.md`, and `.claude/skills/research-loop/SKILL.md` stating: every targets/evaluate/analysis
stage built through `gateway.research.containment.stage_plan` must pass `provenance_dir=` and receives the deployed
`PYTHONPATH=/provenance:/snapshot/src[:/work]`; the sandbox writes `research-containment-provenance-v1` records to `/stage/provenance/`;
the implementer's synthetic harness must retain those record directories inside the committed worktree and submit a
`research-provenance-evidence-v1` index via `implementation-submit --provenance-evidence`; submission is refused when any stage lacks
records, a quantipy module resolved outside `/snapshot/src`, or a `/work` module differs from the tested commit; the historical worker
applies the same verification and fails a run with `provenance_invalid`. Keep each bootstrap file under 20,000 bytes (check with `wc -c`).

### 7. Tests
- New `tests/gateway/research/test_provenance.py`: unit tests for `verify_stage_provenance` using a tmp snapshot dir, a tmp git repo
  worktree with one committed module, and hand-written records (good record passes; wrong snapshot hash, wrong work hash, untracked
  work file, quantipy from runtime origin, wrong recorder sha, wrong stage, safe_path false, extra non-json entry, missing quantipy
  when required, duplicate JSON keys → each ProvenanceError). Also test `validate_provenance_evidence` happy path and: missing analysis
  stage, records_dir outside worktree, record file not tracked at commit, commit mismatch.
- A real end-to-end recorder test: run the shared venv-free interpreter `sys.executable` with `PYTHONPATH=<recorder dir>` and
  `RESEARCH_PROVENANCE_DIR/STAGE` set on a tiny script that imports a module from a tmp "snapshot" dir mounted at a fake path — since
  there is no bwrap here, monkeypatch nothing; instead assert the record file is written, has the contract, and lists the module with
  origin "runtime" (paths outside `/snapshot/src` classify as runtime) and the recorder with a correct sha256. Also assert no file is
  written when the env vars are absent.
- `tests/gateway/research/test_containment.py`: update existing bwrap tests for the new `/provenance` ro-bind and PYTHONPATH values;
  add tests that targets/evaluate/analysis without `provenance_dir` raise, validate with `provenance_dir` raises, and the bind/env
  triplet is appended after the stage mounts.
- `tests/gateway/research/test_worker.py`: the real containment tests (`test_real_target_fixture_executes_inside_containment_boundary`,
  the two-scenario test, analysis tests) must pass with provenance directories created; add one test where the analysis stage's
  records are deleted before verification (monkeypatch `verify_stage_provenance` to raise, or make the fixture entrypoint call
  `os._exit(0)` so no record is written) → run evidence status `provenance_invalid`.
- `tests/gateway/research/test_store.py` / `test_cli_scenario.py` / `test_review_evidence.py` / `test_wake.py`: update for the new
  required `containment_provenance` keyword, the `--provenance-evidence` option, the bundle file, and the wake message.

## Files you may change
- gateway/research/containment.py
- gateway/research/containment_provenance/sitecustomize.py (new)
- gateway/research/provenance.py (new)
- gateway/research/worker.py
- gateway/research/store.py (only `submit_implementation` and its projection helper calls)
- gateway/research/cli.py (only the `implementation-submit` command and imports)
- gateway/research/review_evidence.py (only `build_review_bundle`)
- gateway/research/wake.py (only the OPENED action string)
- gateway/agent_config/skills/autoresearch/SKILL.md, gateway/agent_config/skills/research-loop/SKILL.md, .claude/skills/research-loop/SKILL.md
- tests/gateway/research/test_provenance.py (new), test_containment.py, test_worker.py, test_store.py, test_cli_scenario.py,
  test_review_evidence.py, test_wake.py
- pyproject.toml ONLY if the new package directory must be added to package data for `containment_provenance/` to ship; say so explicitly.

## Files you must not change
- Everything else, including host_records.py, contracts.py, admission.py, control.py, jobs.py, status.py, any other agent_config file,
  and anything under /home/dev/repos/quantipy.

## Verification (run these yourself and paste real output)
```
uv run pytest tests/gateway/research -q
uv run ruff check gateway tests
uv run ruff format --check gateway tests
uv run mypy gateway tests
wc -c gateway/agent_config/skills/autoresearch/SKILL.md gateway/agent_config/skills/research-loop/SKILL.md
```
Note: tests that launch real bubblewrap/systemd-run stages cannot run inside your sandbox and will fail with
`containment_unavailable` / `Operation not permitted`; report exactly which tests failed that way and do not "fix" them. Everything
else must pass. If mypy or ruff reports something in a file you may not change, report it verbatim and stop.

## Completion report
List changed files, the exact verification output (last 8 lines of each command), the sandbox-environmental test failures by name, and
unresolved issues. Reply with the single line `PLAN_C_DONE` as the very last line only if every acceptance criterion above is met.
