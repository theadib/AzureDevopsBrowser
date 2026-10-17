"""Tests with simulated API responses (no access to Azure DevOps needed)."""

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import adobrowser


def fake_response(json_data, status=200):
    r = mock.Mock(status_code=status)
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock()
        self.client = adobrowser.AzureDevOpsClient(self.session, "my-org", "my-project")

    def test_list_repos_url_and_version(self):
        self.session.request.return_value = fake_response({"value": [{"name": "A"}]})
        self.assertEqual(self.client.list_repos(), [{"name": "A"}])
        url = self.session.request.call_args.args[1]
        params = self.session.request.call_args.kwargs["params"]
        self.assertEqual(url, "https://dev.azure.com/my-org/my-project/_apis/git/repositories")
        self.assertEqual(params["api-version"], "7.1")

    def test_list_repos_skips_blacklist(self):
        client = adobrowser.AzureDevOpsClient(self.session, "my-org", "my-project", {"Fork", "ID-2"})
        self.session.request.return_value = fake_response(
            {"value": [{"name": "A", "id": "id-1"}, {"name": "fork", "id": "id-3"}, {"name": "B", "id": "id-2"}]}
        )
        self.assertEqual(client.list_repos(), [{"name": "A", "id": "id-1"}])

    def test_commits_since_reads_recent_branches_once_per_commit(self):
        def head(name, date):
            return {"name": name, "commit": {"committer": {"date": date}, "author": {"name": "x", "email": "x"}}}

        def commit(cid, date, author="Jane"):
            return {"commitId": cid, "committer": {"date": date}, "comment": f"msg {cid}\nbody",
                    "author": {"name": author, "email": f"{author.lower()}@corp.com"}}

        self.session.request.side_effect = [
            fake_response({"value": [head("main", "2025-03-05T10:00:00Z"), head("dev", "2025-03-06T10:00:00Z"),
                                     head("old", "2024-01-01T10:00:00Z")]}),
            fake_response({"value": [commit("c3", "2025-03-06T10:00:00Z"), commit("c1", "2025-03-04T10:00:00Z")]}),
            fake_response({"value": [commit("c2", "2025-03-05T10:00:00Z", "Bob"), commit("c1", "2025-03-04T10:00:00Z")]}),
        ]
        since = adobrowser.parse_date("2025-03-01T00:00:00Z")
        commits = self.client.commits_since("R", since)
        self.assertEqual([(c.id, c.branch, c.author, c.message) for c in commits], [
            ("c3", "dev", "Jane", "msg c3"), ("c2", "main", "Bob", "msg c2"), ("c1", "dev", "Jane", "msg c1"),
        ])
        # Branch statistics, then dev (newest head) and main; "old" is not read
        calls = self.session.request.call_args_list
        self.assertEqual(len(calls), 3)
        params = calls[1].kwargs["params"]
        self.assertEqual(calls[1].args[1], "https://dev.azure.com/my-org/my-project/_apis/git/repositories/R/commits")
        self.assertEqual(params["searchCriteria.itemVersion.version"], "dev")
        self.assertEqual(params["searchCriteria.fromDate"], "2025-03-01T00:00:00Z")

    def test_branches_strip_prefix(self):
        self.session.request.return_value = fake_response(
            {"value": [{"name": "refs/heads/main"}, {"name": "refs/heads/feature/x"}]}
        )
        self.assertEqual(self.client.list_branches("R"), ["main", "feature/x"])

    def test_list_items_drops_folder_itself_and_passes_branch(self):
        self.session.request.return_value = fake_response(
            {"value": [{"path": "/src", "isFolder": True}, {"path": "/src/main.c"}]}
        )
        self.assertEqual(self.client.list_items("R", "/src", "dev"), [{"path": "/src/main.c"}])
        params = self.session.request.call_args.kwargs["params"]
        self.assertEqual(params["versionDescriptor.version"], "dev")
        self.assertEqual(params["recursionLevel"], "OneLevel")

    def test_last_commit_picks_newest_branch(self):
        self.session.request.return_value = fake_response({"value": [
            {"name": "main", "commit": {"committer": {"date": "2024-01-01T10:00:00Z"}}},
            {"name": "feature/x", "commit": {"committer": {"date": "2025-03-05T08:30:00Z"},
                                             "author": {"name": "Jane Doe", "email": "jane@example.com"}}},
        ]})
        date, branch, author, email = self.client.last_commit("id-1")
        self.assertEqual(branch, "feature/x")
        self.assertEqual((author, email), ("Jane Doe", "jane@example.com"))
        self.assertEqual(date.year, 2025)
        self.assertTrue(self.session.request.call_args.args[1].endswith("/repositories/id-1/stats/branches"))

    def test_repos_by_commit_order(self):
        repos = [
            {"id": "a", "name": "Old", "defaultBranch": "refs/heads/main"},
            {"id": "b", "name": "Empty"},
            {"id": "c", "name": "New", "defaultBranch": "refs/heads/main"},
        ]
        dates = {"a": "2023-01-01T00:00:00Z", "c": "2025-01-01T00:00:00Z"}

        def get(method, url, params):
            if url.endswith("/repositories"):
                return fake_response({"value": repos})
            repo_id = url.split("/repositories/")[1].split("/")[0]
            return fake_response({"value": [{"name": "main", "commit": {"committer": {"date": dates[repo_id]}}}]})

        self.session.request.side_effect = get
        result = self.client.list_repos_by_commit()
        self.assertEqual([r["name"] for r, _ in result], ["New", "Old", "Empty"])
        self.assertIsNone(result[2][1])

    def test_resolve_authors_aliases_then_identity_then_name(self):
        def commit(author, email):
            return adobrowser.LastCommit(None, "main", author, email)

        def get(method, url, params):
            users = {"jdoe@corp.com": [{"providerDisplayName": "Jane Doe"}]}
            self.assertTrue(url.startswith("https://vssps.dev.azure.com/my-org/"))
            return fake_response({"value": users.get(params["filterValue"], [])})

        self.session.request.side_effect = get
        commits = [commit("jd", "JDoe@corp.com"), commit("janie", "jane@home.org"),
                   commit("J. Doe", "other@home.org"), commit("Bob", "bob@home.org"), None]
        names = self.client.resolve_authors(commits, {"jane@home.org": "Jane Doe", "j. doe": "Jane Doe"})
        self.assertEqual(names, {
            ("jd", "JDoe@corp.com"): "Jane Doe",          # identity lookup
            ("janie", "jane@home.org"): "Jane Doe",       # alias by email
            ("J. Doe", "other@home.org"): "Jane Doe",     # alias by name
            ("Bob", "bob@home.org"): "Bob",               # unknown: git name
        })
        looked_up = {c.kwargs["params"]["filterValue"] for c in self.session.request.call_args_list}
        self.assertEqual(looked_up, {"jdoe@corp.com", "bob@home.org"})

    def test_identity_lookup_failure_falls_back_to_name(self):
        self.session.request.return_value = fake_response({}, status=401)
        commit = adobrowser.LastCommit(None, "main", "Bob", "bob@corp.com")
        self.assertEqual(self.client.resolve_authors([commit]), {("Bob", "bob@corp.com"): "Bob"})

    def test_changed_work_items(self):
        def item(i, changed):
            return {"id": i, "fields": {"System.ChangedDate": changed}}

        responses = [
            fake_response({"workItems": [{"id": 1}, {"id": 2}, {"id": 3}]}),
            fake_response({"value": [item(1, "2025-01-02T00:00:00Z"), item(2, "2025-01-05T00:00:00Z")]}),
            fake_response({"value": [item(3, "2025-01-03T00:00:00Z")]}),
        ]
        self.session.request.side_effect = responses
        items = self.client.changed_work_items(days=7, batch_size=2)
        self.assertEqual([w["id"] for w in items], [2, 3, 1])

        calls = self.session.request.call_args_list
        self.assertEqual(calls[0].args, ("POST", "https://dev.azure.com/my-org/my-project/_apis/wit/wiql"))
        self.assertIn("@today - 7", calls[0].kwargs["json"]["query"])
        self.assertNotIn("AreaPath", calls[0].kwargs["json"]["query"])
        self.assertEqual(calls[1].kwargs["json"]["ids"], [1, 2])
        self.assertEqual(calls[2].kwargs["json"]["ids"], [3])

    def test_auth_failure(self):
        self.session.request.return_value = fake_response({}, status=203)
        with self.assertRaises(PermissionError):
            self.client.list_repos()


