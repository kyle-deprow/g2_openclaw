# `--test-script` CLI/UX Design

> Design doc for the interactive `--test-script <file.txt>` mode on `gateway launch`.

---

## 1. Script File Format Spec

### File: Plain `.txt`, one utterance per line

```text
# Lines starting with '#' are comments (stripped before processing).
# Blank lines are ignored.

Start a copilot session in test
Ask copilot to set up UV scaffolding in that repo
Close the copilot session
```

### Rules

| Rule | Detail |
|------|--------|
| Encoding | UTF-8 |
| Comments | Lines where `line.lstrip().startswith("#")` — silently skipped |
| Blank lines | `line.strip() == ""` — silently skipped |
| Trailing whitespace | Stripped via `line.strip()` |
| Min lines | ≥ 1 non-blank, non-comment line (else abort with error) |
| Max lines | No hard cap; warn if > 20 (long scripts take minutes to TTS+preview) |

### Parser function

```python
# gateway/test_script.py

from pathlib import Path

def parse_script(path: Path) -> list[str]:
    """Parse a test-script file into a list of utterances.

    Raises:
        typer.BadParameter  if file is empty or contains no usable lines.
    """
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    if not lines:
        raise typer.BadParameter(
            f"Test script {path} contains no usable lines "
            "(only comments/blanks)."
        )
    if len(lines) > 20:
        console.print(
            f"[yellow]⚠  Script has {len(lines)} lines — "
            "TTS + Whisper preview may take a while.[/yellow]"
        )
    return lines
```

---

## 2. CLI Option Design

### Option definition (add alongside existing options — [cli.py L660-676](gateway/cli.py#L660-L676))

```python
_test_script_option = typer.Option(
    None,
    "--test-script",
    help=(
        "Path to a .txt script file (one utterance per line). "
        "Each line is TTS'd to WAV, Whisper-previewed, then fed "
        "to the simulator on each tap."
    ),
    exists=True,
    dir_okay=False,
    readable=True,
)
```

### Signature change ([cli.py L678-686](gateway/cli.py#L678-L686))

```python
@app.command()
def launch(
    audio_device: str | None = _audio_device_option,
    no_simulator: bool = _no_simulator_option,
    no_openclaw: bool = _no_openclaw_daemon_option,
    list_audio_devices: bool = _list_audio_devices_option,
    local_audio: bool = _local_audio_option,
    test_wav: str | None = _test_wav_option,
    test_script: Path | None = _test_script_option,    # ← NEW
) -> None:
```

### Mutual exclusion with `--test-wav`

