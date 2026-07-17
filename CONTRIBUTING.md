# Contributing

## Development setup

The Python 3 tooling lives in `BitCracker/btcrecover-master/`.

```bash
cd BitCracker/btcrecover-master
pip install -r requirements-dev.txt
```

## Running the checks locally

These are the same checks CI runs:

```bash
ruff check multibit_check.py tests/     # lint
python -m pytest tests/ -v              # tests
```

The test suite covers the MultiBit key derivation (known-answer tests against the
bundled test wallet), the typo generator, and the tokenlist parser. It needs only
`pycryptodome`, so it runs anywhere without a GPU.

## Building the CUDA tool

The CUDA tool is Windows + MSVC + CUDA-toolkit specific and is built locally, not
in CI:

```bat
cd BitCracker\btcrecover-master
build_cuda.bat
```

## Branch protection

`master` is protected. Changes land through pull requests, not direct pushes, and
the CI workflow must pass before a PR can merge. To configure this on a fork
(Settings > Branches > Add branch protection rule):

- Require a pull request before merging
- Require status checks to pass: the `Lint & test` jobs from CI
- Require branches to be up to date before merging

This keeps the linear-history-of-direct-commits problem from recurring: nothing
reaches `master` without the tests going green first.
