[PR Similarity](../../README.md) / [benchmark](../README.md)

# Credentials after a run

The persistent ChatGPT profile and an attempt's private HOME have different
lifetimes. In the default OAuth example, reusable login credentials live at:

```text
~/.local/share/prsimbm/chatgpt/data/opencode/auth.json
```

The profile intentionally survives the run for login reuse and token refresh.
It is outside the output directory and is never added to the publication archive.
The runner forwards access-only OAuth credentials to the worker environment,
with the refresh token left empty. Each attempt uses a temporary private HOME;
normal completion/cancellation disposes of it. A host crash or forced kill can
leave temporary files, so directory cleanup is not equivalent to token revocation.

Check **file presence and permissions**, without printing the file's contents:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import stat
profile = Path('~/.local/share/prsimbm/chatgpt').expanduser()
auth = profile / 'data/opencode/auth.json'
print('profile exists:', profile.exists())
print('auth file exists:', auth.exists())
if auth.exists():
    print('auth file mode:', oct(stat.S_IMODE(auth.stat().st_mode)))
PY
```

Substitute your configured profile if it differs. The private profile directory
is mode 700 and native credentials are created with an owner-only umask. Never
`cat` the credentials into a terminal log, issue or chat.

If you want no reusable local login after a batch, wait until **all runs using
this profile have stopped**, then deliberately remove only its auth file:

```bash
rm -i -- ~/.local/share/prsimbm/chatgpt/data/opencode/auth.json
```

The interactive command asks before removing the local credential file. Check
its absence with the presence-only snippet above. The next run needs `make auth`
again. This is local deletion, not server-side revocation. The harness has no
`make logout` or automatic profile deletion option.

API keys from `.env`/your environment and operator-supplied viewer `TOKEN_FILE`
also remain where you put them; the harness does not delete those inputs. Viewer
login passwords/grants are held in memory by the publisher, not written into its
operation journal. Known credential values are redacted from subprocess streams;
usage SQLite is exported without credential tables. This is not a promise that
arbitrary setup tools cannot copy secrets elsewhere. Before sharing a run, review
its prompts, source, setup/resources and logs; checking only for `auth.json` is
not a complete secret audit.