Add immediately after `if list_audio_devices:` block ([cli.py L690](gateway/cli.py#L690)):

```python
    # -- Mutual exclusion: --test-wav vs --test-script -------------------------
    if test_wav and test_script:
        console.print(
            "[red]✗[/red] --test-wav and --test-script are mutually exclusive."
        )
        raise typer.Exit(code=1)
```

### Implied flags

When `--test-script` is set:
- `local_audio` is forced **off** (WAV injection replaces mic input)
- `no_simulator` stays user-controlled (script works with or without the simulator)

---

## 3. Interactive Approval Flow

### 3.1 Phase: TTS Generation

After validation, before the launch sequence begins:

```python
    # -- Test script: TTS + preview -------------------------------------------
    if test_script:
        from gateway.test_script import (
            parse_script,
            generate_wavs,
            preview_transcriptions,
            approval_loop,
        )

        utterances = parse_script(test_script)

        console.print(
            f"\n[bold]Test script:[/bold] {test_script} "
            f"({len(utterances)} utterance{'s' if len(utterances) != 1 else ''})\n"
        )

        with console.status("[bold green]Generating TTS audio…[/bold green]"):
            wav_dir, wav_paths = generate_wavs(utterances)
            # wav_dir  = Path to tempfile.mkdtemp(prefix="g2_test_script_")
            # wav_paths = list[Path], one per utterance, in order

        with console.status("[bold green]Transcribing with Whisper…[/bold green]"):
            transcriptions = preview_transcriptions(wav_paths)
            # list[str], one per WAV

        approved_wavs = approval_loop(utterances, transcriptions, wav_paths)
        # Returns final list[Path] after user approves
        # (may re-generate individual lines)
```

### 3.2 Preview Table

Uses `rich.table.Table` (import already available via `rich.console`):

```python
# gateway/test_script.py

from rich.table import Table
from rich.text import Text
from rich.console import Console

console = Console()

# Max column width for text truncation
_COL_MAX = 50


def _similarity_ok(original: str, transcription: str) -> bool:
    """Quick heuristic: normalised edit distance < 0.3."""
    # Lowercase, strip punctuation for comparison
    import re
    def _norm(s: str) -> str:
        return re.sub(r"[^\w\s]", "", s.lower()).strip()
    a, b = _norm(original), _norm(transcription)
    if not a:
        return False
    # Simple ratio: shared words / max words
    words_a, words_b = set(a.split()), set(b.split())
    if not words_a:
        return False
    overlap = len(words_a & words_b) / max(len(words_a), len(words_b))
    return overlap >= 0.7


def build_preview_table(
    utterances: list[str],
    transcriptions: list[str],
) -> tuple[Table, list[bool]]:
    """Build the Rich preview table and per-row pass/fail list."""
    table = Table(
        title="Test Script Preview",
        show_lines=True,
        title_style="bold",
    )
    table.add_column("#", justify="center", width=4)
    table.add_column("Original", max_width=_COL_MAX)
    table.add_column("Whisper Transcription", max_width=_COL_MAX)
    table.add_column("Status", justify="center", width=6)

    statuses: list[bool] = []
    for i, (orig, trans) in enumerate(zip(utterances, transcriptions), 1):
        ok = _similarity_ok(orig, trans)
        statuses.append(ok)
        status_icon = "✅" if ok else "⚠️"
        table.add_row(
            str(i),
            Text(orig, overflow="ellipsis", no_wrap=True),
            Text(trans, overflow="ellipsis", no_wrap=True),
            status_icon,
        )
    return table, statuses
```

### 3.3 Approval Loop

```python
def approval_loop(
    utterances: list[str],
    transcriptions: list[str],
    wav_paths: list[Path],
) -> list[Path]:
    """Interactive approval flow. Returns approved wav_paths.

    User actions at the prompt:
        <Enter>       — approve all, continue to launch
        r <N>         — re-record line N (re-run TTS + Whisper, redisplay table)
        e <N> <text>  — replace line N's text, re-TTS, re-transcribe, redisplay
        q             — abort (typer.Exit)
    """
    while True:
        table, statuses = build_preview_table(utterances, transcriptions)
        console.print(table)
        console.print()

        warn_count = statuses.count(False)
        if warn_count:
            console.print(
                f"[yellow]⚠  {warn_count} line(s) have low transcription "
                "similarity.[/yellow]"
            )

        console.print(
            "[dim]Commands:  ⏎ approve  │  r N  retry line  │  "
            "e N <text>  edit line  │  q  quit[/dim]"
        )
        answer = console.input("[bold green]❯[/bold green] ").strip()

        if answer == "" or answer.lower() in ("y", "yes"):
            return wav_paths

        if answer.lower() in ("q", "quit", "n", "no"):
            console.print("[red]Aborted.[/red]")
            raise typer.Exit(code=0)

        # --- retry: r N ---
        m = re.match(r"r\s+(\d+)", answer, re.IGNORECASE)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(utterances):
                console.print(f"  Re-generating line {idx + 1}…")
                wav_paths[idx] = _generate_single_wav(utterances[idx], wav_paths[idx].parent, idx)
                transcriptions[idx] = _transcribe_single(wav_paths[idx])
                continue
            console.print(f"[red]Invalid line number: {m.group(1)}[/red]")
            continue

        # --- edit: e N <new text> ---
        m = re.match(r"e\s+(\d+)\s+(.+)", answer, re.IGNORECASE)
        if m:
            idx = int(m.group(1)) - 1
            new_text = m.group(2).strip()
            if 0 <= idx < len(utterances):
                utterances[idx] = new_text
                console.print(f"  Re-generating line {idx + 1} with new text…")
                wav_paths[idx] = _generate_single_wav(new_text, wav_paths[idx].parent, idx)
                transcriptions[idx] = _transcribe_single(wav_paths[idx])
                continue
            console.print(f"[red]Invalid line number: {m.group(1)}[/red]")
            continue

        console.print("[red]Unknown command.[/red] Use ⏎, r N, e N <text>, or q.")
```

---

## 4. Error Handling

### 4.1 File validation errors

| Condition | Behaviour |
|-----------|-----------|
| File not found | Typer's `exists=True` handles this before `launch()` runs |
| File is a directory | Typer's `dir_okay=False` handles this |
| File is empty / all comments | `parse_script()` raises `typer.BadParameter` |
| File not readable | Typer's `readable=True` handles this |

### 4.2 TTS failures

```python
def _generate_single_wav(text: str, out_dir: Path, index: int) -> Path:
    """Generate a single WAV via espeak-ng. Raises RuntimeError on failure."""
    out_path = out_dir / f"line_{index:03d}.wav"
    result = subprocess.run(
        [
            "espeak-ng",
            "-v", "en-us",
            "-s", "150",          # words-per-minute (natural pace)
            "-w", str(out_path),  # write WAV
            text,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"espeak-ng failed for line {index + 1}: {result.stderr.strip()}"
        )
    if not out_path.is_file() or out_path.stat().st_size < 100:
        raise RuntimeError(
            f"espeak-ng produced empty/missing WAV for line {index + 1}"
        )
    return out_path
```

If TTS fails on **any** line during initial batch:
- Print `[red]✗[/red] TTS failed on line {N}: {detail}` 
- Clean up temp dir
- `raise typer.Exit(code=1)`

If TTS fails during an interactive retry (`r N` / `e N`):
- Print the error but stay in the approval loop so the user can try again or quit

### 4.3 Transcription failures

If Whisper fails on a line:
- Set that line's transcription to `"[transcription failed]"`
- Mark status as ⚠️ 
- User can `r N` to retry or approve anyway

### 4.4 espeak-ng not installed

Check at the top of the test-script flow:

```python
def _check_espeak() -> None:
    """Verify espeak-ng is on PATH."""
    if shutil.which("espeak-ng") is None:
        console.print(
            "[red]✗[/red] espeak-ng not found on PATH.\n"
            "  Install: [bold]sudo apt install espeak-ng[/bold]"
        )
        raise typer.Exit(code=1)
```

---

## 5. Passing Approved WAVs to the Gateway

### Strategy: Temp directory + environment variable `G2_TEST_SCRIPT_DIR`

The existing `--test-wav` mechanism passes a single file via `G2_TEST_WAV` env var. For multi-WAV scripts, we introduce a **directory-based** approach:

### 5.1 Temp dir layout

```
/tmp/g2_test_script_a1b2c3d4/
├── manifest.json          # ordered list + metadata
├── line_000.wav
├── line_001.wav
└── line_002.wav
```

**`manifest.json`**:
```json
{
  "version": 1,
  "utterances": [
    {"index": 0, "text": "Start a copilot session in test", "wav": "line_000.wav"},
    {"index": 1, "text": "Ask copilot to set up UV scaffolding…", "wav": "line_001.wav"},
    {"index": 2, "text": "Close the copilot session", "wav": "line_002.wav"}
  ]
}
```

### 5.2 Passing to the gateway subprocess

In the launch sequence ([cli.py L800-810](gateway/cli.py#L800-L810)), add alongside the existing `G2_TEST_WAV` injection:

```python
    # In the gateway subprocess env setup:
    if approved_script_dir:
        gw_env["G2_TEST_SCRIPT_DIR"] = str(approved_script_dir)
```

This is the same pattern used for `G2_TEST_WAV` at [cli.py L807-808](gateway/cli.py#L807-L808).

### 5.3 Config dataclass extension ([config.py L13-31](gateway/config.py#L13-L31))

```python
@dataclass(frozen=True)
class GatewayConfig:
    # ... existing fields ...
    test_wav: str | None = None
    test_script_dir: str | None = None   # ← NEW: path to manifest dir
```

And in `load_config()`:

```python
    test_script_dir_raw = os.environ.get("G2_TEST_SCRIPT_DIR")
    test_script_dir: str | None = None
    if test_script_dir_raw:
        p = Path(test_script_dir_raw)
        manifest = p / "manifest.json"
        if not manifest.is_file():
            raise ValueError(f"G2_TEST_SCRIPT_DIR missing manifest.json: {test_script_dir_raw}")
        test_script_dir = str(p.resolve())
```

### 5.4 Server consumption (future — out of scope for this design)

The server reads `manifest.json`, then on each `stop_audio` cycle, pops the next WAV from the list (instead of using the audio buffer). This mirrors the existing `_test_wav` injection at [server.py L370-390](gateway/server.py#L370-L390) but iterates through the manifest.

### 5.5 Temp dir lifecycle

| Event | Action |
|-------|--------|
| Approval completed | `generate_manifest(wav_dir, utterances, wav_paths)` writes `manifest.json` |
| Gateway exits | Cleanup callback in `_cleanup()` removes the temp dir via `shutil.rmtree()` |
| `Ctrl+C` during preview | `approval_loop` raises `typer.Exit`; caller's `finally:` cleans temp dir |

Add to the `_cleanup` function at [cli.py L710](gateway/cli.py#L710):

```python
    def _cleanup(*_: object) -> None:
        console.print("\n[bold yellow]Shutting down…[/bold yellow]")
        _terminate_procs(spawned)
        for fh in log_files:
            with contextlib.suppress(Exception):
                fh.close()
        if _test_script_tmpdir and _test_script_tmpdir.exists():
            shutil.rmtree(_test_script_tmpdir, ignore_errors=True)
        console.print("[green]All processes stopped.[/green]")
```

---

## 6. Summary Panel Extension

In the launch summary ([cli.py L905-915](gateway/cli.py#L905-L915)), add:

```python
    if test_script:
        rows.append(
            f"[bold]Test script:[/bold]  {test_script.name} "
            f"({len(approved_wavs)} lines, tap to advance)"
        )
```

---

## 7. New Module: `gateway/test_script.py`

All the above functions live in a single new module. Public API:

```python
# gateway/test_script.py

def check_espeak() -> None: ...
def parse_script(path: Path) -> list[str]: ...
def generate_wavs(utterances: list[str]) -> tuple[Path, list[Path]]: ...
def preview_transcriptions(wav_paths: list[Path]) -> list[str]: ...
def build_preview_table(utterances: list[str], transcriptions: list[str]) -> tuple[Table, list[bool]]: ...
def approval_loop(utterances: list[str], transcriptions: list[str], wav_paths: list[Path]) -> list[Path]: ...
def generate_manifest(wav_dir: Path, utterances: list[str], wav_paths: list[Path]) -> Path: ...
```

None of these import the gateway server modules — the module is CLI-only, called from `launch()` before any subprocess is spawned.

---

## 8. End-to-End Flow Diagram

```
User runs:
  python -m gateway launch --test-script my_test.txt --audio-device alsa:default

  ┌─ parse_script("my_test.txt")
  │   → ["Start a copilot session…", "Ask copilot…", "Close…"]
  │
  ├─ check_espeak()
  │   → OK (or exit 1)
  │
  ├─ generate_wavs(utterances)
  │   → /tmp/g2_test_script_xxxx/  +  [line_000.wav, line_001.wav, …]
  │
  ├─ preview_transcriptions(wav_paths)
  │   → ["Start a co-pilot session…", "Ask co-pilot…", "Close…"]
  │
  ├─ approval_loop(utterances, transcriptions, wav_paths)
  │   │
  │   │  ┌──────────────────────────────────────────────────────────────┐
  │   │  │  #  │ Original           │ Whisper Transcription │ Status  │
  │   │  ├──────────────────────────────────────────────────────────────┤
  │   │  │  1  │ Start a copilot …  │ Start a co-pilot …   │   ✅    │
  │   │  │  2  │ Ask copilot to …   │ Ask co-pilot to …    │   ✅    │
  │   │  │  3  │ Close the copilot… │ Close the co-pilot…  │   ✅    │
  │   │  └──────────────────────────────────────────────────────────────┘
  │   │  Commands:  ⏎ approve  │  r N  retry  │  e N <text>  edit  │  q quit
  │   │  ❯ <Enter>
  │   │
  │   └─→ approved wav_paths
  │
  ├─ generate_manifest(wav_dir, utterances, wav_paths)
  │   → writes manifest.json
  │
  └─ Normal launch sequence (steps 1-4):
      gw_env["G2_TEST_SCRIPT_DIR"] = "/tmp/g2_test_script_xxxx"
      → OpenClaw daemon
      → Gateway subprocess (reads manifest, serves WAVs on each tap cycle)
      → Vite dev server
      → Simulator
```
