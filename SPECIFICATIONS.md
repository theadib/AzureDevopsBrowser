# Specifications: Azure DevOps API calls

This document lists every call `adobrowser.py` makes to Azure DevOps: the REST endpoints, their parameters, the response fields the tool reads, and which command uses them. For installation and usage, see [README.md](README.md).

## Common conventions

### Hosts and base URLs

| Name in code | URL |
|---|---|
| `base` | `https://dev.azure.com/{org}/{project}/_apis/git` |
| `wit` | `https://dev.azure.com/{org}/{project}/_apis/wit` |
| `project_url` | `https://dev.azure.com/{org}/{project}` |
| (organization level) | `https://dev.azure.com/{org}/_apis/...` |
| (identity service) | `https://vssps.dev.azure.com/{org}/_apis/...` |

`{org}` and `{project}` come from `--org`/`--project`, then `AZURE_DEVOPS_ORG`/`AZURE_DEVOPS_PROJECT`, then `config.ini`.

### API version

Every REST request goes through `AzureDevOpsClient._request`, which adds the query parameter `api-version=7.1`.

### Authentication

Set up in `make_session`:

| Mode | Header |
|---|---|
| PAT (`AZURE_DEVOPS_PAT`) | `Authorization: Basic base64(":" + PAT)` (empty user name) |
| Browser login / `az login` | `Authorization: Bearer {token}` |

Entra ID tokens are requested for the Azure DevOps resource `499b84ac-1321-427f-aa17-267ca6975798/.default`, which Microsoft defines and which is the same for every tenant.

PAT scopes the tool needs:

| Scope | Needed for |
|---|---|
| Code: Read | all Git endpoints and `git clone`/`git fetch` |
| Work Items: Read | WIQL and work item batch |
| Project and Team: Read | teams, team field values, backlogs |
| Identity: Read | identity lookup (optional; the tool falls back without it) |

### Error handling

- HTTP `401`, or `203` (Azure DevOps returns a login page with 203 instead of JSON when the token is invalid or expired), raises `PermissionError("Authentication failed (check your token).")`.
- Any other 4xx/5xx raises `requests.HTTPError` via `raise_for_status()`.
- The tool does not follow continuation tokens or `x-ms-continuationtoken` headers; each list endpoint is read with a single request.

### Concurrency

Calls that run once per repo, team or email use a `ThreadPoolExecutor`: 8 workers for branch statistics, team settings and identity lookups, and 4 workers for `git` clones and fetches.

---

## Git (Repos)

### 1. List repositories

```
GET https://dev.azure.com/{org}/{project}/_apis/git/repositories?api-version=7.1
```

- **Method:** `list_repos`
- **Used by:** `repos`, `repos --sort commit`, `authors`
- **Response fields read:** `value[].id`, `name`, `defaultBranch`, `webUrl`, `remoteUrl`, `isDisabled`
- **Notes:** repos whose name or ID is in `[blacklist] repos` in `config.ini` are removed from the result (case-insensitive), so no further calls are made for them. A repo without `defaultBranch` is empty. Empty and disabled repos are skipped when branch statistics or clones are needed.

### 2. Branch statistics

```
GET https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repoId}/stats/branches?api-version=7.1
```

- **Method:** `last_commit` (called by `list_repos_by_commit` once per repo, in parallel)
- **Used by:** `repos --sort commit`, `repos --days`
- **Response fields read:** `value[].name`, `value[].commit.committer.date`, `value[].commit.author.name`, `value[].commit.author.email`
- **Logic:** the response contains the head commit of every branch. The tool takes the branch whose head has the newest **committer** date and reports that commit's **author** name and email.
- **Errors:** an `HTTPError` for one repo is ignored; that repo is listed without a commit.

### 2b. List commits of a branch

```
GET https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repoId}/commits
    ?searchCriteria.itemVersion.version={branch}
    &searchCriteria.itemVersion.versionType=branch
    &searchCriteria.fromDate={since, e.g. 2025-03-01T00:00:00Z}
    &searchCriteria.$top=1000
    &searchCriteria.$skip={0, 1000, ...}
    &api-version=7.1
```

