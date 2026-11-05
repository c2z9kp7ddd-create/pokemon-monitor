#!/usr/bin/env python3
"""Mac only (launchd com.pokemon-sync, every 5 min): keeps this copy in step with GitHub. Runs through Python
because macOS denies /bin/sh access to the Desktop. On a network that blocks GitHub it fails quietly and retries."""
import pathlib, subprocess as s
here = pathlib.Path(__file__).resolve().parent
if s.run(["git", "pull", "-q", "--rebase", "--autostash"], cwd=here, capture_output=True).returncode:
    s.run(["git", "rebase", "--abort"], cwd=here, capture_output=True)
s.run(["git", "push", "-q"], cwd=here, capture_output=True)
