"""Proof of concept: browse Azure DevOps repos via the REST API.

Examples:
    python adobrowser.py repos
    python adobrowser.py branches MyRepo
    python adobrowser.py ls MyRepo /src
    python adobrowser.py cat MyRepo /README.md
"""

import argparse
import configparser
import os
import sys
from pathlib import Path

import requests

API_VERSION = "7.1"
# Azure DevOps resource ID for Entra ID tokens (fixed, defined by Microsoft)
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798/.default"

DEFAULT_CONFIG = Path(__file__).with_name("config.ini")


def load_config(path=DEFAULT_CONFIG):
    """Read organization and project from the INI file ([azure_devops] section).

    The environment variables AZURE_DEVOPS_ORG / AZURE_DEVOPS_PROJECT take precedence.
    """
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    section = parser["azure_devops"] if parser.has_section("azure_devops") else {}
    return {
        "org": os.environ.get("AZURE_DEVOPS_ORG", section.get("organization")),
        "project": os.environ.get("AZURE_DEVOPS_PROJECT", section.get("project")),
    }


def make_session(auth_mode="auto"):
    """Create an authenticated requests.Session.

    auto:    PAT from AZURE_DEVOPS_PAT, otherwise browser login
    pat:     PAT only
    browser: browser login (azure-identity)
    azcli:   token from `az login` (azure-identity)
    """
    session = requests.Session()
    pat = os.environ.get("AZURE_DEVOPS_PAT")

    if auth_mode == "pat" or (auth_mode == "auto" and pat):
        if not pat:
            sys.exit("AZURE_DEVOPS_PAT is not set.")
        session.auth = ("", pat)  # Basic auth: empty username + PAT
        return session

    try:
        from azure.identity import AzureCliCredential, InteractiveBrowserCredential
    except ImportError:
        sys.exit("No PAT set and azure-identity is missing: pip install azure-identity")

    cred = AzureCliCredential() if auth_mode == "azcli" else InteractiveBrowserCredential()
    token = cred.get_token(ADO_RESOURCE).token
    session.headers["Authorization"] = f"Bearer {token}"
    return session


class AzureDevOpsClient:
    def __init__(self, session, org, project):
        self.session = session
        self.base = f"https://dev.azure.com/{org}/{project}/_apis/git"

    def _get(self, path, **params):
        params["api-version"] = API_VERSION
        r = self.session.get(f"{self.base}{path}", params=params)
        if r.status_code in (401, 203):
            # 203 = login page instead of JSON, typical for a wrong/expired token
            raise PermissionError("Authentication failed (check your token).")
        r.raise_for_status()
        return r

    def list_repos(self):
        return self._get("/repositories").json()["value"]

    def list_branches(self, repo):
        refs = self._get(f"/repositories/{repo}/refs", filter="heads/").json()["value"]
        return [ref["name"].removeprefix("refs/heads/") for ref in refs]

    def _version(self, branch):
        if not branch:
            return {}
        return {"versionDescriptor.version": branch, "versionDescriptor.versionType": "branch"}

    def list_items(self, repo, path="/", branch=None):
        items = self._get(
            f"/repositories/{repo}/items",
            scopePath=path,
            recursionLevel="OneLevel",
            **self._version(branch),
        ).json()["value"]
        # The first entry is the folder itself
        return [i for i in items if i["path"].rstrip("/") != path.rstrip("/")]

    def get_file(self, repo, path, branch=None):
        return self._get(
            f"/repositories/{repo}/items",
            path=path,
            includeContent="true",
            **self._version(branch),
        ).json()["content"]


def main(argv=None):
    p = argparse.ArgumentParser(description="Browse Azure DevOps repos")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to config.ini")
    p.add_argument("--org", help="organization (overrides config.ini)")
    p.add_argument("--project", help="project (overrides config.ini)")
    p.add_argument("--auth", choices=["auto", "pat", "browser", "azcli"], default="auto")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("repos", help="list repos in the project")
    b = sub.add_parser("branches", help="list branches of a repo")
    b.add_argument("repo")
    ls = sub.add_parser("ls", help="list folder contents")
    ls.add_argument("repo")
    ls.add_argument("path", nargs="?", default="/")
    ls.add_argument("--branch")
    cat = sub.add_parser("cat", help="print a file")
    cat.add_argument("repo")
    cat.add_argument("path")
    cat.add_argument("--branch")

    args = p.parse_args(argv)
    config = load_config(args.config)
    org = args.org or config["org"]
    project = args.project or config["project"]
    if not org or not project:
        sys.exit(f"Organization and project missing: set them in {args.config} or pass --org/--project.")
    client = AzureDevOpsClient(make_session(args.auth), org, project)

    if args.cmd == "repos":
        for repo in sorted(client.list_repos(), key=lambda r: r["name"].lower()):
            branch = repo.get("defaultBranch", "").removeprefix("refs/heads/")
            print(f"{repo['name']:<40} {branch:<15} {repo['webUrl']}")
    elif args.cmd == "branches":
        for name in client.list_branches(args.repo):
            print(name)
    elif args.cmd == "ls":
        for item in client.list_items(args.repo, args.path, args.branch):
            suffix = "/" if item.get("isFolder") else ""
            print(item["path"] + suffix)
    elif args.cmd == "cat":
        print(client.get_file(args.repo, args.path, args.branch), end="")


if __name__ == "__main__":
    main()
