# Offline developer health tools

These tools inspect **source release declarations** and **recorded test outcomes**.
They do not import or run application modules, start a server, run a simulation,
train a model, invoke a shell, access a network, or submit application actions.
Existing application, training and model interfaces are unchanged.

## Run from the checkout root

Python 3.11+ needs no additional dependency. On Python 3.10, install the development
extra (`python -m pip install -e '.[dev]'`) to provide the TOML reader.

```bash
python -m devtools.health release
python -m devtools.health junit --report reports/unit-tests.xml
python -m devtools.health schemas
python -m unittest discover -s devtools/checks -v
```

The command prints JSON. Exit status is 0 for a clean source check/all recorded
cases passing, 1 for findings/non-passing or empty test reports, and 2 for invalid
input or unreadable files. A skipped testcase does not count as passed. The
release check verifies the declared top-level console callable using Python's
AST, matches package-data globs, and checks declared external data files exist.
It does not execute those entry points. Re-exported callables are reported as
unresolved rather than imported.

## Function-call integration

A trusted host imports `function_schemas()` and registers the returned function
definitions with its model provider (adapt the outer wrapper if that provider
uses a different API dialect). For a selected call, JSON-decode the arguments and
invoke `dispatch(checkout_root, name, arguments)`. Serialize the returned object
as the tool response. Catch `ValueError`, `TypeError`, `OSError` and XML parse errors at the
host boundary and return a normal tool error; do not turn errors into a pass.

There are exactly two allowed functions:

- `inspect_release({})`: inspect source release declarations.
- `summarize_test_report({"report": "reports/unit-tests.xml"})`: read a JUnit report.

The host chooses the checkout root; a model cannot override it. Arguments are
validated again at dispatch, not just described by schemas. There are no
`eval`, arbitrary command, arbitrary URL, simulator-state or action tools.
Paths must be relative POSIX paths within the checkout. Absolute paths, `..`,
backslashes and outward-pointing symlinks are rejected. Inputs are bounded to
4 MiB. XML accepts UTF-8 only and rejects DTD/entity declarations. Do not register
this adapter against an untrusted, concurrently modified checkout or place
secrets in a report: a path check alone is not an OS sandbox, and filesystem
permissions remain the host's responsibility.

## Evidence boundaries

A clean release check is **not** proof that an installed wheel contains every
runtime resource, that every public interface works, or that application tests
pass. It only covers the declarations present in `pyproject.toml`. The JUnit
summary counts actual `testcase` elements rather than trusting suite totals; it
does not prove report freshness, authorship or test coverage. Empty reports and
suite-level totals without testcase records are never treated as a pass.

Tests use small synthetic repositories and XML fixtures. They do not exercise
application behavior. These developer-tool tests are located in `devtools/checks`.
This upload package includes the application `checks/` directory as well; its
`.gitignore` does not ignore directories merely because they are named `tests`.