class SessionTest(unittest.TestCase):
    def test_pat_uses_basic_auth(self):
        with mock.patch.dict("os.environ", {"AZURE_DEVOPS_PAT": "secret"}):
            s = adobrowser.make_session("auto")
        self.assertEqual(s.auth, ("", "secret"))


class BrowserCredentialTest(unittest.TestCase):
    def test_login_once_then_reuse_record(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("azure.identity.InteractiveBrowserCredential") as cred_cls, \
                mock.patch("azure.identity.AuthenticationRecord") as record_cls:
            record_path = Path(tmp) / "auth_record.json"
            cred_cls.return_value.authenticate.return_value.serialize.return_value = "{}"

            adobrowser.browser_credential(record_path)
            cred_cls.return_value.authenticate.assert_called_once()
            self.assertEqual(record_path.read_text(), "{}")

            cred_cls.reset_mock()
            adobrowser.browser_credential(record_path)
            cred_cls.return_value.authenticate.assert_not_called()
            record_cls.deserialize.assert_called_once_with("{}")
            self.assertIs(cred_cls.call_args.kwargs["authentication_record"],
                          record_cls.deserialize.return_value)


class GitAuthorsTest(unittest.TestCase):
    """Runs real git against a local repo standing in for Azure DevOps."""

    def git(self, *args, cwd=None, author=None):
        env = None
        if author:
            name, email = author
            env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                       GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
        subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

    def test_authors_from_all_branches_and_incremental_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            self.git("init", "-q", "-b", "main", str(src))
            jane, jd = ("Jane", "jane@corp.com"), ("jd", "jane@home.org")
            self.git("commit", "-q", "--allow-empty", "-m", "1", cwd=src, author=jane)
            self.git("commit", "-q", "--allow-empty", "-m", "2", cwd=src, author=jane)
            self.git("checkout", "-q", "-b", "dev", cwd=src)
            self.git("commit", "-q", "--allow-empty", "-m", "3", cwd=src, author=jd)
            self.git("checkout", "-q", "main", cwd=src)

            session = mock.Mock(auth=("", "secret"))
            session.request.return_value = fake_response({"value": [
                {"id": "a", "name": "A", "defaultBranch": "refs/heads/main", "remoteUrl": str(src)},
                {"id": "b", "name": "Empty", "remoteUrl": "unused"},
            ]})
            client = adobrowser.AzureDevOpsClient(session, "my-org", "my-project")
            cache = Path(tmp) / "cache"

            self.assertEqual(client.list_authors(cache_dir=cache), {jane: {"A": 2}, jd: {"A": 1}})
            self.assertTrue((cache / "my-org" / "my-project" / "a.git" / "HEAD").exists())

            # Second run fetches into the existing clone
            self.git("commit", "-q", "--allow-empty", "-m", "4", cwd=src, author=jd)
            self.assertEqual(client.list_authors(cache_dir=cache), {jane: {"A": 2}, jd: {"A": 2}})

    def test_git_env_passes_pat_as_header(self):
        env = adobrowser.git_env(mock.Mock(auth=("", "secret")))
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraHeader")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Authorization: Basic OnNlY3JldA==")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

        env = adobrowser.git_env(mock.Mock(auth=None, headers={"Authorization": "Bearer t"}))
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Authorization: Bearer t")


