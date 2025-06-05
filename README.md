# `backport` - Tool to semi-automate backport pull requests

This tool is used to backport pull requests labeled `to-be-backported`.

Generally backport PRs are [automatically created by GitHub Actions workflow](https://github.com/cupy/cupy/blob/main/.github/workflows/backport.yml).
However, in case there is a conflict during the backport process, you need to run this tool in your local environment to manually resolve the conflict.

## Prerequisites

1. Create a [classic GitHub token with `public_repo` scope](https://github.com/settings/tokens/new?scopes=public_repo).
2. Install PyGitHub (`pip install pygithub`).

## Usage

For example, to backport PR #9876:

```shell
$ export BACKPORT_GITHUB_TOKEN=ghp_XXXXXXXXXXXXXXXXXXXXX
$ python backport.py --repo cupy --pr 9876
```

If there are conflicts during backport, the tool will stop and ask you to resolve the conflict manually.
Follow the instructions shown.

Run `python backport.py --help` for additional options.

## How it works

Basically it follows this procedure:

1. Clone the target branch (e.g. `v13`) of the target repository (e.g. `cupy/cupy`) to a temporary directory.
2. Create a local temporary branch and cherry-pick the merge commit of the original PR.
3. Push it to the user repository.
4. Make a backport PR.