- **Method:** `commits_since` (called by `list_commits_since` once per repo, in parallel)
- **Used by:** `repos --days N --author NAME`
- **Response fields read:** `value[].commitId`, `committer.date`, `author.name`, `author.email`, `comment` (first line only)
- **Logic:** the tool first reads the branch statistics (call 2) of each repo that passed the `--days` filter. It then reads commits only for branches whose head commit is within the time window, newest head first, and pages with `$skip` until a page has fewer than 1,000 commits. A commit that is on several branches is listed once, with the first branch it was found on. Author names are resolved to common names (aliases, then call 12) and compared with `--author`, case-insensitive.
- **Notes:** `--author` is only accepted together with `--days`, which limits both the repos and the branches that are read. `comment` may be truncated by the server (`commentTruncated`); only its first line is shown.
- **Errors:** an `HTTPError` for one repo is ignored; that repo's commits are left out.

### 3. List branches

```
GET https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}/refs?filter=heads/&api-version=7.1
```

- **Method:** `list_branches`
- **Used by:** `branches`
- **Response fields read:** `value[].name` (the prefix `refs/heads/` is removed for display)
- **Notes:** `{repo}` can be the repo name or its ID.

### 4. List a folder

```
GET https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}/items
    ?scopePath={path}
    &recursionLevel=OneLevel
    [&versionDescriptor.version={branch}&versionDescriptor.versionType=branch]
    &api-version=7.1
```

- **Method:** `list_items`
- **Used by:** `ls`
- **Response fields read:** `value[].path`, `value[].isFolder`
- **Notes:** the response includes the folder itself as its first entry; the tool removes it. Without `--branch`, the version descriptor is omitted and the server uses the default branch.

### 5. Read a file

```
GET https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}/items
    ?path={path}
    &includeContent=true
    [&versionDescriptor.version={branch}&versionDescriptor.versionType=branch]
    &api-version=7.1
```

- **Method:** `get_file`
- **Used by:** `cat`
- **Response fields read:** `content`
- **Notes:** the tool requests the JSON form and prints `content` as text, so it is meant for text files.

### 6. Git over HTTPS (not REST)

```
git clone --bare --filter=blob:none {remoteUrl} ~/.adobrowser/git/{org}/{project}/{repoId}.git
git -C {path} fetch --prune origin
git -C {path} log --branches --format=%an%x1f%ae
```

- **Functions:** `git_log_authors`, `git_env`; called by `list_authors`
- **Used by:** `authors`
- **Authentication:** the same `Authorization` header as the REST session, passed to git as `http.extraHeader` through the `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0` environment variables (git ≥ 2.31), so the token is not on the command line. `GIT_TERMINAL_PROMPT=0` stops git from asking for credentials.
- **Notes:** the first run makes a blob-less bare clone (commits and trees, no file contents) and sets the refspec `+refs/heads/*:refs/heads/*`; later runs only fetch. git reads the full history of all branches locally instead of through paged REST requests. If git fails for a repo, the tool prints a warning and continues with the other repos.

---

## Work item tracking

### 7. WIQL query

```
POST https://dev.azure.com/{org}/{project}/_apis/wit/wiql?api-version=7.1
Content-Type: application/json

{
  "query": "SELECT [System.Id] FROM WorkItems
            WHERE [System.TeamProject] = @project
              AND [System.ChangedDate] >= @today - {days}
              [AND [System.AreaPath] UNDER '{area}']
              [AND [System.State] IN ({states})]
            ORDER BY [System.ChangedDate] DESC"
}
```

- **Method:** `changed_work_items`
- **Used by:** `workitems` (`--days`, default 14; `--area`, `--current` and `--planned`, optional)
- **Response fields read:** `workItems[].id`
- **Notes:** WIQL returns only IDs. `{states}` is `'Active', 'Resolved', 'Closed'` with `--current`, `'UP-NEXT'` with `--planned`, and all four with both; without either option the state condition is left out. In `{area}`, a single quote is escaped by doubling it. `{days}` is cast to `int` before it goes into the query. WIQL returns at most 20,000 work items per query.

