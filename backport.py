#!/usr/bin/env python

import argparse
import contextlib
import logging
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

import github


logger = logging.getLogger(__name__)


class GracefulError(Exception):
    pass


class NoActionRequiredError(GracefulError):
    pass


class GitCommandError(Exception):
    def __init__(self, msg, cmd):
        super(GitCommandError, self).__init__(msg)
        self.cmd = cmd

    def __str__(self):
        return "{}\nCommand: {}".format(
            super(GitCommandError, self).__str__(),
            str(self.cmd))


@contextlib.contextmanager
def tempdir(delete=True, **kwargs):
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


class GitWorkDir(object):
    def __init__(self, use_cwd, **kwargs):
        self.use_cwd = use_cwd
        self.tempdir = None
        self.tempdir_kwargs = kwargs
        self.workdir = None

    def __enter__(self):
        if self.use_cwd:
            self.workdir = os.getcwd()
        else:
            self.tempdir = tempdir(**self.tempdir_kwargs)
            tempd = self.tempdir.__enter__()
            self.workdir = os.path.join(tempd, 'work')

        return self

    def __exit__(self, typ, value, traceback):
        if self.use_cwd:
            pass
        else:
            self.tempdir.__exit__(typ, value, traceback)


def git(args, cd=None, stdout=None, stderr=None):
    cmd = ['git']
    if cd is not None:
        assert os.path.isdir(cd)
        cmd += ['-C', cd]
    cmd += list(args)

    print('**GIT** {}'.format(' '.join(cmd)))

    proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr)
    stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        raise GitCommandError(
            "Git command failed with code {}".format(proc.returncode),
            cmd)
    if stdout is not None:
        stdout = stdout.decode('utf8')

    print('')

    return stdout


def git_out(args, cd=None):
    stdout = git(args, cd=cd, stdout=subprocess.PIPE)
    return stdout.rstrip()


def random_string(n):
    chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    return ''.join(random.choice(chars) for _ in range(n))


class App(object):
    def __init__(self, token, organ_name, repo_name, debug=False):
        assert re.match(r'^\w+$', organ_name)
        assert re.match(r'^\w+$', repo_name)
        self.repo_name = repo_name
        self.organ_name = organ_name
        self.g = github.Github(token)
        self.repo = self.g.get_repo('{}/{}'.format(organ_name, repo_name))
        self.user_name = self.g.get_user().login
        self.debug = debug

    def run(self, pr_num, sha, target_branch, is_continue,
            abort_before_push, https):
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

        if self.debug or abort_before_push:
            delete = False
        else:
            delete = 'on-success'

        with GitWorkDir(
                use_cwd=is_continue, prefix='bp-', delete=delete) as workdir:
            workd = workdir.workdir

            print(workd)

            def git_(cmd):
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
                body='Backport of #{}'.format(pr_num))
            bp_pr_issue = self.repo.get_issue(bp_pr.number)
            bp_pr_issue.set_labels('backport', *list(labels))
            bp_pr_issue.create_comment(
                '[automatic post] Jenkins, test this please.')

        print("Done.")
        print(bp_pr.html_url)

    def is_branch_exist(self, branch_name, workd):
        try:
            git_out(['rev-parse', '--verify', branch_name], cd=workd)
        except GitCommandError:
            return False
        return True

    def parse_log_message(self, commit):
        msg = self.repo.get_commit(commit).commit.message
        head_msg, _, title = msg.split('\n')[:3]
        pattern = r'^Merge pull request #(?P<pr_num>[0-9]+) from [^ /]+/(?P<branch_name>[^ ]+)$'  # NOQA
        m = re.match(pattern, head_msg)
        if m is None:
            raise GracefulError('Invalid log message: {}'.format(head_msg))
        pr_num = int(m.group('pr_num'))
        branch_name = m.group('branch_name')
        return pr_num, branch_name, title


def main(args):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--repo', required=True, choices=('chainer', 'cupy'),
        help='chainer or cupy')
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
        '--branch', type=str, default='v8',
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
    args = parser.parse_args(args)

    target_branch = args.branch
    if args.repo == 'chainer':
        organ_name, repo_name = 'chainer', 'chainer'
    elif args.repo == 'cupy':
        organ_name, repo_name = 'cupy', 'cupy'
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

    app.run(
        pr_num=args.pr,
        sha=args.sha,
        target_branch=target_branch,
        is_continue=args.is_continue,
        abort_before_push=args.abort_before_push,
        https=args.https)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    try:
        main(sys.argv[1:])
    except NoActionRequiredError as e:
        sys.stderr.write('No action required: {}\n'.format(e))
    except GracefulError as e:
        sys.stderr.write('Error: {}\n'.format(e))
        sys.exit(1)
    sys.exit(0)
