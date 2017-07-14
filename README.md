Automate backport PR

## Usage

```
usage: backport.py [-h] --repo {chainer,cupy} --token TOKEN --pr PR [--debug]

optional arguments:
  -h, --help            show this help message and exit
  --repo {chainer,cupy}
                        chainer or cupy
  --token TOKEN         GitHub access token.
  --pr PR               The original PR number to be backported.
```

## Example

```shell
$ python backport.py --repo chainer --token abcdefghijklmn --pr 1234
```

## Limitation

Currently, backport PR is made against hard-coded branches: `v2` for `chainer` and `v1` for `cupy`.


## How it works

Basically it follows this procedure:

- Clone the target branch (e.g. `v2`) of the target repository (e.g. `chainer/chainer`) to a temporary directory.
- Create a local temporary branch and cherry-pick the merge commit of the original PR.
- Push it to the user repository.
- Make a backport PR.
