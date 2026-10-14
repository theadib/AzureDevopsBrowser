"""Tests with simulated API responses (no access to Azure DevOps needed)."""

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
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
        self.session.get.return_value = fake_response({"value": [{"name": "A"}]})
        self.assertEqual(self.client.list_repos(), [{"name": "A"}])
        url = self.session.get.call_args.args[0]
        params = self.session.get.call_args.kwargs["params"]
        self.assertEqual(url, "https://dev.azure.com/my-org/my-project/_apis/git/repositories")
        self.assertEqual(params["api-version"], "7.1")

    def test_branches_strip_prefix(self):
        self.session.get.return_value = fake_response(
            {"value": [{"name": "refs/heads/main"}, {"name": "refs/heads/feature/x"}]}
        )
        self.assertEqual(self.client.list_branches("R"), ["main", "feature/x"])

    def test_list_items_drops_folder_itself_and_passes_branch(self):
        self.session.get.return_value = fake_response(
            {"value": [{"path": "/src", "isFolder": True}, {"path": "/src/main.c"}]}
        )
        self.assertEqual(self.client.list_items("R", "/src", "dev"), [{"path": "/src/main.c"}])
        params = self.session.get.call_args.kwargs["params"]
        self.assertEqual(params["versionDescriptor.version"], "dev")
        self.assertEqual(params["recursionLevel"], "OneLevel")

    def test_last_commit_picks_newest_branch(self):
        self.session.get.return_value = fake_response({"value": [
            {"name": "main", "commit": {"committer": {"date": "2024-01-01T10:00:00Z"}}},
            {"name": "feature/x", "commit": {"committer": {"date": "2025-03-05T08:30:00Z"},
                                             "author": {"name": "Jane Doe", "email": "jane@example.com"}}},
        ]})
        date, branch, author, email = self.client.last_commit("id-1")
        self.assertEqual(branch, "feature/x")
        self.assertEqual((author, email), ("Jane Doe", "jane@example.com"))
        self.assertEqual(date.year, 2025)
        self.assertTrue(self.session.get.call_args.args[0].endswith("/repositories/id-1/stats/branches"))

    def test_repos_by_commit_order(self):
        repos = [
            {"id": "a", "name": "Old", "defaultBranch": "refs/heads/main"},
            {"id": "b", "name": "Empty"},
            {"id": "c", "name": "New", "defaultBranch": "refs/heads/main"},
        ]
        dates = {"a": "2023-01-01T00:00:00Z", "c": "2025-01-01T00:00:00Z"}

        def get(url, params):
            if url.endswith("/repositories"):
                return fake_response({"value": repos})
            repo_id = url.split("/repositories/")[1].split("/")[0]
            return fake_response({"value": [{"name": "main", "commit": {"committer": {"date": dates[repo_id]}}}]})

        self.session.get.side_effect = get
        result = self.client.list_repos_by_commit()
        self.assertEqual([r["name"] for r, _ in result], ["New", "Old", "Empty"])
        self.assertIsNone(result[2][1])

    def test_resolve_authors_aliases_then_identity_then_name(self):
        def commit(author, email):
            return adobrowser.LastCommit(None, "main", author, email)

        def get(url, params):
            users = {"jdoe@corp.com": [{"providerDisplayName": "Jane Doe"}]}
            self.assertTrue(url.startswith("https://vssps.dev.azure.com/my-org/"))
            return fake_response({"value": users.get(params["filterValue"], [])})

        self.session.get.side_effect = get
        commits = [commit("jd", "JDoe@corp.com"), commit("janie", "jane@home.org"),
                   commit("J. Doe", "other@home.org"), commit("Bob", "bob@home.org"), None]
        names = self.client.resolve_authors(commits, {"jane@home.org": "Jane Doe", "j. doe": "Jane Doe"})
        self.assertEqual(names, {
            ("jd", "JDoe@corp.com"): "Jane Doe",          # identity lookup
            ("janie", "jane@home.org"): "Jane Doe",       # alias by email
            ("J. Doe", "other@home.org"): "Jane Doe",     # alias by name
            ("Bob", "bob@home.org"): "Bob",               # unknown: git name
        })
        looked_up = {c.kwargs["params"]["filterValue"] for c in self.session.get.call_args_list}
        self.assertEqual(looked_up, {"jdoe@corp.com", "bob@home.org"})

    def test_identity_lookup_failure_falls_back_to_name(self):
        self.session.get.return_value = fake_response({}, status=401)
        commit = adobrowser.LastCommit(None, "main", "Bob", "bob@corp.com")
        self.assertEqual(self.client.resolve_authors([commit]), {("Bob", "bob@corp.com"): "Bob"})

    def test_auth_failure(self):
        self.session.get.return_value = fake_response({}, status=203)
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
            session.get.return_value = fake_response({"value": [
                {"id": "a", "name": "A", "defaultBranch": "refs/heads/main", "remoteUrl": str(src)},
                {"id": "b", "name": "Empty", "remoteUrl": "unused"},
            ]})
            client = adobrowser.AzureDevOpsClient(session, "my-org", "my-project")
            cache = Path(tmp) / "cache"

            self.assertEqual(client.list_authors(cache_dir=cache), {jane: 2, jd: 1})
            self.assertTrue((cache / "my-org" / "my-project" / "a.git" / "HEAD").exists())

            # Second run fetches into the existing clone
            self.git("commit", "-q", "--allow-empty", "-m", "4", cwd=src, author=jd)
            self.assertEqual(client.list_authors(cache_dir=cache), {jane: 2, jd: 2})

    def test_git_env_passes_pat_as_header(self):
        env = adobrowser.git_env(mock.Mock(auth=("", "secret")))
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraHeader")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Authorization: Basic OnNlY3JldA==")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

        env = adobrowser.git_env(mock.Mock(auth=None, headers={"Authorization": "Bearer t"}))
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Authorization: Bearer t")


class ConfigTest(unittest.TestCase):
    def test_reads_ini_and_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            path.write_text("[azure_devops]\norganization = ini-org\nproject = ini-project\n"
                            "[aliases]\njd = Jane Doe\nMe@Example.com = Jane Doe\n")
            with mock.patch.dict("os.environ", {}, clear=True):
                self.assertEqual(adobrowser.load_config(path), {
                    "org": "ini-org", "project": "ini-project",
                    "aliases": {"jd": "Jane Doe", "me@example.com": "Jane Doe"},
                })
            with mock.patch.dict("os.environ", {"AZURE_DEVOPS_ORG": "env-org"}, clear=True):
                self.assertEqual(adobrowser.load_config(path)["org"], "env-org")

    def test_missing_file_gives_none(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(adobrowser.load_config(Path("/nonexistent/config.ini")),
                             {"org": None, "project": None, "aliases": {}})


class CliTest(unittest.TestCase):
    def test_repos_output(self):
        session = mock.Mock()
        session.get.return_value = fake_response({"value": [
            {"name": "MyRepo", "defaultBranch": "refs/heads/main",
             "webUrl": "https://dev.azure.com/my-org/my-project/_git/MyRepo"},
        ]})
        out = io.StringIO()
        with mock.patch.object(adobrowser, "make_session", return_value=session), redirect_stdout(out):
            adobrowser.main(["--org", "my-org", "--project", "my-project", "repos"])
        self.assertIn("MyRepo", out.getvalue())
        self.assertIn("main", out.getvalue())


if __name__ == "__main__":
    unittest.main()
