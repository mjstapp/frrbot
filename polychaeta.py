#!/usr/bin/env python3
#
# Deps:
# pip3 install flask PyGithub apscheduler sqlalchemy dateparser
#
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from flask import Response
from flask import request
from github import Github
from github import GithubException
from hmac import HMAC
from werkzeug.exceptions import BadRequest
import dateparser
import datetime
import hmac
import json
import os
import re
import yaml

# Global data ------------------------------------------------------------------
autoclosemsg = "This issue will be automatically closed in one week unless there is further activity."
noautoclosemsg = "This issue will no longer be automatically closed."
triggerlabel = "autoclose"

pr_greeting_msg = "Thanks for your contribution to FRR!\n\n"
pr_warn_signoff_msg = "* One of your commits is missing a `Signed-off-by` line; we can't accept your contribution until all of your commits have one\n"
pr_warn_commit_msg = (
    "* One of your commits has an improperly formatted commit message\n"
)
pr_guidelines_ref_msg = "\nIf you are a new contributor to FRR, please see our [contributing guidelines](http://docs.frrouting.org/projects/dev-guide/en/latest/workflow.html#coding-practices-style).\n"

# Scheduler functions ----------------------------------------------------------


def close_issue(rn, num):
    app.logger.warning("Closing issue #{}".format(num))
    repo = g.get_repo(rn)
    issue = repo.get_issue(num)
    issue.edit(state="closed")
    try:
        issue.remove_from_labels(triggerlabel)
    except GithubException as e:
        pass


def schedule_close_issue(issue, when):
    """
    Schedule an issue to be automatically closed on a certain date.

    :param github.Issue.Issue issue: issue to close
    :param datetime.datetime when: When to close the issue
    """
    reponame = issue.repository.full_name
    issuenum = issue.number
    issueid = "{}@@@{}".format(reponame, issuenum)
    app.logger.warning(
        "[-] Scheduling issue #{} for autoclose (id: {})".format(issuenum, issueid)
    )
    scheduler.add_job(
        close_issue,
        run_date=when,
        args=[reponame, issuenum],
        id=issueid,
        replace_existing=True,
    )


def cancel_close_issue(issue):
    """
    Dechedule an issue to be automatically closed on a certain date.

    :param github.Issue.Issue issue: issue to cancel
    """
    reponame = issue.repository.full_name
    issuenum = issue.id
    issueid = "{}@@@{}".format(reponame, issuenum)
    app.logger.warning("[-] Descheduling issue #{} for closing".format(issuenum))
    scheduler.remove_job(issueid)


# Module init ------------------------------------------------------------------

print("[+] Loading config")

with open("config.yaml", "r") as conffile:
    conf = yaml.safe_load(conffile)
    whsec = conf["gh_webhook_secret"]
    auth = conf["gh_auth_token"]

print("[+] Github auth token: {}".format(auth))
print("[+] Github webhook secret: {}".format(whsec))

# Initialize GitHub API
g = Github(auth)
my_user = g.get_user().login
print("[+] Initialized GitHub API object")

# Initialize scheduler
jobstores = {"default": SQLAlchemyJobStore(url="sqlite:///jobs.sqlite")}
scheduler = BackgroundScheduler(jobstores=jobstores)
scheduler.start()
print("[+] Initialized scheduler")
print("[+] Current jobs:")
scheduler.print_jobs()

# Initialize Flask app
app = Flask(__name__)
print("[+] Initialized Flask app")

# Webhook handlers -------------------------------------------------------------


def issue_labeled(j):
    reponame = j["repository"]["full_name"]
    issuenum = j["issue"]["number"]

    repo = g.get_repo(reponame)
    issue = repo.get_issue(issuenum)

    if j["label"]["name"] == triggerlabel:
        closedate = datetime.datetime.now() + datetime.timedelta(weeks=1)
        schedule_close_issue(issue, closedate)
        issue.create_comment(autoclosemsg)

    return Response("OK", 200)


def issue_comment_created(j):
    reponame = j["repository"]["full_name"]
    issuenum = j["issue"]["number"]

    issueid = "{}@@@{}".format(reponame, issuenum)
    repo = g.get_repo(reponame)
    issue = repo.get_issue(issuenum)

    # decide if this comment is directed at us
    body = j["comment"]["body"]
    sender = j["sender"]["login"]
    perm = repo.get_collaborator_permission(sender)
    trigger = "@{} autoclose".format(my_user)

    app.logger.warning("Trigger: {}".format(trigger))
    app.logger.warning("Perm: {}".format(perm))

    if trigger.lower() in body.lower() and perm == "admin":
        closedate = dateparser.parse(body.partition("autoclose")[2])
        if closedate is not None and closedate > datetime.datetime.now():
            schedule_close_issue(issue, closedate)
            issue.add_to_labels("autoclose")
            issue.get_comment(j["comment"]["id"]).create_reaction("+1")
    elif scheduler.get_job(issueid) is not None:
        scheduler.remove_job(issueid)
        issue.remove_from_labels(triggerlabel)
        issue.create_comment(noautoclosemsg)

    return Response("OK", 200)