### 8. Work items batch

```
POST https://dev.azure.com/{org}/{project}/_apis/wit/workitemsbatch?api-version=7.1
Content-Type: application/json

{
  "ids": [ ...up to 200 IDs... ],
  "fields": [
    "System.Id", "System.WorkItemType", "System.State", "System.Title",
    "System.ChangedDate", "System.ChangedBy", "System.AssignedTo", "System.AreaPath",
    "Microsoft.VSTS.Scheduling.OriginalEstimate"
  ]
}
```

- **Method:** `changed_work_items`
- **Used by:** `workitems`
- **Response fields read:** `value[].id`, `value[].fields[...]`. `System.ChangedBy` and `System.AssignedTo` are identity objects, of which only `displayName` is read; `System.AssignedTo` is missing for unassigned items. `Microsoft.VSTS.Scheduling.OriginalEstimate` (hours, shown as EFFORT) is missing when it was never set or the work item type does not have it.
- **Notes:** the endpoint accepts at most 200 IDs per request, so the IDs from the WIQL query are sent in batches of 200. After all batches are read, the items are sorted again by `System.ChangedDate`, newest first.

---

## Teams and boards

These calls run only when `workitems` returns at least one item. They are used to work out which team boards show each work item (see `boards_for`).

### 9. List teams

```
GET https://dev.azure.com/{org}/_apis/projects/{project}/teams?$top=1000&api-version=7.1
```

- **Method:** `team_boards`
- **Used by:** `workitems`
- **Response fields read:** `value[].id`, `value[].name`
- **Notes:** this is an organization-level endpoint, with the project in the path. `$top=1000` replaces the default page size of 100.

### 10. Team field values

```
GET https://dev.azure.com/{org}/{project}/{teamId}/_apis/work/teamsettings/teamfieldvalues?api-version=7.1
```

- **Method:** `team_boards` (once per team, in parallel)
- **Used by:** `workitems`
- **Response fields read:** `field.referenceName`, `values[].value`, `values[].includeChildren`
- **Notes:** teams whose team field is not `System.AreaPath` are skipped, together with their backlogs.

### 11. Team backlogs

```
GET https://dev.azure.com/{org}/{project}/{teamId}/_apis/work/backlogs?api-version=7.1
```

- **Method:** `team_boards` (once per team that uses area paths)
- **Used by:** `workitems`
- **Response fields read:** `value[].name`, `value[].type`, `value[].isHidden`, `value[].workItemTypes[].name`
- **Notes:** hidden backlog levels are skipped. The level with `type == "task"` is shown as `Taskboard`. The response already reflects the team's "bugs on backlog/taskboard" setting, so the tool does not read that setting separately. Iteration settings are not read.

---

## Identities

### 12. Look up a user by email

```
GET https://vssps.dev.azure.com/{org}/_apis/identities
    ?searchFilter=General
    &filterValue={email}
    &queryMembership=None
    &api-version=7.1
```

- **Method:** `display_name` (called by `resolve_authors` once per distinct email, in parallel)
- **Used by:** `repos --sort commit`, `repos --author`, `authors`
- **Response fields read:** `value[0].providerDisplayName`
- **Notes:** this endpoint is on the `vssps` host, not `dev.azure.com`. Emails that already match an entry in `[aliases]` (by email or author name) are not looked up. An `HTTPError` or `PermissionError` (for example, a PAT without **Identity: Read**) is ignored, and the tool falls back to the git author name.

---

## Calls per command

| Command | Calls |
|---|---|
| `repos` | 1 |
| `repos --sort commit`, `repos --days` | 1, then 2 per repo, then 12 per new email |
| `repos --days N --author NAME` | 1, 2 per repo; then for each repo with a recent commit: 2 again and 2b per recent branch (and page); 12 per new email |
| `authors [repo]` | 1, then 6 per repo, then 12 per new email |
| `workitems` | 7, 8 per 200 items; if there are items: 9, then 10 and 11 per team |
| `branches REPO` | 3 |
| `ls REPO [PATH]` | 4 |
| `cat REPO PATH` | 5 |