class BoardsTest(unittest.TestCase):
    TEAMS = [
        {"team": "Team A", "areas": [("P\\A", True)],
         "levels": [("Features", "portfolio", {"Feature"}), ("Stories", "requirement", {"User Story", "Bug"}),
                    ("Taskboard", "task", {"Task"})]},
        {"team": "Team B", "areas": [("P\\A\\Sub", False), ("P\\B", False)],
         "levels": [("Stories", "requirement", {"User Story"}), ("Taskboard", "task", {"Task", "Bug"})]},
    ]

    def boards(self, kind, area):
        return adobrowser.boards_for({"System.WorkItemType": kind, "System.AreaPath": area}, self.TEAMS)

    def test_area_and_type_select_boards(self):
        self.assertEqual(self.boards("User Story", "P\\A"), ["Team A: Stories"])
        self.assertEqual(self.boards("User Story", "p\\a\\sub"), ["Team A: Stories", "Team B: Stories"])
        self.assertEqual(self.boards("Bug", "P\\A\\Sub"), ["Team A: Stories", "Team B: Taskboard"])
        self.assertEqual(self.boards("Task", "P\\A\\Sub\\Deeper"), ["Team A: Taskboard"])
        self.assertEqual(self.boards("Feature", "P\\B"), [])
        self.assertEqual(self.boards("User Story", "P\\AB"), [])   # prefix of a name is not a sub-area

    def test_team_boards_reads_settings(self):
        session = mock.Mock()
        responses = {
            "https://dev.azure.com/my-org/_apis/projects/my-project/teams":
                {"value": [{"id": "t1", "name": "Team A"}, {"id": "t2", "name": "Custom"}]},
            "https://dev.azure.com/my-org/my-project/t1/_apis/work/teamsettings/teamfieldvalues":
                {"field": {"referenceName": "System.AreaPath"}, "values": [{"value": "P\\A", "includeChildren": True}]},
            "https://dev.azure.com/my-org/my-project/t1/_apis/work/backlogs": {"value": [
                {"name": "Stories", "type": "requirement", "workItemTypes": [{"name": "User Story"}]},
                {"name": "Epics", "type": "portfolio", "isHidden": True, "workItemTypes": [{"name": "Epic"}]},
                {"name": "Tasks", "type": "task", "workItemTypes": [{"name": "Task"}]},
            ]},
            "https://dev.azure.com/my-org/my-project/t2/_apis/work/teamsettings/teamfieldvalues":
                {"field": {"referenceName": "Custom.Team"}, "values": []},
        }
        session.request.side_effect = lambda method, url, params: fake_response(responses[url])
        client = adobrowser.AzureDevOpsClient(session, "my-org", "my-project")
        self.assertEqual(client.team_boards(), [{
            "team": "Team A",
            "areas": [("P\\A", True)],
            "levels": [("Stories", "requirement", {"User Story"}), ("Taskboard", "task", {"Task"})],
        }])


