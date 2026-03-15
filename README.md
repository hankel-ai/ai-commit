# ai-commit

AI-powered git commit message generator using a local [Ollama](https://ollama.com) LLM. Single Python script, no pip dependencies.

## How it works

1. Detects uncommitted changes in a git repo
2. Sends the diff to a local Ollama model
3. Generates a [conventional commit](https://www.conventionalcommits.org/) message
4. Lets you accept, regenerate, edit, or abort
5. Runs `git add -A`, `git commit`, and `git push`

## Requirements

- Python 3.7+
- [Ollama](https://ollama.com) running locally (or accessible via network)
- A pulled model (e.g. `ollama pull qwen3-coder:480b-cloud`)

## Usage

```bash
# Run in current directory
python ai-commit.py

# Run against a specific repo
python ai-commit.py /path/to/repo

# Use a different model
python ai-commit.py --model mistral

# Test Ollama connectivity without committing
python ai-commit.py --test
```

## Options

| Flag | Env Var | Default | Description |
|------|---------|---------|-------------|
| `[folder]` | — | `.` (cwd) | Path to the git repository |
| `--model` | `AI_COMMIT_MODEL` | `qwen3-coder:480b-cloud` | Ollama model name |
| `--url` | `AI_COMMIT_URL` | `http://localhost:11434` | Ollama base URL |
| `--test` | — | off | Generate message only, don't commit |

## Interactive prompt

After generating a commit message, you'll see:

```
[Enter] Accept   [r] Regenerate   [e] Edit   [q] Quit
```

- **Enter** — accept the message and run add/commit/push
- **r** — ask the LLM for a new message
- **e** — type your own replacement message
- **q** — abort without changes

## Commit format

Messages follow conventional commit style:

```
type(scope): description

Optional body for non-trivial changes.
```

Types: `feat`, `fix`, `refactor`, `docs`, `style`, `test`, `chore`, `build`

## Error handling

- **Ollama not running** — prints connection error with `ollama serve` hint
- **Model not found** — prints 404 with `ollama pull` hint
- **Not a git repo** — prints error and exits
- **No changes** — prints "nothing to commit"
- **Push fails** — commit is saved locally, warning printed
- **Large diffs** — truncated to ~8000 chars to fit LLM context
