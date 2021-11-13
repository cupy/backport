#!/usr/bin/env python
from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterator, Optional, Tuple, TYPE_CHECKING

import github


logger = logging.getLogger(__name__)


ExitCode = int
if TYPE_CHECKING:
    # Support Python 3.7. Use typing_extensions because mypy installs it.
    # `try: from typing import Literal` causes:
    # error: Module 'typing' has no attribute 'Literal'  [attr-defined]
    from typing_extensions import Literal
    TempdirDeleteOption = Literal[True, False, 'on-success']


class GracefulError(Exception):
    pass


class NoActionRequiredError(GracefulError):
    pass


class GitCommandError(Exception):
    def __init__(self, msg: str, cmd: list[str]):
        super(GitCommandError, self).__init__(msg)
        self.cmd = cmd

    def __str__(self) -> str:
        return "{}\nCommand: {}".format(
            super(GitCommandError, self).__str__(),
            str(self.cmd))


@contextlib.contextmanager
def tempdir(
        delete: TempdirDeleteOption = True, **kwargs: Any) -> Iterator[str]:
    assert delete in (True, False, 'on-success')
    temp_dir = tempfile.mkdtemp(**kwargs)
    succeeded = False
    try:
        yield temp_dir
        succeeded = True
    except Exception:
        raise
    finally:
        if delete is True or (delete == 'on-success' and succeeded):
            shutil.rmtree(temp_dir, ignore_errors=True)


@contextlib.contextmanager
def git_work_dir(use_cwd: bool, **tempdir_kwargs: Any) -> Iterator[str]:
    if use_cwd:
        yield os.getcwd()
    else:
        with tempdir(**tempdir_kwargs) as tempd:
            yield os.path.join(tempd, 'work')


def git(args: list[str], cd: Optional[str] = None) -> None:
    cmd = ['git']
    if cd is not None:
        assert os.path.isdir(cd)
        cmd += ['-C', cd]
    cmd += list(args)

    print('**GIT** {}'.format(' '.join(cmd)))

    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise GitCommandError(
            "Git command failed with code {}".format(proc.returncode),
            cmd)

    print('')


class App(object):
    def __init__(
            self, token: str, organ_name: str, repo_name: str,
            debug: bool = False):
        assert isinstance(organ_name, str)
        assert isinstance(repo_name, str)
        self.repo_name = repo_name
        self.organ_name = organ_name
        self.g = github.Github(token)
        self.repo = self.g.get_repo('{}/{}'.format(organ_name, repo_name))
        self.user_name = self.g.get_user().login
        self.debug = debug

    def run_cli(self, **kwargs: Any) -> ExitCode:
        try:
            self._run(**kwargs)
        except NoActionRequiredError as e:
            sys.stderr.write('No action required: {}\n'.format(e))
        except GracefulError as e:
            sys.stderr.write('Error: {}\n'.format(e))
            return 1
        return 0

    def run_bot(self, *, pr_num: int, **kwargs: Any) -> ExitCode:
        try:
            self._run(pr_num=pr_num, **kwargs)
        except NoActionRequiredError as e:
            sys.stderr.write('No action required: {}\n'.format(e))
        except Exception as e:
            sys.stderr.write('Backport failed: {}\n'.format(e))
            pr = self.repo.get_pull(pr_num)
            mention = 'cupy/code-owners'
            if pr.is_merged():
                merged_by = pr.merged_by.login
                if not merged_by.endswith('[bot]'):
                    mention = merged_by
                elif pr.assignee is not None:
                    # For PRs merged by bots (Mergify), mention assignee.
                    mention = pr.assignee.login
            pr.create_issue_comment(f'''\
@{mention} Failed to backport automatically.

----

```
{e}
```
''')
        return 0

    def _run(self, *, pr_num: Optional[int], sha: Optional[str],
             target_branch: str, is_continue: bool,
             abort_before_push: bool, https: bool) -> None:
        assert isinstance(pr_num, int) and pr_num >= 1 or pr_num is None
        assert (pr_num is None and sha is not None) or (
            pr_num is not None and sha is None
        )
        assert isinstance(target_branch, str)
        assert isinstance(is_continue, bool)
        assert isinstance(abort_before_push, bool)
        assert isinstance(https, bool)

        # Get information of the original pull request
        if sha is not None:
            pr_num, branch_name, _ = self.parse_log_message(sha)
        assert pr_num is not None

        pr = self.repo.get_pull(pr_num)
        if not pr.merged:
            raise GracefulError('PR #{} is not merged'.format(pr_num))
        merge_commit_sha = pr.merge_commit_sha
        _, branch_name, _ = self.parse_log_message(merge_commit_sha)

        title = pr.title

        pr_issue = self.repo.get_issue(pr_num)
        labels = set(label.name for label in pr_issue.labels)
        if 'to-be-backported' not in labels:
            raise NoActionRequiredError(
                'PR #{} doesn\'t have \'to-be-backported\' label.'.format(
                    pr_num))
        labels.remove('to-be-backported')
        labels.discard('reviewer-team')
        labels = set(_ for _ in labels if not _.startswith('st:'))

        organ_name = self.organ_name
        user_name = self.user_name
        repo_name = self.repo_name
        if https:
            uri_template = 'https://github.com/{}/{}'
        else:
            uri_template = 'git@github.com:{}/{}'
        origin_remote = uri_template.format(organ_name, repo_name)
        user_remote = uri_template.format(user_name, repo_name)
        bp_branch_name = 'bp-{}-{}-{}'.format(pr_num,
                                              target_branch, branch_name)

        delete: TempdirDeleteOption
        if self.debug or abort_before_push:
            delete = False
        else:
            delete = 'on-success'

        with git_work_dir(
                use_cwd=is_continue, prefix='bp-', delete=delete) as workd:
            assert workd is not None

            print(workd)

            def git_(cmd: list[str]) -> None:
                return git(cmd, cd=workd)

            manual_steps = (
                'Working tree is saved at: {workd}\n\n'
                'Follow these steps:\n\n'
                '  1. Go to the working tree:\n\n'
                '    cd {workd}\n\n'
                '  2. Manually resolve the conflict.\n\n'
                '  3. Continue cherry-pick.\n\n'
                '    git cherry-pick --continue\n\n'
                '  4. Run the backport script with the --continue option.\n\n'
                '    {backport} --continue\n\n\n').format(
                    workd=workd,
                    backport=' '.join([shlex.quote(v) for v in sys.argv]))

            if not is_continue:
                # Clone target repo
                git(['clone', '--branch', target_branch, origin_remote, workd])

                # Create backport branch
                git_(['checkout', '-b', bp_branch_name])
                git_(['fetch', 'origin', merge_commit_sha])
                try:
                    git_(['cherry-pick', '-m1', merge_commit_sha])
                except GitCommandError:
                    sys.stderr.write(
                        'Cherry-pick failed.\n{}'.format(manual_steps))
                    raise GracefulError('Not cleanly cherry-picked')

            if abort_before_push:
                sys.stderr.write(
                    'Backport procedure has been aborted due to'
                    ' configuration.\n{}'.format(manual_steps))
                raise GracefulError('Aborted')

            # Push to user remote
            git_(['push', user_remote, 'HEAD'])

            # Create backport pull request
            print("Creating a pull request.")

            bp_pr = self.repo.create_pull(
                title='[backport] {}'.format(title),
                head='{}:{}'.format(self.user_name, bp_branch_name),
                base=target_branch,
                body='Backport of #{} by @{}'.format(pr_num, pr.user.login))
            bp_pr_issue = self.repo.get_issue(bp_pr.number)
            bp_pr_issue.set_labels('backport', *list(labels))

        print("Done.")
        print(bp_pr.html_url)

    def parse_log_message(self, commit: str) -> Tuple[int, str, str]:
        msg = self.repo.get_commit(commit).commit.message
        head_msg, _, title = msg.split('\n')[:3]
        pattern = r'^Merge pull request #(?P<pr_num>[0-9]+) from [^ /]+/(?P<branch_name>[^ ]+)$'  # NOQA
        m = re.match(pattern, head_msg)
        if m is None:
            raise GracefulError('Invalid log message: {}'.format(head_msg))
        pr_num = int(m.group('pr_num'))
        branch_name = m.group('branch_name')
        return pr_num, branch_name, title


