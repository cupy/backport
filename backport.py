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
import logging

import github


logger = logging.getLogger(__name__)


class GracefulError(Exception):
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
        if delete == True or (delete == 'on-success' and succeeded):
            shutil.rmtree(temp_dir, ignore_errors=True)


class GitWorkDir:
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

    def run(self, pr_num, target_branch, is_continue, abort_before_push):
        assert isinstance(pr_num, int)
        assert pr_num >= 1
        assert re.match(r'^\w+$', target_branch)
        assert isinstance(is_continue, bool)
        assert isinstance(abort_before_push, bool)

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
        labels = set(label.name for label in pr_issue.labels)
        if 'to-be-backported' not in labels:
            raise GracefulError(
                'PR #{} doesn\'t have \'to-be-backported\' label.'.format(pr_num))
        labels.remove('to-be-backported')
        labels.discard('reviewer-team')
        labels = set(_ for _ in labels if not _.startswith('st:'))

        #

        user_name = self.user_name
        origin_remote = 'git@github.com:{}/{}'.format(self.organ_name, self.repo_name)
        user_remote = 'git@github.com:{}/{}'.format(self.user_name, self.repo_name)
        bp_branch_name = 'bp-{}-{}'.format(pr_num, branch_name)

        if self.debug or abort_before_push:
            delete = False
        else:
            delete = 'on-success'

        with GitWorkDir(use_cwd=is_continue, prefix='bp-', delete=delete) as workdir:
            workd = workdir.workdir
            print(workd)

            git_ = lambda cmd: git(cmd, cd=workd)

            if not is_continue:
                #-------------------
                # Clone target repo
                #-------------------
                git(['clone', '--branch', target_branch, origin_remote, workd])

                #-------------------
                # Fetch user remote
                #-------------------
                git_(['remote', 'add', user_name, user_remote])
                git_(['fetch', '--depth', '1', user_name])

                #------------------------
                # Create backport branch
                #------------------------

                git_(['checkout', '-b', bp_branch_name])
                git_(['fetch', 'origin', merge_commit_sha])
                try:
                    git_(['cherry-pick', '-m1', merge_commit_sha])
                except GitCommandError as e:
                    sys.stderr.write(
                        'Cherry-pick failed.\n' +
                        'Working tree is saved at: {}\n'.format(workd) +
                        'Go to the working tree, resolve the conflict and type `git cherry-pick --continue`,\n'
                        'then run this script with --continue option.\n')
                    raise GracefulError('Not cleanly cherry-picked')

            if abort_before_push:
                sys.stderr.write(
                    'Backport procedure has been aborted due to configuration.\n' +
                    'Working tree is saved at: {}\n'.format(workd) +
                    'Go to the working tree, make a modification and commits,\n'
                    'then run this script with --continue option.\n')
                raise GracefulError('Aborted')

            # Push to user remote
            git_(['push', user_name, 'HEAD'])

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
            bp_pr_issue.set_labels('backport', *list(labels))

        #-----
        print("Done.")
        print(bp_pr.html_url)

    def is_branch_exist(self, branch_name, workd):
        try:
            git_out(['rev-parse', '--verify', branch_name], cd=workd)
        except GitCommandError as e:
            return False
        return True

    def parse_log_message(self, commit):
        msg = self.repo.get_commit(commit).commit.message
        head_msg, _, title = msg.split('\n')[:3]
        m = re.match(r'^Merge pull request #(?P<pr_num>[0-9]+) from [^ /]+/(?P<branch_name>[^ ]+)$', head_msg)
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
    parser.add_argument('--continue', action='store_true', dest='is_continue', help='Continues the process suspended by conflict situation.')
    parser.add_argument('--abort-before-push', action='store_true',
                        help='Abort the procedure before making an push. Useful if you want to make some modification to the backport branch. Use --continue to make an actual push after making modification.')
    args = parser.parse_args(args)

    if args.repo == 'chainer':
        target_branch = 'v4'
        organ_name, repo_name = 'chainer', 'chainer'
    elif args.repo == 'cupy':
        target_branch = 'v4'
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
        target_branch = target_branch,
        is_continue=args.is_continue,
        abort_before_push=args.abort_before_push)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    try:
        main(sys.argv[1:])
    except GracefulError as e:
        sys.stderr.write('Error: {}\n'.format(e))
        sys.exit(1)
