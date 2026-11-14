# DisScrape

Collect Discord display names, bios, custom statuses, pronouns and avatars from servers your account can access. Text goes into separate files; avatars are saved by user ID. The terminal shows progress, saved counts and request errors while it runs.

## Setup

You need Python 3.11+ and Git.

```sh
git clone https://github.com/cockier/DisScrape.git
cd DisScrape
python -m pip install .
```

Create `input/tokens.txt` and put one Discord account token on a single line. Run commands from the project directory:

```sh
python main.py
```

For plain terminal output:

```sh
python main.py --plain
```

Use `--root PATH` to read `input/` and write `output/` in another directory. Keep the token file private; it is excluded from Git along with collected data.

## Output

```text
output/
  avatars/           Images named by user ID
  names/display.txt  Display names
  bio/bio.txt        Bios
  status/status.txt  Custom status text
  pronouns/pronouns.txt
  errors.jsonl       Request failures
```

`python main.py` writes one value per line in the text files. Line breaks inside values are escaped. Empty fields are skipped, and users shared across servers are counted once per run. Avatars keep the image format returned by the CDN.

Bios and pronouns prefer server profiles, then global profiles. Status text comes from available activity data, so fields may be missing even when an avatar is saved. Errors are recorded in `output/errors.jsonl`.

Text files are rewritten from the current run's records at checkpoints. Back up an existing output directory before running again. Run one instance per output directory.

## Notes

The client uses Discord's Gateway and an undocumented profile endpoint. Access and returned fields can change. This implementation signs in as a user account; it is not a bot API client.

The terminal display uses [richer](https://github.com/cockier/richer). `python -m pip install .` installs the version pinned in `pyproject.toml`.
