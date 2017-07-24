#!/usr/bin/env python

import argparse
import subprocess
import re
import random
import sys
import contextlib
import shutil
import tempfile
import os

import github


class GracefulError(RuntimeError):
    pass


@contextlib.contextmanager
def tempdir(delete=True, **kwargs):
    temp_dir = tempfile.mkdtemp(**kwargs)
    try:
        yield temp_dir
    finally:
        if delete:
            shutil.rmtree(temp_dir, ignore_errors=True)


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
        raise RuntimeError("Git command failed with code {}".format(proc.returncode))
    if stdout is not None:
        stdout = stdout.decode('utf8')
    print('')
    return stdout


def git_out(args):
    stdout = git(args, stdout=subprocess.PIPE)
    return stdout.rstrip()


def random_string(n):
    chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    return ''.join(random.choice(chars) for _ in range(n))


class App:
    def __init__(self, token, organ_name, repo_name, debug=False):
        assert re.match(r'^\w+$', organ_name)
        assert re.match(r'^\w+$', repo_name)
        self.repo_name = repo_name
        self.organ_name = organ_name
        self.g = github.Github(token)
        self.repo = self.g.get_repo('{}/{}'.format(organ_name, repo_name))
        self.user_name = self.g.get_user().login
        self.debug = debug

    def run(self, pr_num, target_branch):
        assert isinstance(pr_num, int)
        assert pr_num >= 1
        assert re.match(r'^\w+$', target_branch)

        #----------------------------------------------
        # Get information of the original pull request
        #----------------------------------------------
        pr = self.repo.get_pull(pr_num)
        if not pr.merged:
            raise GracefulError('PR #{} is not merged'.format(pr_num))
        merge_commit_sha = pr.merge_commit_sha
        _, branch_name, _ = self.parse_log_message(merge_commit_sha)
        title = pr.title

        pr_issue = self.repo.get_issue(pr_num)
        labels = [label.name for label in pr_issue.labels]
        if 'to-be-backported' not in labels:
            raise GracefulError(
                'PR #{} doesn\'t have \'to-be-backported\' label.'.format(pr_num))
        labels.remove('to-be-backported')

        #

        user_name = self.user_name
        origin_remote = 'git@github.com:{}/{}'.format(self.organ_name, self.repo_name)
        user_remote = 'git@github.com:{}/{}'.format(self.user_name, self.repo_name)

        with tempdir(prefix='bp-', delete=not self.debug) as tempd:
            print(tempd)
            workd = os.path.join(tempd, 'workdir')

            #-------------------
            # Clone target repo
            #-------------------
            git(['clone', '--branch', target_branch, origin_remote, workd])

            git_ = lambda cmd: git(cmd, cd=workd)

            #-------------------
            # Fetch user remote
            #-------------------
            git_(['remote', 'add', user_name, user_remote])
            git_(['fetch', '--depth', '1', user_remote])

            #------------------------
            # Create backport branch
            #------------------------
            tmp_branch_name = 'bp-tmp-{}'.format(random_string(20))
            bp_branch_name = 'bp-{}'.format(branch_name)

            git_(['checkout', '-b', tmp_branch_name])
            git_(['fetch', 'origin', merge_commit_sha])
            git_(['cherry-pick', '-m1', merge_commit_sha])

            # Push to user remote
            git_(['push', user_name, 'HEAD:{}'.format(bp_branch_name)])

            #------------------------------
            # Create backport pull request
            #------------------------------
            print("Creating a pull request.")
            bp_pr = self.repo.create_pull(
                title = '[backport] {}'.format(title),
                head = '{}:{}'.format(self.user_name, bp_branch_name),
                base = target_branch,
                body = 'Backport of #{}'.format(pr_num))
            bp_pr_issue = self.repo.get_issue(bp_pr.number)
            bp_pr_issue.set_labels('backport', *labels)

        #-----
        print("Done.")
        print(bp_pr.html_url)

    def parse_log_message(self, commit):
        msg = self.repo.get_commit(commit).commit.message
        head_msg, _, title = msg.split('\n')[:3]
        m = re.match(r'^Merge pull request #(?P<pr_num>[0-9]+) from [^ /]+/(?P<branch_name>[^ /]+)$', head_msg)
        if m is None:
            raise GracefulError('Invalid log message: {}'.format(head_msg))
        pr_num = int(m.group('pr_num'))
        branch_name = m.group('branch_name')
        return pr_num, branch_name, title


def main(args):
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True, choices=('chainer', 'cupy'), help='chainer or cupy')
    parser.add_argument('--token', required=True, help='GitHub access token.')
    parser.add_argument('--pr', required=True, type=int, help='The original PR number to be backported.')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args(args)

    if args.repo == 'chainer':
        target_branch = 'v2'
        organ_name, repo_name = 'chainer', 'chainer'
    elif args.repo == 'cupy':
        target_branch = 'v1'
        organ_name, repo_name = 'cupy', 'cupy'
    else:
        assert False

    github_token = args.token

    if args.debug:
        github.enable_console_debug_logging()


    app = App(
        github_token,
        organ_name = organ_name,
        repo_name = repo_name)

    app.run(
        pr_num = args.pr,
        target_branch = target_branch)

if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except GracefulError as e:
        sys.stderr.write('Error: {}\n'.format(e))
        sys.exit(1)