def main(args_: list[str]) -> ExitCode:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--repo', required=True,
        choices=('chainer', 'cupy', 'cupy-release-tools', 'sandbox'),
        help='target repository')
    parser.add_argument(
        '--token', type=str, default=None,
        help='GitHub access token.')
    parser.add_argument(
        '--pr', default=None, type=int,
        help='The original PR number to be backported. Exclusive with --sha')
    parser.add_argument(
        '--sha', default=None, type=str,
        help='The SHA hash of the merge commit. Exclusive with --pr')
    parser.add_argument(
        '--branch', type=str, default='v10',
        help='Target branch to make a backport')
    parser.add_argument(
        '--https', action='store_true', default=False,
        help='Use HTTPS instead of SSH for git access')
    parser.add_argument(
        '--debug', action='store_true')
    parser.add_argument(
        '--continue', action='store_true', dest='is_continue',
        help='Continues the process suspended by conflict situation. Run from'
        ' the working tree directory.')
    parser.add_argument(
        '--abort-before-push', action='store_true',
        help='Abort the procedure before making an push. Useful if you want to'
        ' make some modification to the backport branch. Use --continue to'
        ' make an actual push after making modification.')
    parser.add_argument(
        '--bot', action='store_true', default=False,
        help='Leave a comment when backport failed. This is intended for use'
             ' with GitHub workflow.')
    args = parser.parse_args(args_)

    target_branch = args.branch
    if args.repo == 'chainer':
        organ_name, repo_name = 'chainer', 'chainer'
    elif args.repo == 'cupy':
        organ_name, repo_name = 'cupy', 'cupy'
    elif args.repo == 'cupy-release-tools':
        organ_name, repo_name = 'cupy', 'cupy-release-tools'
    elif args.repo == 'sandbox':
        organ_name, repo_name = 'chainer-ci', 'backport-sandbox'
    else:
        assert False

    if args.pr is None and args.sha is None:
        parser.error('Specify only --pr or --sha')

    if args.pr is not None and args.sha is not None:
        parser.error('Can\'t specify both --pr and --sha')

    github_token = args.token
    if github_token is None:
        if 'BACKPORT_GITHUB_TOKEN' not in os.environ:
            parser.error('GitHub Access token must be specified with '
                         '--token or BACKPORT_GITHUB_TOKEN '
                         'environment variable.')
        github_token = os.environ['BACKPORT_GITHUB_TOKEN']

    if args.debug:
        github.enable_console_debug_logging()

    app = App(
        github_token,
        organ_name=organ_name,
        repo_name=repo_name)

    run_func: Callable[..., ExitCode] = app.run_cli
    if args.bot:
        print('Running as bot mode (will leave a comment when failed).')
        run_func = app.run_bot

    return run_func(
        pr_num=args.pr,
        sha=args.sha,
        target_branch=target_branch,
        is_continue=args.is_continue,
        abort_before_push=args.abort_before_push,
        https=args.https)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    sys.exit(main(sys.argv[1:]))