def pull_request_opened(j):
    """
    Handle a pull request being opened.

    This function checks each commit's message for proper summary line
    formatting, Signed-off-by, and modified directories. If it finds formatting
    issues or missing Signed-off-by, it leaves a review on the PR asking for
    the problem to be fixed.

    Also, modified directories are extracted from commits and used to apply the
    corresponding topic labels.
    """
    reponame = j["repository"]["full_name"]

    repo = g.get_repo(reponame)
    pr = repo.get_pull(j["number"])
    commits = pr.get_commits()

    warn_bad_msg = False
    warn_signoff = False
    labels = set()

    # apply labels based on commit messages
    label_map = {
        "babeld": "babel",
        "bfdd": "bfd",
        "bgpd": "bgp",
        "debian": "packaging",
        "doc": "documentation",
        "docker": "docker",
        "eigrpd": "eigrp",
        "fpm": "fpm",
        "isisd": "isis",
        "ldpd": "ldp",
        "lib": "libfrr",
        "nhrpd": "nhrp",
        "ospf6d": "ospfv3",
        "ospfd": "ospf",
        "pbrd": "pbr",
        "pimd": "pim",
        "pkgsrc": "packaging",
        "python": "clippy",
        "redhat": "packaging",
        "ripd": "rip",
        "ripngd": "ripng",
        "sharpd": "sharp",
        "snapcraft": "packaging",
        "solaris": "packaging",
        "staticd": "staticd",
        "tests": "tests",
        "tools": "tools",
        "vtysh": "vtysh",
        "vrrp": "vrrp",
        "watchfrr": "watchfrr",
        "yang": "yang",
        "zebra": "zebra",
        # files
        "configure.ac": "build",
        "Makefile.am": "build",
        "bootstrap.sh": "build",
    }

    for commit in commits:
        msg = commit.commit.message

        if msg.startswith("Revert") or msg.startswith("Merge"):
            continue

        match = re.match(r"^([^:\n]+):", msg)
        if match:
            lbls = map(lambda x: x.strip(), match.groups()[0].split(","))
            lbls = filter(lambda x: x in label_map, lbls)
            lbls = set(map(lambda x: label_map[x], lbls))
            labels = labels | lbls
        else:
            warn_bad_msg = True

        if not "Signed-off-by:" in msg:
            warn_signoff = True

    if warn_bad_msg or warn_signoff:
        comment = pr_greeting_msg
        comment += pr_warn_commit_msg if warn_bad_msg else ""
        comment += pr_warn_signoff_msg if warn_signoff else ""
        comment += pr_guidelines_ref_msg
        pr.create_review(body=comment, event="REQUEST_CHANGES")

    if labels:
        pr.set_labels(*labels)

    return Response("OK", 200)


# API handler map
# {
#   'event1': {
#     'action1': ev1_action1_handler,
#     'action2': ev1_action2_handler,
#     ...
#   }
#   'event2': {
#     'action1': ev2_action1_handler,
#     'action2': ev2_action2_handler,
#     ...
#   }
# }
event_handlers = {
    "issues": {"labeled": issue_labeled},
    "issue_comment": {"created": issue_comment_created},
    "pull_request": {"opened": pull_request_opened},
}


def handle_webhook(request):
    try:
        evtype = request.headers["X_GITHUB_EVENT"]
    except KeyError as e:
        app.logger.warning("No X-GitHub-Event header...")
        return Response("No X-GitHub-Event header", 400)

    app.logger.warning("Handling webhook '{}'".format(evtype))

    try:
        event = event_handlers[evtype]
    except KeyError as e:
        app.logger.warning("Unknown event '{}'".format(evtype))
        return Response("OK", 200)

    try:
        j = request.get_json()
    except BadRequest as e:
        app.logger.warning("Could not parse payload as JSON")
        return Response("Bad JSON", 400)

    try:
        action = j["action"]
    except KeyError as e:
        app.logger.warning("No action for event '{}'".format(evtype))
        return Response("OK", 200)

    try:
        handler = event_handlers[evtype][action]
    except KeyError as e:
        app.logger.warning("No handler for action '{}'".format(action))
        return Response("OK", 200)

    try:
        sender = j["sender"]["login"]
        reponame = j["repository"]["full_name"]
        repo = g.get_repo(reponame)
        if sender == my_user:
            app.logger.warning("[-] Ignoring event triggered by me")
            return Response("OK", 200)
    except KeyError as e:
        pass

    app.logger.warning("Handling action '{}' on event '{}'".format(action, evtype))
    return handler(j)


# Flask hooks ------------------------------------------------------------------


def gh_sig_valid(req):
    mydigest = "sha1=" + HMAC(bytes(whsec, "utf8"), req.get_data(), "sha1").hexdigest()
    ghdigest = req.headers["X_HUB_SIGNATURE"]
    comp = hmac.compare_digest(ghdigest, mydigest)
    app.logger.warning("Request: mine = {}, theirs = {}".format(mydigest, ghdigest))
    return comp


@app.route("/payload", methods=["GET", "POST"])
def parse_payload():
    try:
        if not gh_sig_valid(request):
            return Response("Unauthorized", 401)
    except:
        return Response("Unauthorized", 401)

    app.logger.warning("Got {}".format(request.method))

    if request.method == "POST":
        return handle_webhook(request)
    else:
        return Response("OK", 200)
