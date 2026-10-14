# AzureDevopsBrowser (Proof of Concept)

A small Python command-line tool that browses the Git repos of an Azure DevOps project via the REST API (version 7.1). The organization and project are read from `config.ini`.

## Installation

```bash
pip install -r requirements.txt   # Python 3.9 or newer
```

## Configuration

Enter your organization and project in `config.ini` (in the project folder):

```ini
[azure_devops]
organization = my-organization
project = my-project
```

### Author names

`repos --sort commit` shows the author of each repo's newest commit. Git stores only the name and email from the author's git config, so one person can appear under several aliases. The tool merges them into one name in this order:

1. an entry in the optional `[aliases]` section of `config.ini`, matched by email or author name (case-insensitive):
   ```ini
   [aliases]
   jd = Jane Doe
   jane@home.example = Jane Doe
   ```
2. the display name of the Azure DevOps user with the commit email (identity lookup),
3. otherwise the author name from git.

To find the aliases, `authors` lists every distinct name/email combination with its commit count and the common name it currently resolves to. It reads the full history of every branch with git instead of the REST API: each repo is cloned once as a bare, blob-less clone (commit history only, no file contents) into `~/.adobrowser/git/`, and later runs only fetch new commits. This needs `git` 2.31 or newer; delete that folder to free the space.

The identity lookup needs the **Identity: Read** scope when you use a PAT; without it, the tool falls back to the aliases and git names.

## Authentication

There are two options:

1. **Personal Access Token (PAT).** Create the token at https://dev.azure.com/<organization>/_usersSettings/tokens, select your organization (not "All accessible organizations") and the scopes **Code: Read**, **Work Items: Read**, **Project and Team: Read** and **Identity: Read**.
   ```bash
   export AZURE_DEVOPS_PAT=...        # Windows: set AZURE_DEVOPS_PAT=...
   ```
2. **Browser login.** Without a PAT, the tool opens the browser for the Microsoft login. It only asks once: the account is saved in `~/.adobrowser/auth_record.json` and the tokens in a persistent cache, so later runs sign in silently until the refresh token expires (typically after 90 days of inactivity). The cache uses the system keyring if one is available; otherwise it is stored unencrypted in `~/.IdentityService/`. To log in again, delete `~/.adobrowser/auth_record.json`.

   With `--auth azcli` the tool uses the token from `az login` instead.

## Usage

```bash
python adobrowser.py repos                          # all repos
python adobrowser.py repos --sort commit            # newest commit (any branch) first, with author and email
python adobrowser.py authors                        # all author name/email combinations
python adobrowser.py authors MyRepo                 # only one repo
python adobrowser.py workitems                      # work items changed in the last 14 days
python adobrowser.py workitems --days 30            # ... in the last 30 days
python adobrowser.py workitems --area "MyProject\Team A"  # only this area path and below
python adobrowser.py branches MyRepo                # branches
python adobrowser.py ls  MyRepo /                   # list a folder
python adobrowser.py ls  MyRepo /src --branch dev
python adobrowser.py cat MyRepo /README.md          # print a file
```

The values from `config.ini` can be overridden with the environment variables `AZURE_DEVOPS_ORG` and `AZURE_DEVOPS_PROJECT` and with the options `--org` and `--project` (precedence: option, then environment variable, then `config.ini`). Use `--config PATH` to use a different config file.

### Boards

`workitems` shows on which board each work item appears, as `Team: Board` (e.g. `Team A: Stories`, or `Team A: Taskboard` for tasks on the sprint taskboard). It reads every team's area paths and backlog levels and matches them against the work item's area path and type. If several teams cover the area path, all their boards are listed. Iteration settings are not taken into account. With a PAT this needs the **Project and Team: Read** scope.

## Tests

```bash
python -m unittest -v test_adobrowser
```

The tests use simulated API responses. The tool has not yet been tested against a real organization.
