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

## Authentication

There are two options:

1. **Personal Access Token (PAT).** Create the token at https://dev.azure.com/<organization>/_usersSettings/tokens, select your organization (not "All accessible organizations") and the scope **Code: Read**.
   ```bash
   export AZURE_DEVOPS_PAT=...        # Windows: set AZURE_DEVOPS_PAT=...
   ```
2. **Browser login.** Without a PAT, the tool opens the browser for the Microsoft login. With `--auth azcli` it uses the token from `az login` instead.

## Usage

```bash
python adobrowser.py repos                          # all repos
python adobrowser.py branches MyRepo                # branches
python adobrowser.py ls  MyRepo /                   # list a folder
python adobrowser.py ls  MyRepo /src --branch dev
python adobrowser.py cat MyRepo /README.md          # print a file
```

The values from `config.ini` can be overridden with the environment variables `AZURE_DEVOPS_ORG` and `AZURE_DEVOPS_PROJECT` and with the options `--org` and `--project` (precedence: option, then environment variable, then `config.ini`). Use `--config PATH` to use a different config file.

## Tests

```bash
python -m unittest -v test_adobrowser
```

The tests use simulated API responses. The tool has not yet been tested against a real organization.