class ConfigTest(unittest.TestCase):
    def test_reads_ini_and_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            path.write_text("[azure_devops]\norganization = ini-org\nproject = ini-project\n"
                            "[aliases]\njd = Jane Doe\nMe@Example.com = Jane Doe\n"
                            "[blacklist]\nrepos = Old-Repo, Fork\n  Archive\n")
            with mock.patch.dict("os.environ", {}, clear=True):
                self.assertEqual(adobrowser.load_config(path), {
                    "org": "ini-org", "project": "ini-project",
                    "aliases": {"jd": "Jane Doe", "me@example.com": "Jane Doe"},
                    "blacklist": {"old-repo", "fork", "archive"},
                })
            with mock.patch.dict("os.environ", {"AZURE_DEVOPS_ORG": "env-org"}, clear=True):
                self.assertEqual(adobrowser.load_config(path)["org"], "env-org")

    def test_missing_file_gives_none(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(adobrowser.load_config(Path("/nonexistent/config.ini")),
                             {"org": None, "project": None, "aliases": {}, "blacklist": set()})


class CliTest(unittest.TestCase):
    def test_repos_output(self):
        session = mock.Mock()
        session.request.return_value = fake_response({"value": [
            {"name": "MyRepo", "defaultBranch": "refs/heads/main",
             "webUrl": "https://dev.azure.com/my-org/my-project/_git/MyRepo"},
        ]})
        out = io.StringIO()
        with mock.patch.object(adobrowser, "make_session", return_value=session), redirect_stdout(out):
            adobrowser.main(["--org", "my-org", "--project", "my-project", "repos"])
        self.assertIn("MyRepo", out.getvalue())
        self.assertIn("main", out.getvalue())

    def test_repos_days_keeps_recent_commits_only(self):
        now = adobrowser.datetime.now(adobrowser.timezone.utc)
        recent = adobrowser.LastCommit(now - adobrowser.timedelta(days=2), "main", "Jane", "jane@corp.com")
        old = adobrowser.LastCommit(now - adobrowser.timedelta(days=40), "main", "Bob", "bob@corp.com")
        results = [({"name": "Zeta", "webUrl": "z"}, recent), ({"name": "Old", "webUrl": "o"}, old),
                   ({"name": "Alpha", "webUrl": "a"}, recent), ({"name": "Empty", "webUrl": "e"}, None)]
        out = io.StringIO()
        with mock.patch.object(adobrowser, "make_session"), \
                mock.patch.object(adobrowser.AzureDevOpsClient, "list_repos_by_commit", return_value=results), \
                mock.patch.object(adobrowser.AzureDevOpsClient, "resolve_authors",
                                  return_value={("Jane", "jane@corp.com"): "Jane Doe"}), \
                redirect_stdout(out):
            adobrowser.main(["--config", "/nonexistent/config.ini", "--org", "my-org",
                             "--project", "my-project", "repos", "--days", "7"])
        self.assertEqual([line.split()[0] for line in out.getvalue().splitlines()], ["Alpha", "Zeta"])

    def test_repos_author_lists_commits_of_common_name(self):
        now = adobrowser.datetime.now(adobrowser.timezone.utc)
        last = adobrowser.LastCommit(now, "main", "Jane", "jane@corp.com")
        repo = {"name": "Web", "id": "w", "webUrl": "w"}
        commits = [(repo, adobrowser.Commit(now, "dev", "jd", "jane@home.org", "abcdef123456", "Fix login")),
                   (repo, adobrowser.Commit(now, "main", "Bob", "bob@corp.com", "0123456789ab", "Other"))]
        names = {("jd", "jane@home.org"): "Jane Doe", ("Bob", "bob@corp.com"): "Bob"}
        out = io.StringIO()
        with mock.patch.object(adobrowser, "make_session"), \
                mock.patch.object(adobrowser.AzureDevOpsClient, "list_repos_by_commit", return_value=[(repo, last)]), \
                mock.patch.object(adobrowser.AzureDevOpsClient, "list_commits_since", return_value=commits), \
                mock.patch.object(adobrowser.AzureDevOpsClient, "resolve_authors", return_value=names), \
                redirect_stdout(out):
            adobrowser.main(["--config", "/nonexistent/config.ini", "--org", "my-org",
                             "--project", "my-project", "repos", "--days", "7", "--author", "jane doe"])
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        for text in ("Web", "dev", "abcdef12", "Fix login"):
            self.assertIn(text, lines[0])

    def test_repos_author_requires_days(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            adobrowser.main(["--org", "o", "--project", "p", "repos", "--author", "Jane Doe"])

    def test_authors_summary_merges_aliases_and_lists_repos(self):
        counts = {("Jane", "jane@corp.com"): {"Web": 3, "api": 1},
                  ("jd", "jane@home.org"): {"Tools": 2, "Web": 1},
                  ("Bob", "bob@corp.com"): {"api": 4}}
        names = {("Jane", "jane@corp.com"): "Jane Doe", ("jd", "jane@home.org"): "Jane Doe",
                 ("Bob", "bob@corp.com"): "Bob"}
        out = io.StringIO()
        with mock.patch.object(adobrowser, "make_session"), \
                mock.patch.object(adobrowser.AzureDevOpsClient, "list_authors", return_value=counts), \
                mock.patch.object(adobrowser.AzureDevOpsClient, "resolve_authors", return_value=names), \
                redirect_stdout(out):
            adobrowser.main(["--config", "/nonexistent/config.ini", "--org", "my-org",
                             "--project", "my-project", "authors", "--summary"])
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[1].split(), ["Bob", "4", "api"])
        self.assertEqual(lines[2].split(), ["Jane", "Doe", "7", "api,", "Tools,", "Web"])


    def test_workitems_output(self):
        session = mock.Mock()
        session.request.side_effect = [
            fake_response({"workItems": [{"id": 42}]}),
            fake_response({"value": [{"id": 42, "fields": {
                "System.WorkItemType": "Bug", "System.State": "Active", "System.Title": "Fix login",
                "System.ChangedDate": "2025-03-05T08:30:12.345Z",
                "System.ChangedBy": {"displayName": "Jane Doe"}, "System.AreaPath": "my-project\\Team A",
                "Microsoft.VSTS.Scheduling.OriginalEstimate": 4.5,
            }}]}),
        ]
        teams = [{"team": "Team A", "areas": [("my-project\\Team A", True)],
                  "levels": [("Bugs", "requirement", {"Bug"})]}]
        out = io.StringIO()
        with mock.patch.object(adobrowser, "make_session", return_value=session), \
                mock.patch.object(adobrowser.AzureDevOpsClient, "team_boards", return_value=teams), \
                redirect_stdout(out):
            adobrowser.main(["--org", "my-org", "--project", "my-project", "workitems", "--days", "3",
                           "--area", "my-project\\Team's A", "--current", "--planned"])
        line = out.getvalue().splitlines()[1]
        for text in ("42", "2025-03-05 08:30", "Bug", "Active", "Jane Doe", "4.5", "my-project\\Team A", "Team A: Bugs", "Fix login"):
            self.assertIn(text, line)
        query = session.request.call_args_list[0].kwargs["json"]["query"]
        self.assertIn("@today - 3", query)
        self.assertIn("[System.AreaPath] UNDER 'my-project\\Team''s A'", query)
        self.assertIn("[System.State] IN ('Active', 'Resolved', 'Closed', 'UP-NEXT')", query)

if __name__ == "__main__":
    unittest.main()
