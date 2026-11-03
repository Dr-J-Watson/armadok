# Contributing to Armadok

Thank you for your interest. Armadok is a self-hosted interface to deploy Docker Compose projects, publish them with Traefik (HTTPS, DNS) and see what is connected to what. The project is **in design**: the first code is being written, so the most useful contributions right now are feedback, bug reports on early releases, and translations once the interface exists.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems must **not** be reported in public: see [SECURITY.md](SECURITY.md).

## Ways to help

- **Report a bug** or **suggest a feature** with the issue forms (search first to avoid duplicates).
- **Ask a question** with an issue labelled `question`.
- **Improve the documentation**, in English and in French.
- **Translate**: the interface is available in English and French; adding a language will be a single translation file.
- **Send a pull request** (see below). For anything larger than a small fix, open an issue first so we can agree on the approach.

## Pull requests

1. Fork the repository and create a branch from `main`: `feat/short-name`, `fix/short-name`, `docs/short-name`.
2. Keep each pull request focused on one change.
3. Write the pull request **title** as a Conventional Commit (it becomes the squash commit and feeds the changelog).
4. Fill in the pull request template, including how you tested.
5. Make sure the checks pass. A maintainer reviews, may ask for changes, then squash-merges.

### Commit and title format

[Conventional Commits](https://www.conventionalcommits.org/), in **English**:

```text
feat(traefik): pause a whole domain in one click
fix(dns): keep the TTL when updating a record
docs: clarify the worker installation steps
```

Types: `feat`, `fix`, `perf`, `docs`, `refactor`, `test`, `build`, `ci`, `chore`, `revert`.
Scopes: `docker`, `traefik`, `dns`, `certs`, `git`, `nodes`, `worker`, `rights`, `auth`, `audit`, `notes`, `notify`, `ui`, `i18n`, `api`, `ops`, `security`.
A breaking change is marked with `!` (`feat(api)!: ...`) or a `BREAKING CHANGE:` footer.

### Local safety hook

Enable the repository's pre-commit hook once; it refuses commits that contain a secret-looking value, a personal e-mail address, or a file that is git-ignored:

```bash
git config core.hooksPath .githooks
```

Use your GitHub **noreply** address for commits (GitHub → Settings → Emails → "Keep my email addresses private").

## Code guidelines

- Code, comments, commit messages and issues are written in **English**.
- Code is **commented**: a header on each module, a docstring on every public function, class and endpoint, and comments that explain **why**, not what.
- Small modules with one responsibility, explicit types, no duplicated logic, no dead code.
- A new feature is a self-contained module that registers what it provides; adding one should not require editing unrelated modules.
- Every user-visible text goes through the translation files (English and French). The server returns error codes, not sentences.
- Never commit secrets, real domains or real IP addresses; use `example.com` and `192.0.2.x` in examples.
- Add or update tests, and update the documentation when behaviour changes.

Detailed development setup (how to run the backend, the interface and the tests) will be added here when the first code is published.

## Documentation

`README.md` is a short bilingual hub; `README.en.md` and `README.fr.md` are the full documentation and must keep the **same sections in the same order**. If you change one, change the other (or say in the pull request that a translation is needed). A script checks their structure: `python scripts/check_readme_parity.py`.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
