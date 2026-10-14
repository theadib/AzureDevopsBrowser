"""Proof of concept: browse Azure DevOps repos via the REST API.

Examples:
    python adobrowser.py repos
    python adobrowser.py branches MyRepo
    python adobrowser.py ls MyRepo /src
    python adobrowser.py cat MyRepo /README.md
"""

import argparse
import base64
import configparser
import os
import subprocess
import sys
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

API_VERSION = "7.1"
# Azure DevOps resource ID for Entra ID tokens (fixed, defined by Microsoft)
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798/.default"

DEFAULT_CONFIG = Path(__file__).with_name("config.ini")
# Account info of the last browser login (no secrets; the tokens live in the MSAL cache)
AUTH_RECORD = Path.home() / ".adobrowser" / "auth_record.json"

LastCommit = namedtuple("LastCommit", "date branch author email")
WORK_ITEM_FIELDS = [
    "System.Id", "System.WorkItemType", "System.State", "System.Title",
    "System.ChangedDate", "System.ChangedBy", "System.AssignedTo", "System.AreaPath",
]
# Local bare clones (commit history only) used by the "authors" command
GIT_CACHE = Path.home() / ".adobrowser" / "git"


def load_config(path=DEFAULT_CONFIG):
    """Read organization and project from the INI file ([azure_devops] section).

    The environment variables AZURE_DEVOPS_ORG / AZURE_DEVOPS_PROJECT take precedence.
    """
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    section = parser["azure_devops"] if parser.has_section("azure_devops") else {}
    aliases = parser["aliases"] if parser.has_section("aliases") else {}
    return {
        "org": os.environ.get("AZURE_DEVOPS_ORG", section.get("organization")),
        "project": os.environ.get("AZURE_DEVOPS_PROJECT", section.get("project")),
        # Commit author name or email (lowercase) -> common name
        "aliases": {key.lower(): value for key, value in aliases.items()},
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
        from azure.identity import AzureCliCredential
    except ImportError:
        sys.exit("No PAT set and azure-identity is missing: pip install azure-identity")

    cred = AzureCliCredential() if auth_mode == "azcli" else browser_credential()
    token = cred.get_token(ADO_RESOURCE).token
    session.headers["Authorization"] = f"Bearer {token}"
    return session


def browser_credential(record_path=AUTH_RECORD):
    """Browser login with a persistent token cache.

    The first run opens the browser and saves the account in record_path; later
    runs get their tokens silently from the cache until the refresh token expires.
    """
    from azure.identity import (
        AuthenticationRecord,
        InteractiveBrowserCredential,
        TokenCachePersistenceOptions,
    )

    # Uses the system keyring if available, otherwise a plaintext file in ~/.IdentityService
    cache = TokenCachePersistenceOptions(name="adobrowser", allow_unencrypted_storage=True)
    record = None
    if record_path.exists():
        record = AuthenticationRecord.deserialize(record_path.read_text())

    cred = InteractiveBrowserCredential(cache_persistence_options=cache, authentication_record=record)
    if record is None:
        record = cred.authenticate(scopes=[ADO_RESOURCE])
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(record.serialize())
    return cred


def git_env(session):
    """Environment that lets git authenticate like the requests session.

    The Authorization header goes in via GIT_CONFIG_* variables (git >= 2.31),
    so the token does not appear on the command line.
    """
    if session.auth:
        user, pat = session.auth
        header = "Basic " + base64.b64encode(f"{user}:{pat}".encode()).decode()
    else:
        header = session.headers.get("Authorization", "")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if header:
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: {header}")
    return env


def git_log_authors(url, path, env):
    """Clone or update a blob-less bare repo at path; return [(name, email)] per commit."""
    def git(*args):
        return subprocess.run(["git", *args], env=env, check=True, capture_output=True, text=True).stdout

    if (path / "HEAD").exists():
        git("-C", str(path), "fetch", "--prune", "--quiet", "origin")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--bare", "--filter=blob:none", "--quiet", url, str(path))
        # A bare clone has no fetch refspec; add one so later fetches update all branches
        git("-C", str(path), "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
    out = git("-C", str(path), "log", "--branches", "--format=%an%x1f%ae")
    return [tuple(line.split("\x1f", 1)) for line in out.splitlines()]


class AzureDevOpsClient:
    def __init__(self, session, org, project):
        self.session = session
        self.org = org
        self.project = project
        self.base = f"https://dev.azure.com/{org}/{project}/_apis/git"
        self.wit = f"https://dev.azure.com/{org}/{project}/_apis/wit"
        self.project_url = f"https://dev.azure.com/{org}/{project}"

    def _get(self, path, **params):
        return self._get_url(f"{self.base}{path}", **params)

    def _get_url(self, url, **params):
        return self._request("GET", url, params=params)

    def _post_url(self, url, body):
        return self._request("POST", url, params={}, json=body)

    def _request(self, method, url, params, **kwargs):
        params["api-version"] = API_VERSION
        r = self.session.request(method, url, params=params, **kwargs)
        if r.status_code in (401, 203):
            # 203 = login page instead of JSON, typical for a wrong/expired token
            raise PermissionError("Authentication failed (check your token).")
        r.raise_for_status()
        return r

    def list_repos(self):
        return self._get("/repositories").json()["value"]

    def last_commit(self, repo):
        """Return the LastCommit of the newest commit across all branches, or None.

        Uses the branch statistics, which contain the head commit of every branch.
        """
        stats = self._get(f"/repositories/{repo}/stats/branches").json()["value"]
        heads = [
            LastCommit(
                datetime.fromisoformat(b["commit"]["committer"]["date"].replace("Z", "+00:00")),
                b["name"],
                b["commit"].get("author", {}).get("name", ""),
                b["commit"].get("author", {}).get("email", ""),
            )
            for b in stats
        ]
        return max(heads, key=lambda h: h.date, default=None)

    def list_repos_by_commit(self, workers=8):
        """Return [(repo, LastCommit or None)], newest commit first."""
        repos = self.list_repos()

        def fetch(repo):
            # Disabled and empty repos have no branches to query
            if repo.get("isDisabled") or not repo.get("defaultBranch"):
                return repo, None
            try:
                return repo, self.last_commit(repo["id"])
            except requests.HTTPError:
                return repo, None

        with ThreadPoolExecutor(workers) as pool:
            results = list(pool.map(fetch, repos))
        with_commit = sorted((r for r in results if r[1]), key=lambda r: r[1].date, reverse=True)
        without = sorted((r for r in results if not r[1]), key=lambda r: r[0]["name"].lower())
        return with_commit + without

    def list_authors(self, repo=None, cache_dir=GIT_CACHE, workers=4, progress=None):
        """Count commits per (author name, email) across repos, using git.

        Each repo is kept as a bare, blob-less clone in cache_dir (commits and
        trees only, no file contents) and updated with `git fetch` on later runs.
        All branches are read. Returns {(name, email): commit count}.
        """
        repos = [r for r in self.list_repos() if repo in (None, r["name"], r["id"])]
        if repo and not repos:
            raise ValueError(f"Repo not found: {repo}")
        # Disabled and empty repos have nothing to clone
        repos = [r for r in repos if not r.get("isDisabled") and r.get("defaultBranch")]
        env = git_env(self.session)
        done = 0

        def fetch(r):
            path = Path(cache_dir) / self.org / self.project / f"{r['id']}.git"
            try:
                log = git_log_authors(r["remoteUrl"], path, env)
            except subprocess.CalledProcessError as e:
                print(f"warning: {r['name']}: git failed: {e.stderr.strip()}", file=sys.stderr)
                log = []
            return r, log

        counts = {}
        with ThreadPoolExecutor(workers) as pool:
            for r, log in pool.map(fetch, repos):
                done += 1
                if progress:
                    progress(f"[{done}/{len(repos)}] {r['name']}")
                for key in log:
                    counts[key] = counts.get(key, 0) + 1
        return counts

    def changed_work_items(self, days=14, area=None, batch_size=200):
        """Return work items of the project changed in the last `days` days, newest first.

        With `area`, only items in that area path or below it are returned.
        One WIQL query returns the IDs, then the fields are read in batches of 200.
        """
        area_filter = ""
        if area:
            # WIQL string literals escape a single quote by doubling it
            area_filter = " AND [System.AreaPath] UNDER '{}'".format(area.replace("'", "''"))
        query = (
            "SELECT [System.Id] FROM WorkItems"
            " WHERE [System.TeamProject] = @project"
            f" AND [System.ChangedDate] >= @today - {int(days)}"
            f"{area_filter}"
            " ORDER BY [System.ChangedDate] DESC"
        )
        ids = [w["id"] for w in self._post_url(f"{self.wit}/wiql", {"query": query}).json()["workItems"]]
        items = []
        for i in range(0, len(ids), batch_size):
            body = {"ids": ids[i:i + batch_size], "fields": WORK_ITEM_FIELDS}
            items += self._post_url(f"{self.wit}/workitemsbatch", body).json()["value"]
        return sorted(items, key=lambda w: w["fields"]["System.ChangedDate"], reverse=True)

    def team_boards(self, workers=8):
        """Return the board setup of every team in the project.

        [{"team": name, "areas": [(area path, include children)],
          "levels": [(board name, backlog type, {work item types})]}]
        Teams whose team field is not the area path are skipped.
        """
        teams = self._get_url(
            f"https://dev.azure.com/{self.org}/_apis/projects/{self.project}/teams", **{"$top": 1000}
        ).json()["value"]

        def fetch(team):
            base = f"{self.project_url}/{team['id']}/_apis/work"
            fields = self._get_url(f"{base}/teamsettings/teamfieldvalues").json()
            if fields["field"]["referenceName"] != "System.AreaPath":
                return None
            # The team's backlog levels already reflect its "bugs on backlog/taskboard" setting
            backlogs = self._get_url(f"{base}/backlogs").json()["value"]
            return {
                "team": team["name"],
                "areas": [(v["value"], v["includeChildren"]) for v in fields["values"]],
                "levels": [
                    ("Taskboard" if b.get("type") == "task" else b["name"], b.get("type"),
                     {t["name"] for t in b["workItemTypes"]})
                    for b in backlogs if not b.get("isHidden")
                ],
            }

        with ThreadPoolExecutor(workers) as pool:
            return sorted((t for t in pool.map(fetch, teams) if t), key=lambda t: t["team"].lower())


    def display_name(self, email):
        """Return the Azure DevOps display name of the user with this email, or None."""
        r = self._get_url(
            f"https://vssps.dev.azure.com/{self.org}/_apis/identities",
            searchFilter="General",
            filterValue=email,
            queryMembership="None",
        )
        users = r.json().get("value", [])
        return users[0].get("providerDisplayName") if users else None

    def resolve_authors(self, commits, aliases=None, workers=8):
        """Map each commit author to a common name.

        Order: [aliases] by email, [aliases] by name, Azure DevOps identity by email,
        otherwise the git author name. Returns {(author, email): name}.
        """
        aliases = aliases or {}
        authors = {(c.author, c.email) for c in commits if c}
        emails = {e.lower() for a, e in authors if e and e.lower() not in aliases and a.lower() not in aliases}

        def lookup(email):
            try:
                return email, self.display_name(email)
            except (requests.HTTPError, PermissionError):
                # e.g. a PAT without the "Identity (Read)" scope
                return email, None

        with ThreadPoolExecutor(workers) as pool:
            identities = dict(pool.map(lookup, emails))
        return {
            (author, email): aliases.get(email.lower()) or aliases.get(author.lower())
            or identities.get(email.lower()) or author
            for author, email in authors
        }

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


def boards_for(fields, teams):
    """Return ["Team: Board", ...] for the boards a work item shows up on."""
    area = fields.get("System.AreaPath", "").lower()
    kind = fields.get("System.WorkItemType")
    boards = []
    for team in teams:
        in_area = any(
            area == path.lower() or (children and area.startswith(path.lower() + "\\"))
            for path, children in team["areas"]
        )
        if in_area:
            boards += [f"{team['team']}: {name}" for name, _, types in team["levels"] if kind in types]
    return boards


def main(argv=None):
    p = argparse.ArgumentParser(description="Browse Azure DevOps repos")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to config.ini")
    p.add_argument("--org", help="organization (overrides config.ini)")
    p.add_argument("--project", help="project (overrides config.ini)")
    p.add_argument("--auth", choices=["auto", "pat", "browser", "azcli"], default="auto")
    sub = p.add_subparsers(dest="cmd", required=True)

    repos = sub.add_parser("repos", help="list repos in the project")
    repos.add_argument("--sort", choices=["name", "commit"], default="name",
                       help="sort by name or by newest commit in any branch (newest first)")
    au = sub.add_parser("authors", help="list all distinct commit author name/email combinations")
    au.add_argument("repo", nargs="?", help="only this repo (default: all repos)")
    wi = sub.add_parser("workitems", help="list work items changed recently")
    wi.add_argument("--days", type=int, default=14, help="changed within this many days (default: 14)")
    wi.add_argument("--area", help="only this area path and its sub-areas, e.g. 'MyProject\\Team A'")
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

    if args.cmd == "repos" and args.sort == "commit":
        results = client.list_repos_by_commit()
        names = client.resolve_authors([last for _, last in results], config["aliases"])
        for repo, last in results:
            if last:
                date, branch = last.date.strftime("%Y-%m-%d %H:%M"), last.branch
                author, email = names[last.author, last.email], last.email
            else:
                date, branch, author, email = "-", "", "", ""
            print(f"{repo['name']:<40} {date:<17} {branch:<25} {author:<25} {email:<35} {repo['webUrl']}")
    elif args.cmd == "repos":
        for repo in sorted(client.list_repos(), key=lambda r: r["name"].lower()):
            branch = repo.get("defaultBranch", "").removeprefix("refs/heads/")
            print(f"{repo['name']:<40} {branch:<15} {repo['webUrl']}")
    elif args.cmd == "authors":
        try:
            counts = client.list_authors(args.repo, progress=lambda msg: print(msg, file=sys.stderr))
        except ValueError as e:
            sys.exit(str(e))
        names = client.resolve_authors(
            [LastCommit(None, None, author, email) for author, email in counts], config["aliases"]
        )
        rows = sorted(counts.items(), key=lambda kv: (names[kv[0]].lower(), kv[0][1].lower(), kv[0][0].lower()))
        print(f"{'COMMON NAME':<25} {'GIT NAME':<25} {'EMAIL':<35} COMMITS")
        for (author, email), count in rows:
            print(f"{names[author, email]:<25} {author:<25} {email:<35} {count:>7}")
    elif args.cmd == "workitems":
        def person(field):
            # Identity fields are objects; unassigned items have no AssignedTo field
            return (field or {}).get("displayName", "")

        print(f"{'ID':>7}  {'CHANGED':<16}  {'TYPE':<12} {'STATE':<12} {'CHANGED BY':<22} {'ASSIGNED TO':<22} {'AREA PATH':<30} {'BOARD':<30} TITLE")
        items = client.changed_work_items(args.days, args.area)
        teams = client.team_boards() if items else []
        for item in items:
            f = item["fields"]
            board = ", ".join(boards_for(f, teams)) or "-"
            changed = datetime.fromisoformat(f["System.ChangedDate"][:19]).strftime("%Y-%m-%d %H:%M")
            print(f"{item['id']:>7}  {changed:<16}  {f['System.WorkItemType'][:12]:<12} {f['System.State'][:12]:<12} "
                  f"{person(f.get('System.ChangedBy'))[:22]:<22} {person(f.get('System.AssignedTo'))[:22]:<22} "
                  f"{f.get('System.AreaPath', ''):<30} {board:<30} {f['System.Title']}")
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
