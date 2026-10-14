"""Tests with simulated API responses (no access to Azure DevOps needed)."""

import io
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

    def test_auth_failure(self):
        self.session.get.return_value = fake_response({}, status=203)
        with self.assertRaises(PermissionError):
            self.client.list_repos()


class SessionTest(unittest.TestCase):
    def test_pat_uses_basic_auth(self):
        with mock.patch.dict("os.environ", {"AZURE_DEVOPS_PAT": "secret"}):
            s = adobrowser.make_session("auto")
        self.assertEqual(s.auth, ("", "secret"))


class ConfigTest(unittest.TestCase):
    def test_reads_ini_and_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            path.write_text("[azure_devops]\norganization = ini-org\nproject = ini-project\n")
            with mock.patch.dict("os.environ", {}, clear=True):
                self.assertEqual(adobrowser.load_config(path), {"org": "ini-org", "project": "ini-project"})
            with mock.patch.dict("os.environ", {"AZURE_DEVOPS_ORG": "env-org"}, clear=True):
                self.assertEqual(adobrowser.load_config(path)["org"], "env-org")

    def test_missing_file_gives_none(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(adobrowser.load_config(Path("/nonexistent/config.ini")),
                             {"org": None, "project": None})


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
